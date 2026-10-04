"""Data for the Charts page, already converted to the viewer's units.

Resolution follows the requested span so a chart never carries more points than
it can show: raw 5-minute observations up to 3 days, hourly rollups up to 120
days, daily rollups beyond. Gaps longer than two steps get an explicit null so a
station outage draws as a break — not a straight line across 66 missing days.
Daily values whose sensor coverage is poor are nulled for the same reason.
"""
import csv
import datetime as dt
import io
import math

from django.db.models import Case, F, FloatField, IntegerField, Min, Sum, When
from django.db.models.functions import Floor, Mod

from .models import DailyRollup, HourlyRollup, Observation, StationEvent
from .sensors import low_batteries, station_sensors
from .units import HEAT_INDEX, WIND_CHILL, apparent

RAW_MAX = dt.timedelta(days=3)
HOURLY_MAX = dt.timedelta(days=120)
MIN_DAILY_COVERAGE = 0.5      # below this a day's temperature is shown as missing
CALENDAR_COVERAGE = 0.9


def rain_known(rain_mm, coverage, threshold):
    """Whether a day's rain total can be shown. Measured rain is real however
    incomplete the day was; only a *zero* needs enough coverage to be trusted
    (no data must not read as a dry day)."""
    if rain_mm is None:
        return False
    return rain_mm > 0 or coverage >= threshold

# Wind rose speed bins, in each unit's own round numbers (upper edges).
ROSE_BINS = {'mph': (1, 5, 10, 15, 20), 'kmh': (2, 8, 16, 24, 32), 'ms': (0.5, 2, 4, 6, 8), 'kn': (1, 4, 8, 12, 16)}


def local_range(station, start_date, end_date):
    """Local calendar dates (inclusive) → UTC instants (start, end]."""
    tz = station.tzinfo
    start = dt.datetime.combine(start_date, dt.time(), tzinfo=tz).astimezone(dt.UTC)
    end = dt.datetime.combine(end_date + dt.timedelta(days=1), dt.time(), tzinfo=tz).astimezone(dt.UTC)
    return start, end


def resolution_for(start, end):
    span = end - start
    if span <= RAW_MAX:
        return 'raw'
    if span <= HOURLY_MAX:
        return 'hourly'
    return 'daily'


def _r(value, digits):
    return None if value is None else round(value, digits)


def _with_gaps(rows, max_gap_ms):
    """Insert an all-null row wherever consecutive times are more than max_gap_ms apart."""
    out = []
    for row in rows:
        if out and row[0] - out[-1][0] > max_gap_ms:
            out.append([out[-1][0] + 1] + [None] * (len(row) - 1))
        out.append(row)
    return out


HISTORY_COLUMNS = ('time', 'temp', 'temp_min', 'temp_max', 'dewpoint', 'humidity', 'wind', 'gust',
                   'rain', 'pressure', 'solar', 'uv', 'windchill', 'heatindex')


def _apparent_pair(temp_c, humidity, wind_ms):
    """(wind chill, heat index) for one reading; each None unless it applies."""
    feels, kind = apparent(temp_c, humidity, wind_ms)
    return (feels if kind == WIND_CHILL else None), (feels if kind == HEAT_INDEX else None)


# Extra-sensor values in the viewer's units, by unit kind (weather.sensors).
SENSOR_DIGITS = {'temp': 1, 'pct': 0, 'cb': 0, 'pm': 1, 'ppm': 0, 'count': 0}
SENSOR_LINES_PER_CHART = 8      # the categorical palette's size; more channels → another chart


def _sensor_display(sensor, si, prefs):
    if si is None:
        return None
    return _r(prefs.t(si) if sensor.unit_kind == 'temp' else si, SENSOR_DIGITS.get(sensor.unit_kind, 1))


def _strikes(times_ms, counters, tzinfo):
    """Lightning: consoles report strikes *so far today*. Turn the counter at the end
    of each bucket into strikes during it (a new local day starts from zero)."""
    out, prev, prev_day = [], None, None
    for ms, value in zip(times_ms, counters):
        day = dt.datetime.fromtimestamp(ms / 1000, tzinfo).date()
        if value is None:
            out.append(None)
            continue
        if prev is not None and day == prev_day and value >= prev:
            out.append(value - prev)
        else:
            out.append(value)
        prev, prev_day = value, day
    return out


def _sensor_charts(sensors, prefs):
    """Chart definitions for the History section: one chart per kind (split past 8 channels)."""
    units = {'temp': prefs.label('temp'), 'pct': '%', 'cb': 'cb', 'pm': 'µg/m³', 'ppm': 'ppm', 'count': 'strikes'}
    by_kind = {}
    for sensor, name in sensors:
        by_kind.setdefault(sensor.kind, []).append((sensor, name))
    charts = []
    for kind, members in by_kind.items():
        for part in range(0, len(members), SENSOR_LINES_PER_CHART):
            chunk = members[part:part + SENSOR_LINES_PER_CHART]
            first = chunk[0][0]
            # Named so they can't be mistaken for the main charts: "Temperature: Greenhouse (°F)"
            # for one sensor (no legend then), "Temperature sensors (°F)" for several.
            if kind == 'lightning':
                title = 'Lightning strikes'
            elif len(chunk) == 1:
                title = f'{first.kind_label}: {chunk[0][1]} ({units[first.unit_kind]})'
            else:
                title = f'{first.kind_label} sensors ({units[first.unit_kind]})'
            charts.append({
                'key': f'sensor_{kind}_{part}',
                'title': title,
                'unit': units[first.unit_kind],
                'digits': SENSOR_DIGITS.get(first.unit_kind, 1),
                'bars': kind == 'lightning',
                'lines': [{'col': f'x:{s.key}', 'name': n} for s, n in chunk],
            })
    return charts


def history(station, start, end, prefs, include_private=False):
    """Columnar series for (start, end]. Times are epoch ms of each bucket's start.
    Extra sensors the viewer may see are added as `x:<upload key>` columns."""
    res = resolution_for(start, end)
    d = prefs.digits
    sensors = [(s, n) for s, n in station_sensors(station, include_private) if s.chartable]
    if res == 'raw':
        qs = (Observation.objects.filter(station=station, timestamp__gt=start, timestamp__lte=end)
              .order_by('timestamp').values_list('timestamp', 'interval_s', 'temp_c', 'temp_min_c', 'temp_max_c',
                                                'dewpoint_c', 'humidity', 'wind_speed_ms', 'wind_gust_ms', 'rain_mm',
                                                'pressure_rel_hpa', 'solar_wm2', 'uv_index', 'extra', 'source'))
        raw = list(qs)
        battery = [(int((r[0] - dt.timedelta(seconds=r[1])).timestamp() * 1000), low_batteries(r[13], r[14])) for r in raw]
        rows = [[int((ts - dt.timedelta(seconds=iv)).timestamp() * 1000),
                 _r(prefs.t(t), d['temp']), _r(prefs.t(tmin), d['temp']), _r(prefs.t(tmax), d['temp']),
                 _r(prefs.t(dp), d['temp']), _r(h, 0), _r(prefs.w(w), 1), _r(prefs.w(g), 1),
                 _r(prefs.r(rain), d['rain'] + 1), _r(prefs.p(p), d['pressure'] + 1), _r(sol, 0), _r(uv, 1)]
                + [_r(prefs.t(v), d['temp']) for v in _apparent_pair(t, h, w)]
                + [_sensor_display(s, s.si((extra or {}).get(s.key)), prefs) for s, _ in sensors]
                for ts, iv, t, tmin, tmax, dp, h, w, g, rain, p, sol, uv, extra, _src in raw]
        step_ms = 300_000
    else:
        model, key = (HourlyRollup, 'period_start') if res == 'hourly' else (DailyRollup, 'date')
        qs = model.objects.filter(station=station)
        if res == 'hourly':
            qs = qs.filter(period_start__gte=start, period_start__lt=end)
        else:
            tz = station.tzinfo
            qs = qs.filter(date__gte=start.astimezone(tz).date(), date__lte=(end - dt.timedelta(seconds=1)).astimezone(tz).date())
        rows, battery = [], []
        for r in qs.order_by(key):
            if res == 'hourly':
                ms = int(r.period_start.timestamp() * 1000)
            else:
                ms = int(dt.datetime.combine(r.date, dt.time(), tzinfo=station.tzinfo).timestamp() * 1000)
            battery.append((ms, r.low_batteries or []))
            temp_ok = res == 'hourly' or r.temp_coverage >= MIN_DAILY_COVERAGE
            rain_ok = res == 'hourly' or rain_known(r.rain_mm, r.rain_coverage, MIN_DAILY_COVERAGE)
            rows.append([
                ms,
                _r(prefs.t(r.temp_avg_c), d['temp']) if temp_ok else None,
                _r(prefs.t(r.temp_min_c), d['temp']) if temp_ok else None,
                _r(prefs.t(r.temp_max_c), d['temp']) if temp_ok else None,
                _r(prefs.t(r.dewpoint_avg_c), d['temp']) if temp_ok else None,
                _r(r.humidity_avg, 0),
                _r(prefs.w(r.wind_speed_avg_ms), 1), _r(prefs.w(r.wind_gust_max_ms), 1),
                _r(prefs.r(r.rain_mm), d['rain'] + 1) if rain_ok else None,
                _r(prefs.p(r.pressure_avg_hpa), d['pressure'] + 1),
                _r(r.solar_max_wm2 if res == 'daily' else r.solar_avg_wm2, 0),
                _r(r.uv_max, 1),
                # Lowest wind chill / highest heat index of the hour or day.
                _r(prefs.t(r.windchill_min_c), d['temp']),
                _r(prefs.t(r.heatindex_max_c), d['temp']),
            ] + [
                # Lightning keeps the counter's highest value (strikes so far today); the rest their mean.
                _sensor_display(sen, (r.extra.get(sen.key) or [None, None, None])[2 if sen.kind == 'lightning' else 0], prefs)
                for sen, _ in sensors
            ])
        step_ms = 3_600_000 if res == 'hourly' else 86_400_000
    names = list(HISTORY_COLUMNS) + [f'x:{s.key}' for s, _ in sensors]
    columns = [list(col) for col in zip(*rows)] if rows else [[] for _ in names]
    series = dict(zip(names, columns))
    for sensor, _ in sensors:
        if sensor.kind == 'lightning' and res != 'daily':
            col = f'x:{sensor.key}'
            series[col] = _strikes(series['time'], series[col], station.tzinfo)
    rows = _with_gaps([list(r) for r in zip(*(series[n] for n in names))] if rows else [], 2 * step_ms + 1)
    columns = [list(col) for col in zip(*rows)] if rows else [[] for _ in names]
    return {
        'resolution': res,
        'start': int(start.timestamp() * 1000),
        'end': int(end.timestamp() * 1000),
        'series': dict(zip(names, columns)),
        'sensor_charts': _sensor_charts(sensors, prefs),
        # Low-battery periods are for the owner, like the dashboard's battery warning.
        'battery': battery_periods(battery, step_ms) if include_private else [],
    }


BATTERY_BRIDGE_MS = 86_400_000    # a weak battery reads low at night and recovers in the afternoon sun


def battery_periods(buckets, step_ms):
    """Merge buckets that reported a low battery into periods:
    [{'start': ms, 'end': ms, 'names': [...], 'intermittent': bool}]. Low readings
    less than a day apart belong to the same period (marked intermittent when the
    battery read OK in between); a longer gap ends it."""
    periods = []
    for ms, names in buckets:
        if not names:
            continue
        last = periods[-1] if periods else None
        if last and ms - last['end'] <= max(step_ms, BATTERY_BRIDGE_MS):
            if ms > last['end']:
                last['intermittent'] = True
            last['end'] = ms + step_ms
            last['names'] = sorted(set(last['names']) | set(names))
        else:
            periods.append({'start': ms, 'end': ms + step_ms, 'names': list(names), 'intermittent': False})
    return periods


def history_csv(station, start, end, prefs, include_private=False):
    data = history(station, start, end, prefs, include_private)
    tz = station.tzinfo
    labels = prefs.as_json()['labels']
    header = ['time (' + station.timezone + ')', f'temperature ({labels["temp"]})', f'temp min ({labels["temp"]})',
              f'temp max ({labels["temp"]})', f'dew point ({labels["temp"]})', 'humidity (%)',
              f'wind ({labels["wind"]})', f'gust ({labels["wind"]})', f'rain ({labels["rain"]})',
              f'pressure ({labels["pressure"]})', 'solar (W/m²)', 'UV index',
              f'wind chill ({labels["temp"]})', f'heat index ({labels["temp"]})']
    columns = list(HISTORY_COLUMNS[1:])
    for chart in data['sensor_charts']:
        unit = chart['unit']
        for line in chart['lines']:
            columns.append(line['col'])
            header.append(f'{line["name"]} ({unit})')
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    s = data['series']
    for i, ms in enumerate(s['time']):
        if all(s[c][i] is None for c in columns):
            continue   # gap marker
        when = dt.datetime.fromtimestamp(ms / 1000, tz).strftime('%Y-%m-%d %H:%M')
        writer.writerow([when] + ['' if s[c][i] is None else s[c][i] for c in columns])
    return out.getvalue()


def wind_rose(station, start, end, prefs):
    """Share of time (%) by 16 compass directions × speed bins, plus calm."""
    edges = ROSE_BINS[prefs.wind]
    si_edges = [e / prefs.w(1.0) for e in edges]          # viewer-unit edges → m/s
    qs = Observation.objects.filter(station=station, timestamp__gt=start, timestamp__lte=end,
                                    wind_speed_ms__isnull=False)
    total = qs.aggregate(s=Sum('interval_s'))['s'] or 0
    calm = qs.filter(wind_speed_ms__lt=si_edges[0]).aggregate(s=Sum('interval_s'))['s'] or 0
    whens = [When(wind_speed_ms__lt=si_edges[i + 1], then=i) for i in range(len(si_edges) - 1)]
    cells = (qs.filter(wind_speed_ms__gte=si_edges[0], wind_dir_deg__isnull=False)
             .annotate(dbin=Mod(Floor((F('wind_dir_deg') + 11.25) / 22.5, output_field=FloatField()), 16),
                       sbin=Case(*whens, default=len(si_edges) - 1, output_field=IntegerField()))
             .values('dbin', 'sbin').annotate(s=Sum('interval_s')))
    grid = [[0.0] * 16 for _ in range(len(si_edges))]
    for c in cells:
        grid[int(c['sbin'])][int(c['dbin']) % 16] += c['s']
    pct = (lambda v: round(100.0 * v / total, 2)) if total else (lambda v: 0.0)
    label = prefs.label('wind')
    names = [f'{edges[i]}–{edges[i + 1]} {label}' for i in range(len(edges) - 1)] + [f'{edges[-1]}+ {label}']
    return {
        'bins': names,
        'values': [[pct(v) for v in row] for row in grid],
        'calm_pct': pct(calm),
        'hours': round(total / 3600, 1),
    }


TEMP_METRICS = {'high': 'temp_max_c', 'low': 'temp_min_c', 'mean': 'temp_avg_c'}


def years_available(station):
    first = DailyRollup.objects.filter(station=station).aggregate(d=Min('date'))['d']
    if first is None:
        return []
    last = DailyRollup.objects.filter(station=station).order_by('-date').values_list('date', flat=True).first()
    return list(range(first.year, last.year + 1))


def calendar(station, year, metric, prefs):
    """[date, value] for every day of `year`; missing/poorly covered days are absent."""
    qs = DailyRollup.objects.filter(station=station, date__year=year).order_by('date')
    days = []
    if metric == 'rain':
        for r in qs:
            if rain_known(r.rain_mm, r.rain_coverage, CALENDAR_COVERAGE):
                days.append([r.date.isoformat(), _r(prefs.r(r.rain_mm), prefs.digits['rain'])])
    else:
        field = TEMP_METRICS.get(metric, 'temp_max_c')
        for r in qs.filter(temp_coverage__gte=CALENDAR_COVERAGE):
            value = getattr(r, field)
            if value is not None:
                days.append([r.date.isoformat(), _r(prefs.t(value), prefs.digits['temp'])])
    return {'year': year, 'metric': metric, 'days': days}


def _doy_key(date):
    """Day-of-year on a leap-year axis, so Feb 29 has a slot and Mar 1 lines up every year."""
    return (dt.date(2000, date.month, date.day) - dt.date(2000, 1, 1)).days


def year_over_year(station, metric, prefs, smooth_days=7):
    """One 366-slot series per year. Rain: cumulative total of all measured rain
    (a day that can't be trusted as dry breaks the line but adds nothing).
    Temperatures: centred moving average of daily values."""
    rows = DailyRollup.objects.filter(station=station).order_by('date')
    by_year = {}
    for r in rows:
        by_year.setdefault(r.date.year, []).append(r)
    series = []
    for year, days in sorted(by_year.items()):
        values = [None] * 366
        if metric == 'rain':
            total = 0.0
            for r in days:
                if rain_known(r.rain_mm, r.rain_coverage, MIN_DAILY_COVERAGE):
                    total += r.rain_mm
                    values[_doy_key(r.date)] = _r(prefs.r(total), prefs.digits['rain'])
        else:
            field = TEMP_METRICS.get(metric, 'temp_max_c')
            raw = [None] * 366
            for r in days:
                if r.temp_coverage >= CALENDAR_COVERAGE and getattr(r, field) is not None:
                    raw[_doy_key(r.date)] = getattr(r, field)
            half = smooth_days // 2
            for i in range(366):
                window = [v for v in raw[max(0, i - half): i + half + 1] if v is not None]
                if raw[i] is not None and len(window) >= math.ceil(smooth_days / 2):
                    values[i] = _r(prefs.t(sum(window) / len(window)), prefs.digits['temp'])
        series.append({'year': year, 'values': values})
    return {'metric': metric, 'series': series}


def events(station, start, end, include_private=False):
    """Station log entries in (start, end] as chart markers."""
    qs = StationEvent.objects.filter(station=station, occurred_at__gte=start, occurred_at__lte=end)
    if not include_private:
        qs = qs.filter(is_public=True)
    return [{'t': int(e.occurred_at.timestamp() * 1000), 'has_time': e.has_time, 'kind': e.get_kind_display(),
             'title': e.title, 'notes': e.notes[:300]} for e in qs.order_by('occurred_at')]
