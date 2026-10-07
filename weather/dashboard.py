"""Everything the station dashboard shows, gathered in one place.

Live values come from LatestReading (updated by every push/poll); "today" uses
the daily rollup (≤ 5 minutes old) widened by the live reading, so the high and
low never lag the number on screen. Month and year rain add the console's own
daily counter for today to the rollups of the days before, so the totals match
the console. Values stay SI here; templates and the chart JSON convert to the
viewer's units.
"""
import datetime as dt
from types import SimpleNamespace

from django.db.models import Sum
from django.utils import timezone

from . import access
from . import forecast as forecasts
from . import neighbours as neighbour_data
from . import sensors as sensor_catalog
from . import units as u
from .models import DailyRollup, LatestReading, Observation
from .calibration import correct_live
from .ingest.store import console_source
from .quality import excluded_fields_at

STALE_AFTER = dt.timedelta(minutes=10)
GATEWAY_FRESH = dt.timedelta(minutes=15)
SPARK_HOURS = 24
RAIN_DAYS = 14


def _max(*values):
    values = [v for v in values if v is not None]
    return max(values) if values else None


def _min(*values):
    values = [v for v in values if v is not None]
    return min(values) if values else None


def _pressure_change(station, latest):
    """Change in relative pressure over the last three hours (hPa), or None."""
    now_p = latest.data.get('pressure_rel_hpa') if latest else None
    if now_p is None:
        return None
    target = latest.timestamp - dt.timedelta(hours=3)
    then = (Observation.objects.filter(station=station, pressure_rel_hpa__isnull=False,
                                       timestamp__gte=target - dt.timedelta(minutes=20),
                                       timestamp__lte=target + dt.timedelta(minutes=20))
            .order_by('timestamp').values_list('pressure_rel_hpa', flat=True).first())
    return None if then is None else now_p - then


def _series(station, now, prefs):
    rows = list(Observation.objects.filter(station=station, timestamp__gt=now - dt.timedelta(hours=SPARK_HOURS))
                .order_by('timestamp')
                .values_list('timestamp', 'temp_c', 'pressure_rel_hpa', 'wind_speed_ms', 'wind_gust_ms',
                             'humidity', 'dewpoint_c'))

    def points(index, convert, digits):
        return [[int(r[0].timestamp() * 1000), round(convert(r[index]), digits)] for r in rows if r[index] is not None]

    d = prefs.digits
    return {
        'temp': points(1, prefs.t, d['temp']),
        'pressure': points(2, prefs.p, d['pressure'] + 1),
        'wind': points(3, prefs.w, 1),
        'gust': points(4, prefs.w, 1),
        'humidity': points(5, lambda v: v, 0),
        'dewpoint': points(6, prefs.t, d['temp']),
    }


def _live(station, latest):
    """(data, excluded fields, calibrated?) for the latest reading as it should be shown."""
    if latest is None:
        return {}, set(), False
    data = dict(latest.data)
    # An ongoing data-quality exclusion (a failing sensor) hides that live value too.
    excluded = excluded_fields_at(station, latest.timestamp)
    for field in excluded:
        data.pop(field, None)
    corrected = correct_live(station, data, latest.timestamp)
    return corrected, excluded, corrected is not data


def _sensors_now(station, latest, now):
    """The latest extra-sensor values: the console's, plus any sensor gateway's
    that has reported recently (gateways upload extra sensors only)."""
    extra = dict(latest.extra) if latest else {}
    for gateway in station.gateways.filter(latest_at__gte=now - GATEWAY_FRESH):
        extra.update(gateway.latest_extra)
    return SimpleNamespace(extra=extra, timestamp=latest.timestamp if latest else now,
                           source=latest.source if latest else console_source(station))


def _neighbours(station, data, now):
    """Owner's line on the temperature card: the neighbours' average now, and the difference."""
    live = neighbour_data.live(station, now)
    if live is None:
        return None
    for field in ('temp_c', 'humidity'):
        live[f'{field}_diff'] = None if data.get(field) is None or live[field] is None else data[field] - live[field]
    return live


def live_values(station, latest):
    """The latest reading's values as the dashboard shows them (exclusions and calibration applied)."""
    return _live(station, latest)[0]


def build(station, prefs, viewer=None, now=None):
    now = now or timezone.now()
    tz = station.tzinfo
    today = now.astimezone(tz).date()
    latest = LatestReading.objects.filter(station=station).first()
    data, excluded, calibrated = _live(station, latest)
    rollup = DailyRollup.objects.filter(station=station, date=today).first()
    sensors_now = _sensors_now(station, latest, now)

    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    past = DailyRollup.objects.filter(station=station, date__lt=today)
    month_before = past.filter(date__gte=month_start).aggregate(s=Sum('rain_mm'))['s'] or 0.0
    year_before = past.filter(date__gte=year_start).aggregate(s=Sum('rain_mm'))['s'] or 0.0
    rain_today = data.get('rain_daily_mm')
    if rain_today is None and rollup is not None:
        rain_today = rollup.rain_mm
    rain_days = list(DailyRollup.objects.filter(station=station, date__gt=today - dt.timedelta(days=RAIN_DAYS),
                                                date__lt=today).order_by('date').values_list('date', 'rain_mm'))

    temp = data.get('temp_c')
    age = (now - latest.timestamp) if latest else None
    change = _pressure_change(station, latest)
    can_see_private = access.can_see_private(viewer, station)
    can_manage = access.can_manage(viewer, station)
    can_see_management = access.can_see_management(viewer, station)

    return {
        'station': station,
        'latest': latest,
        'now': data,
        'calibrated': calibrated,
        'age_s': int(age.total_seconds()) if age is not None else None,
        'stale': age is None or age > STALE_AFTER,
        **dict(zip(('feels_like_c', 'feels_kind'), u.apparent(temp, data.get('humidity'), data.get('wind_speed_ms')))),
        'today': {
            'high_c': _max(rollup.temp_max_c if rollup else None, temp),
            'low_c': _min(rollup.temp_min_c if rollup else None, temp),
            'gust_max_ms': _max(rollup.wind_gust_max_ms if rollup else None, data.get('wind_gust_ms')),
            'rain_mm': rain_today,
            'rain_rate_max_mmh': _max(rollup.rain_rate_max_mmh if rollup else None, data.get('rain_rate_mmh')),
            'uv_max': _max(rollup.uv_max if rollup else None, data.get('uv_index')),
            'solar_max_wm2': _max(rollup.solar_max_wm2 if rollup else None, data.get('solar_wm2')),
        },
        'rain_month_mm': month_before + (rain_today or 0.0),
        'rain_year_mm': year_before + (rain_today or 0.0),
        'rain_event_mm': data.get('rain_event_mm'),
        'pressure_change_hpa': change,
        'pressure_trend': u.pressure_trend(change),
        'uv': u.uv_category(data.get('uv_index')),
        'wind_compass': u.compass(data.get('wind_dir_deg')),
        'forecast': forecasts.display(forecasts.current(station, now=now), prefs, today),
        'sensor_groups': sensor_catalog.dashboard_groups(station, sensors_now, rollup.extra if rollup else {}, prefs,
                                                         exclude={f[2:] for f in excluded if f.startswith('x:')},
                                                         include_private=can_see_private, batteries=can_see_management),
        'low_batteries': sensor_catalog.low_batteries(sensors_now.extra, sensors_now.source) if can_see_management else [],
        'neighbours': _neighbours(station, data, now) if can_see_management and station.uses_site_services else None,
        'show_indoor': can_see_private and (data.get('temp_in_c') is not None or data.get('humidity_in') is not None),
        'can_manage': can_manage,
        'can_see_private': can_see_private,
        'can_see_management': can_see_management,
        'chart_data': {
            'units': prefs.as_json(),
            'tz': station.timezone,
            'series': _series(station, now, prefs),
            'rain_days': [[d.isoformat(), None if mm is None else round(prefs.r(mm), prefs.digits['rain'] + 1)]
                          for d, mm in rain_days],
        },
    }
