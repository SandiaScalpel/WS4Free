"""Report builder: daily rollups summarised by day, month or year, in the viewer's units.

Conventions follow the US National Weather Service monthly climate summaries
(the "NOAA reports" WeeWX users know): degree days from (high + low) / 2 against
65 °F (18 °C in metric), hot days ≥ 90 °F (32 °C), freeze days with a low
≤ 32 °F (0 °C), rain days ≥ 0.01 in (0.25 mm).

Coverage: means, degree days and day counts use only days with ≥ 90 % of their
temperature data; extremes and rain use every measured value (see almanac); the
"days with data" column shows how complete each row is.
"""
import calendar
import csv
import datetime as dt
import io
from dataclasses import dataclass, field

from . import agro
from .almanac import DAY_COVERAGE, MEASURABLE_RAIN_MM
from .charts import rain_known
from .models import DailyRollup


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    quantity: str          # temp | temp_delta | rain | rate | speed | pressure | pct | count | wm2 | uv | pm | ppm | cb
    daily: bool = True     # meaningful in a day-by-day report
    help: str = ''


COLUMNS = [
    Column('temp_high', 'High', 'temp'),
    Column('temp_low', 'Low', 'temp'),
    Column('temp_mean', 'Mean', 'temp', help='Time-weighted mean of all readings'),
    Column('temp_avg_high', 'Avg high', 'temp', daily=False),
    Column('temp_avg_low', 'Avg low', 'temp', daily=False),
    Column('hdd', 'Heating degree days', 'temp_delta', help='Base 65 °F / 18 °C, from (high + low) / 2'),
    Column('cdd', 'Cooling degree days', 'temp_delta', help='Base 65 °F / 18 °C, from (high + low) / 2'),
    Column('days_hot', 'Hot days', 'count', daily=False, help='High ≥ 90 °F / 32 °C'),
    Column('days_freeze', 'Freeze days', 'count', daily=False, help='Low ≤ 32 °F / 0 °C'),
    Column('gdd', 'Growing degree days', 'temp_delta', help='Base 50 °F / 10 °C, capped at 86 °F / 30 °C'),
    Column('humidity_mean', 'Mean humidity', 'pct'),
    Column('dewpoint_mean', 'Mean dew point', 'temp'),
    Column('rain', 'Rain', 'rain'),
    Column('rain_days', 'Rain days', 'count', daily=False, help='≥ 0.01 in / 0.25 mm'),
    Column('rain_rate_max', 'Max rain rate', 'rate'),
    Column('et0', 'ET₀', 'rain', help='Reference evapotranspiration (FAO-56; Hargreaves on incomplete days)'),
    Column('wind_mean', 'Mean wind', 'speed'),
    Column('gust_max', 'Peak gust', 'speed'),
    Column('pressure_mean', 'Mean pressure', 'pressure'),
    Column('pressure_min', 'Min pressure', 'pressure'),
    Column('pressure_max', 'Max pressure', 'pressure'),
    Column('solar_max', 'Peak solar', 'wm2'),
    Column('uv_max', 'Max UV', 'uv'),
    Column('days_with_data', 'Days with data', 'count', help='Days with ≥ 90 % of temperature data'),
]
BY_KEY = {c.key: c for c in COLUMNS}
DEFAULT_COLUMNS = ['temp_high', 'temp_low', 'temp_mean', 'hdd', 'cdd', 'rain', 'wind_mean', 'gust_max', 'days_with_data']
# How the summary row (and month/year groups) combine values.
COMBINE = {
    'temp_high': 'max', 'temp_low': 'min', 'temp_mean': 'mean', 'temp_avg_high': 'mean', 'temp_avg_low': 'mean',
    'hdd': 'sum', 'cdd': 'sum', 'gdd': 'sum', 'et0': 'sum', 'days_hot': 'sum', 'days_freeze': 'sum', 'humidity_mean': 'mean',
    'dewpoint_mean': 'mean', 'rain': 'sum', 'rain_days': 'sum', 'rain_rate_max': 'max', 'wind_mean': 'mean',
    'gust_max': 'max', 'pressure_mean': 'mean', 'pressure_min': 'min', 'pressure_max': 'max', 'solar_max': 'max',
    'uv_max': 'max', 'days_with_data': 'sum',
}


# Extra-sensor columns ('x:<key>:<stat>'), per sensor kind: (stat, label, quantity, combine).
_SENSOR_STATS = {
    'temperature': [('high', 'high', 'temp', 'max'), ('low', 'low', 'temp', 'min'), ('mean', 'mean', 'temp', 'mean')],
    'soil_temp': [('high', 'high', 'temp', 'max'), ('low', 'low', 'temp', 'min'), ('mean', 'mean', 'temp', 'mean')],
    'humidity': [('mean', 'mean', 'pct', 'mean')],
    'soil_moisture': [('mean', 'mean', 'pct', 'mean')],
    'leaf_wetness': [('mean', 'mean', 'pct', 'mean')],
    'soil_tension': [('mean', 'mean', 'cb', 'mean')],
    'pm25': [('mean', 'mean', 'pm', 'mean'), ('max', 'peak', 'pm', 'max')],
    'pm10': [('mean', 'mean', 'pm', 'mean'), ('max', 'peak', 'pm', 'max')],
    'co2': [('mean', 'mean', 'ppm', 'mean')],
    'lightning': [('strikes', 'strikes', 'count', 'sum')],
}
_STAT_INDEX = {'mean': 0, 'low': 1, 'high': 2, 'max': 2, 'strikes': 2}   # rollup extra = [mean, min, max]


def sensor_columns(station, include_private=False):
    """Report columns for the station's extra sensors the viewer may see."""
    from .sensors import station_sensors
    out = []
    for sensor, name in station_sensors(station, include_private):
        for stat, label, quantity, combine in _SENSOR_STATS.get(sensor.kind, ()):
            out.append(Column(f'x:{sensor.key}:{stat}', f'{name} {label}', quantity,
                              help=f'{sensor.kind_label}: {label} of the readings each day'
                                   + (' (lightning: strikes counted by the detector)' if stat == 'strikes' else '')))
            COMBINE[f'x:{sensor.key}:{stat}'] = combine
    return out


def _sensor_values(r, columns, prefs):
    values = {}
    for c in columns:
        _, key, stat = c.key.split(':', 2)
        triple = (r.extra or {}).get(key)
        value = triple[_STAT_INDEX[stat]] if triple else None
        values[c.key] = prefs.t(value) if (value is not None and c.quantity == 'temp') else value
    return values


def _thresholds(prefs):
    imperial = prefs.temp == 'F'
    return {'base': 65.0 if imperial else 18.0, 'hot': 90.0 if imperial else 32.0, 'freeze': 32.0 if imperial else 0.0}


def _day_values(r, prefs, th, station):
    """Display-unit values for one DailyRollup (None where not known)."""
    full = r.temp_coverage >= DAY_COVERAGE
    hi, lo = prefs.t(r.temp_max_c), prefs.t(r.temp_min_c)
    mid = (hi + lo) / 2 if (full and hi is not None and lo is not None) else None
    rain = r.rain_mm if rain_known(r.rain_mm, r.rain_coverage, DAY_COVERAGE) else None
    return {
        'temp_high': hi, 'temp_low': lo,
        'temp_mean': prefs.t(r.temp_avg_c) if full else None,
        'temp_avg_high': hi if full else None, 'temp_avg_low': lo if full else None,
        'hdd': None if mid is None else max(0.0, th['base'] - mid),
        'cdd': None if mid is None else max(0.0, mid - th['base']),
        'days_hot': None if not full or hi is None else int(hi >= th['hot']),
        'days_freeze': None if not full or lo is None else int(lo <= th['freeze']),
        'humidity_mean': r.humidity_avg,
        'dewpoint_mean': prefs.t(r.dewpoint_avg_c) if full else None,
        'rain': prefs.r(rain),
        'rain_days': None if rain is None else int(rain >= MEASURABLE_RAIN_MM),
        'rain_rate_max': prefs.r(r.rain_rate_max_mmh),
        'wind_mean': prefs.w(r.wind_speed_avg_ms),
        'gust_max': prefs.w(r.wind_gust_max_ms),
        'pressure_mean': prefs.p(r.pressure_avg_hpa),
        'pressure_min': prefs.p(r.pressure_min_hpa),
        'pressure_max': prefs.p(r.pressure_max_hpa),
        'solar_max': r.solar_max_wm2, 'uv_max': r.uv_max,
        'days_with_data': int(full),
        'gdd': None if not full else _gdd_display(r, prefs),
        'et0': prefs.r(agro.et0_for_rollup(r, station)[0]),
    }


def _gdd_display(r, prefs):
    g = agro.gdd_day(r.temp_max_c, r.temp_min_c, agro.f_to_c(50), agro.f_to_c(86))
    return None if g is None else (g * 1.8 if prefs.temp == 'F' else g)


def _combine(how, values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return {'max': max, 'min': min, 'sum': sum, 'mean': lambda xs: sum(xs) / len(xs)}[how](vals)


@dataclass
class ReportRow:
    label: str
    start: dt.date
    end: dt.date
    values: list
    partial: bool = False


@dataclass
class Report:
    station: object
    start: dt.date
    end: dt.date
    group: str
    columns: list
    rows: list = field(default_factory=list)
    summary: list = field(default_factory=list)
    units: object = None


def auto_group(start, end):
    days = (end - start).days + 1
    return 'day' if days <= 62 else 'month' if days <= 3 * 366 else 'year'


def build(station, start, end, prefs, group='auto', column_keys=None, include_private=False):
    group = group if group in ('day', 'month', 'year') else auto_group(start, end)
    available = {**BY_KEY, **{c.key: c for c in sensor_columns(station, include_private)}}
    keys = [k for k in (column_keys or DEFAULT_COLUMNS) if k in available]
    columns = [available[k] for k in keys if group != 'day' or available[k].daily]
    extra_columns = [c for c in columns if c.key.startswith('x:')]
    th = _thresholds(prefs)
    by_date = {r.date: {**_day_values(r, prefs, th, station), **_sensor_values(r, extra_columns, prefs)}
               for r in DailyRollup.objects.filter(station=station, date__gte=start, date__lte=end)}

    def period(d):
        if group == 'day':
            return d, d
        if group == 'month':
            return d.replace(day=1), d.replace(day=calendar.monthrange(d.year, d.month)[1])
        return d.replace(month=1, day=1), d.replace(month=12, day=31)

    rows, d = [], start
    while d <= end:
        p_start, p_end = period(d)
        p_end = min(p_end, end)
        span = [max(p_start, start) + dt.timedelta(days=n) for n in range((p_end - max(p_start, start)).days + 1)]
        day_values = [by_date.get(x) for x in span]
        present = [v for v in day_values if v is not None]
        values = [_combine(COMBINE[c.key], [v[c.key] for v in present]) if present else None for c in columns]
        if group == 'day':
            label = f'{p_start:%a %b} {p_start.day}, {p_start.year}'
        elif group == 'month':
            label = f'{p_start:%B %Y}'
        else:
            label = str(p_start.year)
        full_days = sum(v['days_with_data'] for v in present)
        rows.append(ReportRow(label, max(p_start, start), p_end, values, partial=full_days < len(span)))
        d = p_end + dt.timedelta(days=1)

    # The summary row combines the *daily* values over the whole range, so a mean is
    # a mean of days (not of monthly means) and a total is the true total.
    all_days = list(by_date.values())
    summary = [_combine(COMBINE[c.key], [v[c.key] for v in all_days]) for c in columns]
    return Report(station, start, end, group, columns, rows, summary, prefs)


def digits_for(column, prefs):
    q = column.quantity
    if q in ('count', 'wm2', 'uv', 'pct', 'ppm', 'cb'):
        return 0
    if q == 'pm':
        return 1
    if q in ('temp', 'temp_delta'):
        return 1 if q == 'temp' else 0
    return {'rain': prefs.digits['rain'], 'rate': prefs.digits['rain'], 'speed': prefs.digits['wind'],
            'pressure': prefs.digits['pressure']}[q]


def unit_for(column, prefs):
    labels = prefs.as_json()['labels']
    return {'temp': labels['temp'], 'temp_delta': f'{labels["temp"]}·days', 'rain': labels['rain'],
            'rate': f'{labels["rain"]}/h', 'speed': labels['wind'], 'pressure': labels['pressure'],
            'pct': '%', 'count': '', 'wm2': 'W/m²', 'uv': '', 'pm': 'µg/m³', 'ppm': 'ppm', 'cb': 'cb'}[column.quantity]   # counts are labelled "… days" already


def to_csv(report):
    out = io.StringIO()
    w = csv.writer(out)
    prefs = report.units
    w.writerow([{'day': 'Date', 'month': 'Month', 'year': 'Year'}[report.group]]
               + [f'{c.label} ({unit_for(c, prefs)})' if unit_for(c, prefs) else c.label for c in report.columns])

    def cell(v, c):
        return '' if v is None else f'{v:.{digits_for(c, prefs)}f}'
    for row in report.rows:
        first = row.start.isoformat() if report.group == 'day' else (
            row.start.strftime('%Y-%m') if report.group == 'month' else str(row.start.year))
        w.writerow([first] + [cell(v, c) for v, c in zip(row.values, report.columns)])
    w.writerow(['Whole period'] + [cell(v, c) for v, c in zip(report.summary, report.columns)])
    return out.getvalue()
