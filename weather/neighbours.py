"""Neighbouring stations: compare this station's temperature and humidity with
nearby stations on Weather Underground.

The owner picks the stations (by Weather Underground station ID). `poll` fetches
each one's current conditions every WU_POLL_MINUTES with the free API key WU
gives to owners of stations that upload to it, and keeps only the temperature,
humidity and dew point. `compare` matches every neighbour reading to this
station's record for the same archive interval and averages the neighbours
after leaving out the highest and the lowest reading, so one sensor in the sun
(or in a cold hollow) doesn't move it.

The key is sent in the query string, so it never appears in a log or an error
message here: request errors are reported by type, not by their text (which
includes the URL).
"""
import datetime as dt
import logging
import math
import re
import statistics
import time
from collections import defaultdict

import requests
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .calibration import SUN_UP_WM2, _lstsq, solar_elevation, sun_times
from .ingest.store import interval_end
from .models import CalibratedValue, Neighbour, NeighbourReading, Observation

log = logging.getLogger(__name__)

TIMEOUT_S = 10
SPACING_S = 2.1                 # WU allows 30 requests a minute per key
DAILY_LIMIT = 1500              # …and 1,500 a day
LIVE_WINDOW = dt.timedelta(minutes=30)
RANGES = {'24h': 1, '7d': 7, '30d': 30}

_ID = re.compile(r'^[A-Z0-9]{3,20}$')


class WUError(Exception):
    pass


class WUKeyError(WUError):
    """The key is missing or Weather Underground rejected it: stop polling."""


def normalise_id(text):
    """A WU station ID from what the owner typed: the ID itself, any case, or a
    wunderground.com dashboard URL. Returns None if it doesn't look like one."""
    text = (text or '').strip()
    found = re.search(r'/pws/([A-Za-z0-9]+)', text)
    wu_id = (found.group(1) if found else text).upper()
    return wu_id if _ID.match(wu_id) else None


def _num(value, low=None, high=None):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or (low is not None and value < low) or (high is not None and value > high):
        return None
    return value


def parse(obs):
    """One WU current-conditions observation (metric units) → our fields."""
    metric = obs.get('metric') or {}
    return {
        'timestamp': dt.datetime.fromisoformat(obs['obsTimeUtc'].replace('Z', '+00:00')),
        'temp_c': _num(metric.get('temp'), -70, 65),
        'dewpoint_c': _num(metric.get('dewpt'), -80, 40),
        'humidity': _num(obs.get('humidity'), 0, 100),
        'qc_status': obs.get('qcStatus'),
        'label': (obs.get('neighborhood') or '')[:100],
        'latitude': _num(obs.get('lat'), -90, 90),
        'longitude': _num(obs.get('lon'), -180, 180),
        'elevation_m': _num(metric.get('elev')),
    }


def fetch_current(wu_id, session=None):
    """The station's current conditions, or None when it has no recent reading
    (offline, or no such station). Raises WUError / WUKeyError."""
    if not settings.WU_API_KEY:
        raise WUKeyError('No Weather Underground API key: set WU_API_KEY in .env.')
    params = {'stationId': wu_id, 'format': 'json', 'units': 'm', 'numericPrecision': 'decimal',
              'apiKey': settings.WU_API_KEY}
    try:
        response = (session or requests).get(settings.WU_API_URL, params=params, timeout=TIMEOUT_S)
    except requests.RequestException as exc:
        raise WUError(f'Weather Underground could not be reached ({type(exc).__name__}).') from None
    if response.status_code in (401, 403):
        raise WUKeyError('Weather Underground rejected the API key (WU_API_KEY).')
    if response.status_code in (204, 404):
        return None
    if response.status_code == 429:
        raise WUError("Weather Underground's request limit was reached; it will be tried again later.")
    if response.status_code != 200:
        raise WUError(f'Weather Underground answered HTTP {response.status_code}.')
    try:
        return parse(response.json()['observations'][0])
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise WUError('Weather Underground sent a reply that could not be read.') from None


def _store(neighbour, obs, now):
    NeighbourReading.objects.get_or_create(
        neighbour=neighbour, timestamp=obs['timestamp'],
        defaults={k: obs[k] for k in ('temp_c', 'humidity', 'dewpoint_c', 'qc_status')})
    meta = {k: obs[k] for k in ('label', 'latitude', 'longitude', 'elevation_m') if obs[k] not in (None, '')}
    Neighbour.objects.filter(pk=neighbour.pk).update(last_ok_at=now, last_error='', **meta)


def add(station, text, session=None, now=None):
    """Add a neighbour after checking WU knows it. Returns the Neighbour; raises
    ValueError with a message for the owner."""
    wu_id = normalise_id(text)
    if wu_id is None:
        raise ValueError('That doesn\'t look like a Weather Underground station ID (letters and digits, like KNMALBUQ123).')
    if station.neighbours.filter(wu_id=wu_id).exists():
        raise ValueError(f'{wu_id} is already on the list.')
    try:
        obs = fetch_current(wu_id, session)
    except WUError as exc:
        raise ValueError(str(exc))
    if obs is None:
        raise ValueError(f'Weather Underground has no current reading for {wu_id}. Check the ID; '
                         'a station that is offline right now can be added once it reports again.')
    now = now or timezone.now()
    neighbour = Neighbour.objects.create(station=station, wu_id=wu_id, last_attempt_at=now)
    _store(neighbour, obs, now)
    neighbour.refresh_from_db()
    return neighbour


def due(now=None):
    """Neighbours whose next poll is due (a little early is fine: the job runs every 5 minutes)."""
    now = now or timezone.now()
    cutoff = now - dt.timedelta(minutes=settings.WU_POLL_MINUTES) + dt.timedelta(seconds=30)
    # Only administrators' stations use the site's key (Station.uses_site_services).
    return list(Neighbour.objects.filter(Q(last_attempt_at__isnull=True) | Q(last_attempt_at__lte=cutoff),
                                         station__owner__is_staff=True, station__owner__is_active=True)
                .order_by('last_attempt_at', 'pk'))


def poll(now=None, session=None, sleep=time.sleep):
    """Fetch every due neighbour once. Returns the number of new readings."""
    if not settings.WU_API_KEY:
        return 0
    now = now or timezone.now()
    added = 0
    for i, neighbour in enumerate(due(now)):
        if i:
            sleep(SPACING_S)
        Neighbour.objects.filter(pk=neighbour.pk).update(last_attempt_at=now)
        try:
            obs = fetch_current(neighbour.wu_id, session)
        except WUKeyError as exc:
            log.error('Neighbour polling stopped: %s', exc)
            Neighbour.objects.filter(pk=neighbour.pk).update(last_error=str(exc)[:200])
            break
        except WUError as exc:
            log.warning('Neighbour %s: %s', neighbour.wu_id, exc)
            Neighbour.objects.filter(pk=neighbour.pk).update(last_error=str(exc)[:200])
            continue
        if obs is None:
            Neighbour.objects.filter(pk=neighbour.pk).update(
                last_error='No current reading: the station may be offline.')
            continue
        before = NeighbourReading.objects.filter(neighbour=neighbour).count()
        _store(neighbour, obs, now)
        added += NeighbourReading.objects.filter(neighbour=neighbour).count() - before
    return added


def calls_per_day(count=None):
    """How many WU requests a day the neighbours on this site use."""
    count = (Neighbour.objects.filter(station__owner__is_staff=True, station__owner__is_active=True).count()
             if count is None else count)
    return math.ceil(count * 24 * 60 / max(settings.WU_POLL_MINUTES, 1))


# ── Comparison ────────────────────────────────────────────────────────────────

def neighbour_average(values):
    """Average of the values after leaving out the highest and the lowest (when
    there are 3 or more). Returns (average, how many were averaged)."""
    values = sorted(v for v in values if v is not None)
    if len(values) >= 3:
        values = values[1:-1]
    if not values:
        return None, 0
    return statistics.fmean(values), len(values)


def distance_km(station, neighbour):
    if None in (station.latitude, station.longitude, neighbour.latitude, neighbour.longitude):
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, map(float, (station.latitude, station.longitude,
                                                          neighbour.latitude, neighbour.longitude)))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _sunny(station, when, solar):
    if solar is not None:
        return solar > SUN_UP_WM2
    if station.latitude is not None and station.longitude is not None:
        return solar_elevation(float(station.latitude), float(station.longitude), when) > 0
    return 6 <= when.astimezone(station.tzinfo).hour < 18


def _stats(deltas):
    if not deltas:
        return None
    return {'mean': statistics.fmean(deltas), 'count': len(deltas)}


def compare(station, start, end, raw=False):
    """This station against its included neighbours over [start, end). With `raw`,
    this station's temperatures are the sensor's own, before any calibration.

    Returns {'rows': [...], 'summary': {field: {all, day, night}}, 'by_hour': {field: [24 averages]},
    'neighbours': {neighbour_id: {field: (average offset from the others, count)}}}; SI units.
    Each row: {t, sunny, temp_c: (ours, neighbours' average, n averaged), humidity: (…)}.
    """
    interval = station.archive_interval_s
    neighbours = {n.pk: n for n in station.neighbours.filter(include=True)}
    buckets = defaultdict(dict)                 # interval end → {neighbour id: (temp, humidity)}
    readings = (NeighbourReading.objects
                .filter(neighbour_id__in=list(neighbours), timestamp__gt=start - dt.timedelta(seconds=interval),
                        timestamp__lt=end)
                .exclude(qc_status=-1).order_by('timestamp')
                .values_list('neighbour_id', 'timestamp', 'temp_c', 'humidity'))
    for nid, ts, temp, hum in readings:
        buckets[interval_end(ts, interval)][nid] = (temp, hum)   # the latest in an interval wins
    ours = {o['timestamp']: o for o in Observation.objects
            .filter(station=station, timestamp__gte=start, timestamp__lt=end + dt.timedelta(seconds=interval))
            .values('timestamp', 'temp_c', 'humidity', 'solar_wm2')}
    if raw:
        for ts, value in (CalibratedValue.objects
                          .filter(observation__station=station, field='temp_c', observation__timestamp__gte=start,
                                  observation__timestamp__lt=end + dt.timedelta(seconds=interval))
                          .values_list('observation__timestamp', 'value')):
            if ts in ours:
                ours[ts]['temp_c'] = value
    fields = ('temp_c', 'humidity')
    rows = []
    deltas = {f: {'all': [], 'day': [], 'night': []} for f in fields}
    hours = {f: defaultdict(list) for f in fields}
    offsets = defaultdict(lambda: defaultdict(list))
    tz = station.tzinfo
    for t in sorted(buckets):
        if not start <= t < end + dt.timedelta(seconds=interval):
            continue
        mine = ours.get(t)
        values = buckets[t]
        sunny = _sunny(station, t, mine['solar_wm2'] if mine else None)
        row = {'t': t, 'sunny': sunny}
        for i, f in enumerate(fields):
            theirs = {nid: v[i] for nid, v in values.items() if v[i] is not None}
            middle, used = neighbour_average(theirs.values())
            own = mine[f] if mine else None
            row[f] = (own, middle, used)
            if own is not None and middle is not None:
                d = own - middle
                deltas[f]['all'].append(d)
                deltas[f]['day' if sunny else 'night'].append(d)
                hours[f][(t - dt.timedelta(seconds=1)).astimezone(tz).hour].append(d)
            if len(theirs) >= 4:                # each neighbour against the (trimmed) average of the others
                for nid, v in theirs.items():
                    others, _ = neighbour_average([w for k, w in theirs.items() if k != nid])
                    offsets[nid][f].append(v - others)
        rows.append(row)
    return {
        'rows': rows,
        'summary': {f: {k: _stats(v) for k, v in deltas[f].items()} for f in fields},
        'by_hour': {f: [statistics.fmean(hours[f][h]) if hours[f][h] else None for h in range(24)] for f in fields},
        'neighbours': {nid: {f: (statistics.fmean(v), len(v)) for f, v in by_field.items()}
                       for nid, by_field in offsets.items()},
    }


# ── Suggested calibration ─────────────────────────────────────────────────────
#
# A temperature sensor's error shows best in two settled periods: at night, once
# the evening cooling has settled (from astronomical dusk, when the sun is 18°
# below the horizon) until just after sunrise, when a poorly shielded sensor
# reads its own radiative error; and while
# the sun is up, when solar heating adds to it. The evening transition is left
# out of the fit: sensors and sites cool at different rates then, which no fixed
# offset describes. The result has the shape TempCalibration uses,
# Δ = night + day·[sun up] + solar·S/1000, the same for every month.

MORNING_GRACE = dt.timedelta(minutes=15)        # after sunrise
MIN_READINGS = 36                               # of each, night and day (3 hours' worth)
NEGLIGIBLE_C = 0.3                              # smaller offsets aren't worth correcting
MIN_IMPROVEMENT = 0.1


def calibration_range(station, today=None):
    """(first, last) local dates a calibration comparison can use: from the first
    neighbour reading kept to today; None when there are none."""
    first = NeighbourReading.objects.filter(neighbour__station=station).order_by('timestamp').first()
    if first is None:
        return None
    tz = station.tzinfo
    today = today or timezone.now().astimezone(tz).date()
    return first.timestamp.astimezone(tz).date(), today


def _phase_finder(station):
    """phase(t) → 'night' | 'day' | 'evening' for an interval end t (see above)."""
    tz = station.tzinfo
    if station.latitude is None or station.longitude is None:
        def phase(t, solar):                     # no position: clock hours stand in for the sun
            hour = t.astimezone(tz).hour
            return 'day' if _sunny(station, t, solar) else 'night' if hour < 6 or hour >= 22 else 'evening'
        return phase
    lat, lon = float(station.latitude), float(station.longitude)
    cache = {}

    def times(day):
        if day not in cache:
            cache[day] = sun_times(lat, lon, day, tz)
        return cache[day]

    def phase(t, solar):
        day = t.astimezone(tz).date()
        sunrise, dusk = times(day)
        _, dusk_before = times(day - dt.timedelta(days=1))
        if sunrise is not None and t <= sunrise + MORNING_GRACE:
            return 'night' if dusk_before is None or t >= dusk_before else 'evening'
        if dusk is not None and t >= dusk:
            return 'night'
        if _sunny(station, t, solar):
            return 'day'
        return 'evening' if sunrise is not None else 'night'
    return phase


def _fit_offsets(samples):
    """samples: (phase, solar, our − neighbours °C). → {night, day, solar} °C."""
    night = [d for p, _, d in samples if p == 'night']
    day = [(s, d) for p, s, d in samples if p == 'day']
    if len(night) < MIN_READINGS or len(day) < MIN_READINGS:
        return None
    n = statistics.fmean(night)
    with_solar = [(s / 1000, d - n) for s, d in day if s is not None]
    fit = None
    if len(with_solar) >= 0.8 * len(day) and max(s for s, _ in with_solar) - min(s for s, _ in with_solar) > 0.3:
        fit = _lstsq([((1.0, s), y, 1.0) for s, y in with_solar])
    if fit is None:
        fit = (statistics.fmean(d - n for _, d in day), 0.0)
    return {'night': n, 'day': fit[0], 'solar': fit[1]}


def _predict(coef, phase, solar):
    return coef['night'] + (coef['day'] + coef['solar'] * (solar or 0.0) / 1000 if phase == 'day' else 0.0)


def suggest_calibration(station, first_day, last_day):
    """A suggested temperature calibration from the neighbours over local dates
    [first_day, last_day], from the sensor's own (uncalibrated) readings.

    Returns {'counts', 'means' (by phase), 'coefficients', 'validation', 'verdict', 'neighbours'}
    in °C; 'coefficients' is None when there are too few readings. 'verdict' is
    'recommended', 'negligible' (the sensor agrees with the neighbours) or 'weak'
    (the correction hardly explains the differences). Validation fits on alternate
    days and tests on the others.
    """
    tz = station.tzinfo
    start = dt.datetime.combine(first_day, dt.time(), tzinfo=tz).astimezone(dt.UTC)
    end = dt.datetime.combine(last_day + dt.timedelta(days=1), dt.time(), tzinfo=tz).astimezone(dt.UTC)
    result = compare(station, start, end, raw=True)
    phase = _phase_finder(station)
    solar = dict(Observation.objects.filter(station=station, timestamp__gte=start, timestamp__lte=end)
                 .values_list('timestamp', 'solar_wm2'))
    samples, days = [], []
    for row in result['rows']:
        ours, theirs, _ = row['temp_c']
        if ours is None or theirs is None:
            continue
        s = solar.get(row['t'])
        samples.append((phase(row['t'], s), s, ours - theirs))
        days.append((row['t'] - dt.timedelta(seconds=1)).astimezone(tz).toordinal() % 2)
    counts = {p: sum(1 for q, _, _ in samples if q == p) for p in ('night', 'day', 'evening')}
    means = {p: statistics.fmean(d for q, _, d in samples if q == p) if counts[p] else None for p in counts}
    out = {'counts': counts, 'means': means, 'coefficients': None, 'validation': None, 'verdict': None,
           'neighbours': NeighbourReading.objects.filter(
               neighbour__station=station, neighbour__include=True, timestamp__gte=start, timestamp__lt=end)
           .values('neighbour').distinct().count()}
    coef = _fit_offsets(samples)
    if coef is None:
        return out
    fitted = [s for s in samples if s[0] != 'evening']
    folds = [d for s, d in zip(samples, days) if s[0] != 'evening']
    before, after = [], []
    for fold in (0, 1):
        train = _fit_offsets([s for s, f in zip(fitted, folds) if f != fold])
        if train is None:
            continue
        for (p, s, d), f in zip(fitted, folds):
            if f == fold:
                before.append(d)
                after.append(d - _predict(train, p, s))
    if not before:                               # too few days to hold any back: test on what was fitted
        before = [d for _, _, d in fitted]
        after = [d - _predict(coef, p, s) for p, s, d in fitted]
    rms = lambda v: math.sqrt(sum(e * e for e in v) / len(v))
    validation = {'readings': len(before), 'before': rms(before), 'after': rms(after),
                  'bias_before': statistics.fmean(before), 'bias_after': statistics.fmean(after)}
    validation['improvement'] = 1 - validation['after'] / validation['before'] if validation['before'] else 0.0
    sunny_noon = coef['night'] + coef['day'] + coef['solar'] * 0.9
    if max(abs(coef['night']), abs(sunny_noon)) < NEGLIGIBLE_C:
        verdict = 'negligible'
    elif validation['improvement'] < MIN_IMPROVEMENT:
        verdict = 'weak'
    else:
        verdict = 'recommended'
    out.update(coefficients=coef, validation=validation, verdict=verdict)
    return out


def live(station, now=None):
    """The included neighbours' latest readings (from the last 30 minutes), as
    {'temp_c', 'humidity', 'n', 'newest'}; None when there are none."""
    now = now or timezone.now()
    latest = {}
    for r in (NeighbourReading.objects
              .filter(neighbour__station=station, neighbour__include=True, timestamp__gte=now - LIVE_WINDOW)
              .exclude(qc_status=-1).order_by('-timestamp')):
        latest.setdefault(r.neighbour_id, r)
    if not latest:
        return None
    temp, n_temp = neighbour_average([r.temp_c for r in latest.values()])
    hum, n_hum = neighbour_average([r.humidity for r in latest.values()])
    return {'temp_c': temp, 'humidity': hum, 'n': max(n_temp, n_hum), 'reporting': len(latest),
            'newest': max(r.timestamp for r in latest.values())}
