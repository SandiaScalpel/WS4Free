"""Everything the station dashboard shows, gathered in one place.

Live values come from LatestReading (updated by every push/poll); "today" uses
the daily rollup (≤ 5 minutes old) widened by the live reading, so the high and
low never lag the number on screen. Month and year rain add the console's own
daily counter for today to the rollups of the days before, so the totals match
the console. Values stay SI here; templates and the chart JSON convert to the
viewer's units.
"""
import datetime as dt

from django.db.models import Sum
from django.utils import timezone

from . import sensors as sensor_catalog
from . import units as u
from .models import DailyRollup, LatestReading, Observation
from .calibration import correct_live
from .quality import excluded_fields_at

STALE_AFTER = dt.timedelta(minutes=10)
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


def build(station, prefs, viewer=None, now=None):
    now = now or timezone.now()
    tz = station.tzinfo
    today = now.astimezone(tz).date()
    latest = LatestReading.objects.filter(station=station).first()
    data = dict(latest.data) if latest else {}
    calibrated = False
    if latest:
        # An ongoing data-quality exclusion (a failing sensor) hides that live value too.
        for field in excluded_fields_at(station, latest.timestamp):
            data.pop(field, None)
        corrected = correct_live(station, data, latest.timestamp)
        calibrated = corrected is not data
        data = corrected
    rollup = DailyRollup.objects.filter(station=station, date=today).first()

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
    can_see_private = viewer is not None and viewer.is_authenticated and (
        viewer.is_staff or viewer.pk == station.owner_id)

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
        'sensor_groups': sensor_catalog.dashboard_groups(station, latest, rollup.extra if rollup else {}, prefs,
                                                         include_private=can_see_private),
        'low_batteries': sensor_catalog.low_batteries(latest.extra, latest.source) if latest and can_see_private else [],
        'show_indoor': can_see_private and (data.get('temp_in_c') is not None or data.get('humidity_in') is not None),
        'can_manage': can_see_private,
        'chart_data': {
            'units': prefs.as_json(),
            'tz': station.timezone,
            'series': _series(station, now, prefs),
            'rain_days': [[d.isoformat(), None if mm is None else round(prefs.r(mm), prefs.digits['rain'] + 1)]
                          for d, mm in rain_days],
        },
    }
