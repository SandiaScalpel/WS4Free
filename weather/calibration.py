"""Temperature calibration: correct a sensor that reads wrong in a known way.

A poorly shielded sensor runs warm in sunshine and cold on clear nights. The
correction subtracts

    Δ = night + day·[sun up] + solar · S/1000          (°C; S in W/m²)

with coefficients per calendar month (the error changes with the seasons: a sensor
that reads warm by day in summer may read cold on clear winter nights).
"Sun up" comes from the solar reading (S > 5 W/m²), or from the sun's elevation
at the station when there is no reading.

Reference mode fits the coefficients by comparing the station hour by hour
with a nearby airport (ASOS) station from the Iowa Environmental Mesonet. A
baseline period with a trusted sensor gives the site's natural difference from
the reference for each month and hour; what is left in the affected period is
the sensor's error. Validation fits on alternate weeks and tests on the others.

Applying keeps every original value (CalibratedValue), recalculates dew point
from the corrected temperature, marks rollups dirty, and is fully reversible —
the same pattern as data-quality exclusions, with which it cooperates:
excluded (NULL) readings are skipped, and removing an exclusion re-applies the
calibration to the readings it restores.
"""
import csv
import datetime as dt
import io
import logging
import math
import subprocess
import sys
import time

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q

from . import units
from .ingest.store import interval_end, mark_dirty
from .models import CalibratedValue, ExcludedValue, Observation, TempCalibration

log = logging.getLogger(__name__)

TEMP_FIELDS = ('temp_c', 'temp_min_c', 'temp_max_c')
FAR_FUTURE = dt.datetime(9999, 1, 1, tzinfo=dt.UTC)
ACTIVE = ('pending', 'applied')
SUN_UP_WM2 = 5.0
BATCH = 2000


# ── The correction ────────────────────────────────────────────────────────────

def solar_elevation(lat, lon, when):
    """Sun elevation in degrees (NOAA general solar position approximation)."""
    when = when.astimezone(dt.UTC)
    doy = when.timetuple().tm_yday
    hours = when.hour + when.minute / 60 + when.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hours - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    tst = hours * 60 + eqtime + 4 * lon
    ha = math.radians(tst / 4 - 180)
    phi = math.radians(lat)
    cos_zen = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zen))))


def coefficients_for(calibration, month):
    c = calibration.coefficients.get(str(month)) or {}
    return c.get('night', 0.0), c.get('day', 0.0), c.get('solar', 0.0)


def offset_c(calibration, station, when, solar):
    """Δ (°C) to subtract from a reading taken at `when` with solar radiation `solar`."""
    night, day, slope = coefficients_for(calibration, when.astimezone(station.tzinfo).month)
    if solar is not None:
        sunny = solar > SUN_UP_WM2
    elif station.latitude is not None and station.longitude is not None:
        sunny = solar_elevation(float(station.latitude), float(station.longitude), when) > 0
    else:
        sunny = 6 <= when.astimezone(station.tzinfo).hour < 18
    return night + (day if sunny else 0.0) + slope * (solar or 0.0) / 1000


def _window(station, start, end):
    return Observation.objects.filter(station=station, timestamp__gt=start, timestamp__lte=end or FAR_FUTURE)


def _correct(calibration, rows, fields=TEMP_FIELDS):
    """Correct `fields` (and the dew point that depends on them) on rows not yet
    corrected for those fields. Returns the number of rows changed."""
    station = calibration.station
    changed = 0
    last_pk = 0
    while True:
        chunk = list(rows.filter(pk__gt=last_pk).order_by('pk')
                     .only('pk', 'timestamp', 'solar_wm2', 'humidity', 'dewpoint_c', *TEMP_FIELDS)[:BATCH])
        if not chunk:
            break
        last_pk = chunk[-1].pk
        done = set(CalibratedValue.objects.filter(observation__in=chunk).values_list('observation_id', 'field'))
        backups, updated = [], []
        for obs in chunk:
            delta = offset_c(calibration, station, obs.timestamp, obs.solar_wm2)
            touched = False
            for f in fields:
                value = getattr(obs, f)
                if value is None or (obs.pk, f) in done:
                    continue
                backups.append(CalibratedValue(observation_id=obs.pk, calibration=calibration, field=f, value=value))
                setattr(obs, f, value - delta)
                touched = True
            if touched and obs.dewpoint_c is not None and (obs.pk, 'dewpoint_c') not in done:
                if obs.humidity is not None and obs.temp_c is not None:
                    backups.append(CalibratedValue(observation_id=obs.pk, calibration=calibration,
                                                   field='dewpoint_c', value=obs.dewpoint_c))
                    obs.dewpoint_c = units.dewpoint_c(obs.temp_c, obs.humidity)
            if touched:
                updated.append(obs)
        CalibratedValue.objects.bulk_create(backups, ignore_conflicts=True)
        if updated:
            Observation.objects.bulk_update(updated, [*TEMP_FIELDS, 'dewpoint_c'])
            changed += len(updated)
    return changed


def apply_calibration(calibration):
    with transaction.atomic():
        changed = _correct(calibration, _window(calibration.station, calibration.start, calibration.end))
        calibration.readings = changed
        calibration.status = 'applied'
        calibration.save(update_fields=['readings', 'status'])
    mark_dirty(calibration.station_id, calibration.start)
    return changed


def remove_calibration(calibration):
    """Put the original values back and delete the calibration. Where a data-quality
    exclusion has since set a corrected value aside, the exclusion's copy is
    replaced with the original instead, so the reading stays excluded."""
    restored = 0
    station_id, start = calibration.station_id, calibration.start
    with transaction.atomic():
        last_pk = 0
        while True:
            chunk = list(calibration.originals.filter(pk__gt=last_pk).order_by('pk')
                         .values_list('pk', 'observation_id', 'field', 'value')[:BATCH])
            if not chunk:
                break
            last_pk = chunk[-1][0]
            excluded = set(ExcludedValue.objects.filter(observation_id__in={c[1] for c in chunk})
                           .values_list('observation_id', 'field'))
            by_field = {}
            for _, obs_id, field, value in chunk:
                if (obs_id, field) in excluded:
                    ExcludedValue.objects.filter(observation_id=obs_id, field=field).update(value=value)
                else:
                    by_field.setdefault(field, []).append(Observation(pk=obs_id, **{field: value}))
            for field, objs in by_field.items():
                Observation.objects.bulk_update(objs, [field])
            restored += len(chunk)
        calibration.delete()           # cascades the (now restored) CalibratedValue rows
    mark_dirty(station_id, start)
    return restored


def _active(station, start, end):
    return TempCalibration.objects.filter(station=station, status__in=ACTIVE, start__lt=end).filter(
        Q(end__isnull=True) | Q(end__gt=start))


def apply_to_new_rows(station, start, end, written_fields=None):
    """Ingest hook, after exclusions. Rows in (start, end] were just written; when a
    push merged into an existing row, `written_fields` are the columns it overwrote
    with raw values, whose old backups are therefore stale."""
    for calibration in _active(station, start, end):
        lo, hi = max(start, calibration.start), min(end, calibration.end or end)
        rows = _window(station, lo, hi)
        if written_fields:
            stale = [f for f in (*TEMP_FIELDS, 'dewpoint_c') if f in written_fields]
            CalibratedValue.objects.filter(observation__in=rows, field__in=stale).delete()
        _correct(calibration, rows)


def reapply_window(station, start, end):
    """After an exclusion restores raw readings in (start, end], correct them again."""
    for calibration in _active(station, start, end):
        lo, hi = max(start, calibration.start), min(end or FAR_FUTURE, calibration.end or FAR_FUTURE)
        _correct(calibration, _window(station, lo, hi))


def correct_live(station, data, when):
    """Correct a LatestReading's data dict (a copy) for an active calibration."""
    if data.get('temp_c') is None:
        return data
    calibration = _active(station, when - dt.timedelta(seconds=1), when).first()
    if calibration is None:
        return data
    delta = offset_c(calibration, station, when, data.get('solar_wm2'))
    data = dict(data, temp_c=data['temp_c'] - delta)
    if data.get('humidity') is not None:
        data['dewpoint_c'] = units.dewpoint_c(data['temp_c'], data['humidity'])
    return data


def overlapping(station, start, end):
    """Applied calibrations overlapping [start, end] (for "corrected" labels)."""
    return list(TempCalibration.objects.filter(station=station, status='applied', start__lt=end)
                .filter(Q(end__isnull=True) | Q(end__gt=start)).order_by('start'))


def launch(calibration, action):
    """Fit, apply or remove in a detached process (see weather.quality.launch)."""
    log_file = open(settings.BASE_DIR / 'calibration.log', 'ab')
    subprocess.Popen([sys.executable, str(settings.BASE_DIR / 'manage.py'), 'run_calibration', str(calibration.pk), action],
                     cwd=settings.BASE_DIR, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)


# ── Fitting against a reference station ──────────────────────────────────────

IEM_URL = 'https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py'


class FitError(RuntimeError):
    pass


def _months(start, end):
    d = start.replace(day=1)
    while d <= end:
        nxt = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        yield max(d, start), min(nxt - dt.timedelta(days=1), end)
        d = nxt


def fetch_reference(code, start, end, session=None, sleep=time.sleep):
    """Hourly routine reports {UTC datetime: °C} from IEM for [start, end], one month
    per request with retries (the service answers 503 under load)."""
    session = session or requests.Session()
    out = {}
    for lo, hi in _months(start, end):
        params = {'station': code, 'data': 'tmpf', 'tz': 'Etc/UTC', 'format': 'onlycomma', 'latlon': 'no',
                  'missing': 'empty', 'report_type': '3',
                  'year1': lo.year, 'month1': lo.month, 'day1': lo.day,
                  'year2': (hi + dt.timedelta(days=1)).year, 'month2': (hi + dt.timedelta(days=1)).month,
                  'day2': (hi + dt.timedelta(days=1)).day}
        for attempt in range(5):
            try:
                response = session.get(IEM_URL, params=params, timeout=60)
            except requests.RequestException as exc:
                response, error = None, str(exc)
            else:
                error = f'HTTP {response.status_code}'
                if response.status_code == 200:
                    break
            if attempt == 4:
                raise FitError(f'Reference data for {lo:%Y-%m} could not be fetched ({error}).')
            sleep(5 * 2 ** attempt)
        for row in csv.DictReader(io.StringIO(response.text)):
            if row.get('tmpf'):
                when = dt.datetime.strptime(row['valid'], '%Y-%m-%d %H:%M').replace(tzinfo=dt.UTC)
                out[when] = units.f_to_c(float(row['tmpf']))
        sleep(1.0)
    return out


def _pairs(station, reference):
    """(reference time, station °C, station solar, local datetime, ref °C) for every
    reference report that has a station reading in the same archive interval."""
    by_end = {}
    for when, ref_c in reference.items():
        by_end.setdefault(interval_end(when, station.archive_interval_s), (when, ref_c))
    keys = list(by_end)
    out = []
    for i in range(0, len(keys), BATCH):
        for ts, temp, solar in (Observation.objects.filter(station=station, timestamp__in=keys[i:i + BATCH])
                                .exclude(temp_c__isnull=True).values_list('timestamp', 'temp_c', 'solar_wm2')):
            when, ref_c = by_end[ts]
            out.append((when, temp, solar, when.astimezone(station.tzinfo), ref_c))
    return out


def _lstsq(rows):
    """Weighted least squares for y ≈ X·b, rows = (x tuple, y, w). Tiny normal equations."""
    k = len(rows[0][0])
    a = [[sum(w * x[i] * x[j] for x, _, w in rows) for j in range(k)] for i in range(k)]
    b = [sum(w * x[i] * y for x, y, w in rows) for i in range(k)]
    for i in range(k):
        p = max(range(i, k), key=lambda r: abs(a[r][i]))
        a[i], a[p], b[i], b[p] = a[p], a[i], b[p], b[i]
        if abs(a[i][i]) < 1e-12:
            return None
        for r in range(k):
            if r != i:
                f = a[r][i] / a[i][i]
                a[r] = [x - f * y for x, y in zip(a[r], a[i])]
                b[r] -= f * b[i]
    return [b[i] / a[i][i] for i in range(k)]


def _features(solar, sunny):
    return (1.0, 1.0 if sunny else 0.0, (solar or 0.0) / 1000)


def _fit_months(samples):
    """samples: (month, x, excess). Coefficients per month from that month and its
    neighbours (weights 2/1/1); months with too little data use the pooled fit."""
    pooled = _lstsq([(x, y, 1.0) for _, x, y in samples])
    if pooled is None:
        raise FitError('Not enough daytime and night-time readings to fit a correction.')
    coefficients = {}
    for m in range(1, 13):
        near = {m: 2.0, (m - 2) % 12 + 1: 1.0, m % 12 + 1: 1.0}
        rows = [(x, y, near[mm]) for mm, x, y in samples if mm in near]
        own = sum(1 for mm, _, _ in samples if mm == m)
        fit = _lstsq(rows) if own >= 30 and len(rows) >= 90 else None
        night, day, slope = fit or pooled
        coefficients[str(m)] = {'night': round(night, 3), 'day': round(day, 3), 'solar': round(slope, 3),
                                'hours': own, 'pooled': fit is None}
    return coefficients


def _predict(coefficients, month, x):
    c = coefficients[str(month)]
    return c['night'] * x[0] + c['day'] * x[1] + c['solar'] * x[2]


def fit_calibration(calibration, fetch=fetch_reference):
    """Fit a reference-mode calibration's monthly coefficients and validation."""
    station = calibration.station
    if not calibration.baseline_start or not calibration.baseline_end:
        raise FitError('A baseline period (when the sensor was trusted) is required.')
    tz = station.tzinfo
    affected_start = calibration.start.astimezone(tz).date()
    affected_end = (calibration.end or dt.datetime.now(dt.UTC)).astimezone(tz).date()

    base_ref = fetch(calibration.reference_station, calibration.baseline_start, calibration.baseline_end)
    if not base_ref:
        raise FitError(f'No reports from {calibration.reference_station} for the baseline period. '
                       'Check the station identifier (e.g. DEN, ORD, KSEA → SEA).')
    affected_ref = fetch(calibration.reference_station, affected_start, affected_end)
    base = _pairs(station, base_ref)
    affected = _pairs(station, affected_ref)
    if len(base) < 500 or len(affected) < 500:
        raise FitError(f'Too few matching hours (baseline {len(base)}, affected {len(affected)}); '
                       'the station may have been offline, or readings excluded.')

    # The site's own difference from the reference, by month and hour of day.
    cells = {}
    for _, temp, _, local, ref_c in base:
        cells.setdefault((local.month, local.hour), []).append(temp - ref_c)
    by_hour = {}
    for (m, h), v in cells.items():
        by_hour.setdefault(h, []).extend(v)
    profile = {k: sum(v) / len(v) for k, v in cells.items() if len(v) >= 5}
    hour_only = {h: sum(v) / len(v) for h, v in by_hour.items()}

    def baseline(month, hour):
        for m in (month, (month - 2) % 12 + 1, month % 12 + 1):
            if (m, hour) in profile:
                return profile[(m, hour)]
        return hour_only.get(hour)

    lat = float(station.latitude) if station.latitude is not None else None
    lon = float(station.longitude) if station.longitude is not None else None
    samples, weeks = [], []
    for when, temp, solar, local, ref_c in affected:
        b = baseline(local.month, local.hour)
        if b is None:
            continue
        sunny = solar > SUN_UP_WM2 if solar is not None else (
            solar_elevation(lat, lon, when) > 0 if lat is not None else 6 <= local.hour < 18)
        samples.append((local.month, _features(solar, sunny), temp - ref_c - b))
        weeks.append(local.isocalendar().week % 2)
    if len(samples) < 500:
        raise FitError('Too few comparable hours after aligning with the baseline.')

    coefficients = _fit_months(samples)

    # Validation: fit on even weeks, test on odd ones, and the reverse.
    errors_before, errors_after, by_season = [], [], {}
    for fold in (0, 1):
        train = [s for s, w in zip(samples, weeks) if w != fold]
        test = [s for s, w in zip(samples, weeks) if w == fold]
        if not train or not test:
            continue
        fold_coef = _fit_months(train)
        for month, x, y in test:
            after = y - _predict(fold_coef, month, x)
            errors_before.append(y)
            errors_after.append(after)
            season = 'summer' if month in (6, 7, 8) else 'winter' if month in (12, 1, 2) else 'spring/fall'
            by_season.setdefault(season, ([], []))
            by_season[season][0].append(y)
            by_season[season][1].append(after)
    rms = lambda v: math.sqrt(sum(e * e for e in v) / len(v)) if v else None
    floor = rms([temp - ref_c - baseline(local.month, local.hour) for _, temp, _, local, ref_c in base
                 if baseline(local.month, local.hour) is not None])
    calibration.coefficients = coefficients
    calibration.validation = {
        'hours': len(samples), 'baseline_hours': len(base),
        'before': rms(errors_before), 'after': rms(errors_after), 'floor': floor,
        'improvement': 1 - rms(errors_after) / rms(errors_before) if rms(errors_before) else 0.0,
        'bias_before': sum(errors_before) / len(errors_before), 'bias_after': sum(errors_after) / len(errors_after),
        'seasons': {k: {'hours': len(v[0]), 'before': rms(v[0]), 'after': rms(v[1])} for k, v in by_season.items()},
    }
    calibration.status = 'fitted'
    calibration.message = ''
    calibration.save(update_fields=['coefficients', 'validation', 'status', 'message'])
    return calibration
