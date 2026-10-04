"""Almanac: on this day, records, frost dates. All values SI; templates convert.

Coverage rules (see also charts.rain_known):
  * A measured extreme is real however incomplete its day was, so "highest
    temperature" or "strongest gust" use every day.
  * Statistics that describe a whole day — coldest day (lowest daily high),
    warmest night (highest daily low), normals — need DAY_COVERAGE of
    temperature data, or a three-hour fragment could set them.
  * Frost dates are marked uncertain when missing data could hide a frost.
"""
import datetime as dt
from dataclasses import dataclass, field

from django.db.models import Count, Sum
from django.db.models.functions import Coalesce, TruncMonth

from .charts import rain_known
from .models import DailyRollup, Observation

DAY_COVERAGE = 0.9
NORMAL_WINDOW_DAYS = 3          # normals average ±3 days around the date in every year
FROST_C = 0.0                   # 32 °F
HARD_FREEZE_C = -2.2            # 28 °F
FROST_MARGIN_C = 5.0            # an incomplete day this close to the threshold could hide a frost
MEASURABLE_RAIN_MM = 0.254      # 0.01 in, the gauge's resolution


@dataclass
class Record:
    key: str
    label: str
    kind: str                     # temp | rain | rate | speed | pressure | uv | days | pm | ppm | strikes
    value: float
    date: dt.date
    end_date: dt.date = None      # for spans (wettest month, dry spell)
    time: dt.datetime = None      # when the extreme was observed, if known
    note: str = ''
    sensor: str = ''              # extra sensor's upload key (sensor records only)


def _local_day_bounds(station, day):
    tz = station.tzinfo
    start = dt.datetime.combine(day, dt.time(), tzinfo=tz).astimezone(dt.UTC)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=tz).astimezone(dt.UTC)
    return start, end


def _time_of(station, day, field, lowest=False, own=None):
    """When on `day` the extreme of `field` was observed (end of that interval)."""
    start, end = _local_day_bounds(station, day)
    expr = Coalesce(own, field) if own else None
    qs = Observation.objects.filter(station=station, timestamp__gt=start, timestamp__lte=end)
    if expr is not None:
        qs = qs.annotate(_v=expr).exclude(_v__isnull=True).order_by('_v' if lowest else '-_v')
    else:
        qs = qs.exclude(**{f'{field}__isnull': True}).order_by(field if lowest else f'-{field}')
    return qs.values_list('timestamp', flat=True).first()


def _apparent_time(station, day, kind, lowest):
    """When on `day` the lowest wind chill / highest heat index happened. It's a
    formula of three columns, so the day's readings are scanned in Python."""
    from .units import apparent
    start, end = _local_day_bounds(station, day)
    best = None
    for ts, t, h, w in (Observation.objects.filter(station=station, timestamp__gt=start, timestamp__lte=end)
                        .values_list('timestamp', 'temp_c', 'humidity', 'wind_speed_ms')):
        feels, k = apparent(t, h, w)
        if k == kind and (best is None or (feels < best[0] if lowest else feels > best[0])):
            best = (feels, ts)
    return best[1] if best else None


def _extreme(qs, field, lowest=False):
    row = qs.exclude(**{f'{field}__isnull': True}).order_by(field if lowest else f'-{field}', 'date').first()
    return (row, getattr(row, field)) if row else (None, None)


def records(station, year=None):
    """Records over all time, or one calendar year. Returns a list of Record."""
    days = DailyRollup.objects.filter(station=station)
    if year:
        days = days.filter(date__year=year)
    full = days.filter(temp_coverage__gte=DAY_COVERAGE)
    out = []

    def add(key, label, kind, qs, field, lowest=False, time_field=None, own=None, note=''):
        row, value = _extreme(qs, field, lowest)
        if row is None:
            return
        when = _time_of(station, row.date, time_field, lowest, own) if time_field else None
        out.append(Record(key, label, kind, value, row.date, time=when, note=note))

    add('high', 'Highest temperature', 'temp', days, 'temp_max_c', time_field='temp_c', own='temp_max_c')
    add('low', 'Lowest temperature', 'temp', days, 'temp_min_c', lowest=True, time_field='temp_c', own='temp_min_c')
    add('warm_night', 'Warmest night', 'temp', full, 'temp_min_c', note='Highest daily low')
    add('cold_day', 'Coldest day', 'temp', full, 'temp_max_c', lowest=True, note='Lowest daily high')
    from .units import HEAT_INDEX, WIND_CHILL
    for key, label, field, kind, lowest, note in (
            ('wind_chill', 'Lowest wind chill', 'windchill_min_c', WIND_CHILL, True, 'How cold it felt in the wind'),
            ('heat_index', 'Highest heat index', 'heatindex_max_c', HEAT_INDEX, False, 'How hot it felt with the humidity')):
        row, value = _extreme(days, field, lowest)
        if row is not None:
            out.append(Record(key, label, 'temp', value, row.date, note=note,
                              time=_apparent_time(station, row.date, kind, lowest)))
    add('wet_day', 'Wettest day', 'rain', days, 'rain_mm')
    add('rate', 'Heaviest rain rate', 'rate', days, 'rain_rate_max_mmh', time_field='rain_rate_mmh')
    add('gust', 'Strongest gust', 'speed', days, 'wind_gust_max_ms', time_field='wind_gust_ms')
    add('p_high', 'Highest pressure', 'pressure', days, 'pressure_max_hpa', time_field='pressure_rel_hpa', own='pressure_max_hpa')
    add('p_low', 'Lowest pressure', 'pressure', days, 'pressure_min_hpa', lowest=True, time_field='pressure_rel_hpa', own='pressure_min_hpa')
    add('uv', 'Highest UV index', 'uv', days, 'uv_max', time_field='uv_index', own='uv_max')

    month = (days.annotate(m=TruncMonth('date')).values('m').annotate(total=Sum('rain_mm'), n=Count('id'))
             .exclude(total__isnull=True).order_by('-total').first())
    if month and month['total']:
        first = month['m']
        last = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        out.append(Record('wet_month', 'Wettest month', 'rain', month['total'], first, end_date=last,
                          note=first.strftime('%B %Y')))

    spell = longest_dry_spell(days)
    if spell:
        out.append(Record('dry_spell', 'Longest dry spell', 'days', spell[0], spell[1], end_date=spell[2],
                          note='Days in a row without measurable rain'))
    return out


def longest_dry_spell(days_qs):
    """(length, first day, last day) of the longest run of consecutive days known to
    be dry. A day without trustworthy rain data ends the run — an outage is not a
    drought."""
    best, run_start, run_len, prev = None, None, 0, None
    for day, rain, cov in days_qs.order_by('date').values_list('date', 'rain_mm', 'rain_coverage'):
        dry = rain_known(rain, cov, DAY_COVERAGE) and rain < MEASURABLE_RAIN_MM
        contiguous = prev is not None and day == prev + dt.timedelta(days=1)
        if dry and contiguous and run_len:
            run_len += 1
        elif dry:
            run_start, run_len = day, 1
        else:
            run_len = 0
        if run_len and (best is None or run_len > best[0]):
            best = (run_len, run_start, day)
        prev = day
    return best


@dataclass
class DayInYear:
    year: int
    high_c: float = None
    low_c: float = None
    rain_mm: float = None
    gust_ms: float = None
    partial: bool = False


@dataclass
class OnThisDay:
    month: int
    day: int
    years: list = field(default_factory=list)
    normal_high_c: float = None
    normal_low_c: float = None
    normal_years: int = 0
    record_high: DayInYear = None
    record_low: DayInYear = None
    rain_years: int = 0


def on_this_day(station, month, day):
    rows = DailyRollup.objects.filter(station=station, date__month=month, date__day=day).order_by('-date')
    result = OnThisDay(month, day)
    for r in rows:
        result.years.append(DayInYear(
            r.date.year, r.temp_max_c, r.temp_min_c,
            r.rain_mm if rain_known(r.rain_mm, r.rain_coverage, DAY_COVERAGE) else None,
            r.wind_gust_max_ms, partial=r.temp_coverage < DAY_COVERAGE))
    with_high = [y for y in result.years if y.high_c is not None]
    with_low = [y for y in result.years if y.low_c is not None]
    if with_high:
        result.record_high = max(with_high, key=lambda y: y.high_c)
    if with_low:
        result.record_low = min(with_low, key=lambda y: y.low_c)
    result.rain_years = sum(1 for y in result.years if y.rain_mm is not None and y.rain_mm >= MEASURABLE_RAIN_MM)

    # Normals from the station's own history: every full day within ±window of this date, every year.
    highs, lows, years = [], [], set()
    for year in {r.date.year for r in DailyRollup.objects.filter(station=station).only('date')}:
        try:
            centre = dt.date(year, month, day)
        except ValueError:          # Feb 29 in a common year
            centre = dt.date(year, 2, 28)
        window = DailyRollup.objects.filter(
            station=station, temp_coverage__gte=DAY_COVERAGE,
            date__gte=centre - dt.timedelta(days=NORMAL_WINDOW_DAYS), date__lte=centre + dt.timedelta(days=NORMAL_WINDOW_DAYS))
        vals = list(window.values_list('temp_max_c', 'temp_min_c'))
        if vals:
            years.add(year)
            highs += [h for h, _ in vals if h is not None]
            lows += [lo for _, lo in vals if lo is not None]
    if highs:
        result.normal_high_c = sum(highs) / len(highs)
    if lows:
        result.normal_low_c = sum(lows) / len(lows)
    result.normal_years = len(years)
    return result


@dataclass
class FrostSeason:
    year: int
    last_spring: dt.date = None
    first_fall: dt.date = None
    spring_uncertain: bool = False
    fall_uncertain: bool = False
    season_days: int = None


def frost_dates(station, threshold_c=FROST_C):
    """Last spring and first fall frost per year (southern hemisphere: the season
    runs July → June, so 'spring' is Sep–Dec and 'fall' Mar–Jun of the next year).

    A date is uncertain when a day that could have hidden a frost sits where one
    would change the answer: after the last spring frost up to midsummer, or
    between midsummer and the first fall frost. A day can hide a frost if it is
    missing, or if it is incomplete and its readings have a gap in the hours before
    dawn when frost forms — unless at least half the day was recorded and its low
    stayed more than FROST_MARGIN_C above the threshold.
    """
    south = station.latitude is not None and station.latitude < 0
    days = list(DailyRollup.objects.filter(station=station).order_by('date')
                .values_list('date', 'temp_min_c', 'temp_coverage'))
    if not days:
        return []
    by_date = {d: (t, c) for d, t, c in days}
    first_day, last_day = days[0][0], days[-1][0]
    seasons = []
    for year in range(first_day.year, last_day.year + 1):
        if south:
            spring_lo, mid, fall_hi = dt.date(year, 7, 1), dt.date(year + 1, 1, 1), dt.date(year + 1, 6, 30)
        else:
            spring_lo, mid, fall_hi = dt.date(year, 1, 1), dt.date(year, 7, 1), dt.date(year, 12, 31)
        if spring_lo > last_day or fall_hi < first_day:
            continue
        season = FrostSeason(year)

        def known(d):
            t, c = by_date.get(d, (None, 0.0))
            if t is None:
                return False
            if c >= DAY_COVERAGE or (c >= 0.5 and t > threshold_c + FROST_MARGIN_C):
                return True
            return _night_covered(station, d)     # the hours that matter were recorded

        def frost(d):
            t, _ = by_date.get(d, (None, 0.0))
            return t is not None and t <= threshold_c

        d = spring_lo
        while d < mid:
            if frost(d):
                season.last_spring = d
            d += dt.timedelta(days=1)
        check_from = (season.last_spring + dt.timedelta(days=1)) if season.last_spring else spring_lo
        season.spring_uncertain = any(not known(check_from + dt.timedelta(days=i)) for i in range((mid - check_from).days))

        d = mid
        while d <= fall_hi:
            if frost(d):
                season.first_fall = d
                break
            d += dt.timedelta(days=1)
        check_to = season.first_fall or min(fall_hi, last_day)
        season.fall_uncertain = any(not known(mid + dt.timedelta(days=i)) for i in range((check_to - mid).days))
        if season.first_fall is None and last_day < fall_hi:
            season.fall_uncertain = True        # season still in progress

        if season.last_spring and season.first_fall:
            season.season_days = (season.first_fall - season.last_spring).days - 1
        seasons.append(season)
    return seasons


NIGHT_END_HOUR = 10               # local; frost forms overnight and around dawn
MAX_NIGHT_GAP = dt.timedelta(minutes=30)


def _night_covered(station, day):
    """True if the station reported without a gap over MAX_NIGHT_GAP from local
    midnight to NIGHT_END_HOUR on `day`."""
    tz = station.tzinfo
    start = dt.datetime.combine(day, dt.time(), tzinfo=tz).astimezone(dt.UTC)
    end = dt.datetime.combine(day, dt.time(NIGHT_END_HOUR), tzinfo=tz).astimezone(dt.UTC)
    stamps = list(Observation.objects.filter(station=station, temp_c__isnull=False, timestamp__gte=start,
                                             timestamp__lte=end + MAX_NIGHT_GAP)
                  .order_by('timestamp').values_list('timestamp', 'interval_s'))
    cursor = start
    for ts, interval in stamps:
        if ts - dt.timedelta(seconds=interval) - cursor > MAX_NIGHT_GAP:
            return False
        cursor = max(cursor, ts)
    return end - cursor <= MAX_NIGHT_GAP


def frost_summary(seasons):
    """Average and range of the certain spring/fall dates, as day-of-year → a date in 2001."""
    def stats(dates):
        if not dates:
            return None
        doys = [(d - dt.date(d.year, 1, 1)).days for d in dates]
        base = dt.date(2001, 1, 1)
        return {'average': base + dt.timedelta(days=round(sum(doys) / len(doys))),
                'earliest': min(dates, key=lambda d: (d - dt.date(d.year, 1, 1)).days),
                'latest': max(dates, key=lambda d: (d - dt.date(d.year, 1, 1)).days),
                'years': len(dates)}
    return {
        'spring': stats([s.last_spring for s in seasons if s.last_spring and not s.spring_uncertain]),
        'fall': stats([s.first_fall for s in seasons if s.first_fall and not s.fall_uncertain]),
    }


# Extra-sensor records, per sensor kind: (key suffix, label, record kind, rollup index, lowest).
# Rollup extra values are [mean, min, max] per day (weather.rollups).
_SENSOR_RECORDS = {
    'temperature': [('high', 'highest', 'temp', 2, False), ('low', 'lowest', 'temp', 1, True)],
    'soil_temp': [('high', 'highest', 'temp', 2, False), ('low', 'lowest', 'temp', 1, True)],
    'pm25': [('high', 'highest', 'pm', 2, False)],
    'pm10': [('high', 'highest', 'pm', 2, False)],
    'co2': [('high', 'highest', 'ppm', 2, False)],
    'lightning': [('day', 'most strikes in a day', 'strikes', 2, False)],
}


def _sensor_time(station, day, key, lowest):
    """When on `day` the sensor's extreme reading was (scans that day's readings)."""
    from .sensors import describe
    sensor = describe(key)
    start, end = _local_day_bounds(station, day)
    best = None
    for ts, extra in (Observation.objects.filter(station=station, timestamp__gt=start, timestamp__lte=end,
                                                 extra__has_key=key).values_list('timestamp', 'extra')):
        value = sensor.si(extra.get(key)) if sensor else None
        if value is not None and (best is None or (value < best[0] if lowest else value > best[0])):
            best = (value, ts)
    return best[1] if best else None


def sensor_records(station, year=None, include_private=False):
    """Records for the station's extra sensors the viewer may see, from the daily summaries."""
    from .sensors import station_sensors
    sensors = [(s, n) for s, n in station_sensors(station, include_private) if s.kind in _SENSOR_RECORDS]
    if not sensors:
        return []
    days = DailyRollup.objects.filter(station=station).exclude(extra={})
    if year:
        days = days.filter(date__year=year)
    days = list(days.values_list('date', 'extra'))
    out = []
    for sensor, name in sensors:
        for suffix, label, kind, index, lowest in _SENSOR_RECORDS[sensor.kind]:
            best = None
            for date, extra in days:
                triple = extra.get(sensor.key)
                if not triple or triple[index] is None:
                    continue
                if best is None or (triple[index] < best[1] if lowest else triple[index] > best[1]):
                    best = (date, triple[index])
            if best is None:
                continue
            when = None if kind == 'strikes' else _sensor_time(station, best[0], sensor.key, lowest)
            out.append(Record(f'x:{sensor.key}:{suffix}', f'{name}: {label}', kind, best[1], best[0], time=when,
                              note=sensor.kind_label, sensor=sensor.key))
    return out
