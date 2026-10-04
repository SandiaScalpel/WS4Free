"""Daily and hourly forecast from Open-Meteo (https://open-meteo.com): free, no API key,
worldwide. Weather data by Open-Meteo.com, licensed CC BY 4.0, so the page
credits it.

The forecast is fetched when a dashboard is viewed and the stored one is more
than an hour old, with a short timeout. A failure keeps the previous forecast
and isn't retried for ten minutes, so a slow or unreachable service never
holds up the page. Only the station's location, rounded to two decimals
(about 1 km, finer than the forecast grid), is sent.
"""
import datetime as dt
import logging

import requests
from django.conf import settings
from django.utils import timezone

from .models import StationForecast

log = logging.getLogger(__name__)

DAYS = 16                      # Open-Meteo's longest; the dashboard strip shows as many as fit
REFRESH = dt.timedelta(hours=1)
RETRY = dt.timedelta(minutes=10)
TIMEOUT_S = 5
STALE = dt.timedelta(hours=12)  # older than this, show nothing rather than an out-of-date forecast

# WMO weather interpretation codes (as used by Open-Meteo) → (description, icon).
CODES = {
    0: ('Clear', 'sun'), 1: ('Mostly clear', 'sun'), 2: ('Partly cloudy', 'partly'), 3: ('Cloudy', 'cloud'),
    45: ('Fog', 'fog'), 48: ('Freezing fog', 'fog'),
    51: ('Light drizzle', 'drizzle'), 53: ('Drizzle', 'drizzle'), 55: ('Heavy drizzle', 'drizzle'),
    56: ('Freezing drizzle', 'sleet'), 57: ('Freezing drizzle', 'sleet'),
    61: ('Light rain', 'rain'), 63: ('Rain', 'rain'), 65: ('Heavy rain', 'rain'),
    66: ('Freezing rain', 'sleet'), 67: ('Freezing rain', 'sleet'),
    71: ('Light snow', 'snow'), 73: ('Snow', 'snow'), 75: ('Heavy snow', 'snow'), 77: ('Snow grains', 'snow'),
    80: ('Showers', 'showers'), 81: ('Showers', 'showers'), 82: ('Heavy showers', 'showers'),
    85: ('Snow showers', 'snow'), 86: ('Heavy snow showers', 'snow'),
    95: ('Thunderstorms', 'thunder'), 96: ('Thunderstorms with hail', 'thunder'), 99: ('Thunderstorms with hail', 'thunder'),
}


# Clear and partly cloudy skies look different after dark.
NIGHT_ICONS = {'sun': 'moon', 'partly': 'partly-night'}

# Open-Meteo field → our key. Wind is requested in m/s; times are station-local.
DAILY_FIELDS = {
    'weather_code': 'code', 'temperature_2m_max': 'high_c', 'temperature_2m_min': 'low_c',
    'precipitation_probability_max': 'rain_pct', 'precipitation_sum': 'rain_mm',
    'wind_speed_10m_max': 'wind_ms', 'wind_gusts_10m_max': 'gust_ms', 'wind_direction_10m_dominant': 'wind_dir',
    'uv_index_max': 'uv', 'sunrise': 'sunrise', 'sunset': 'sunset',
}
HOURLY_FIELDS = {
    'weather_code': 'code', 'temperature_2m': 'temp_c', 'apparent_temperature': 'feels_c',
    'relative_humidity_2m': 'humidity', 'precipitation_probability': 'rain_pct', 'precipitation': 'rain_mm',
    'wind_speed_10m': 'wind_ms', 'wind_gusts_10m': 'gust_ms', 'wind_direction_10m': 'wind_dir', 'is_day': 'is_day',
}


def describe(code, night=False):
    label, icon = CODES.get(code, ('', 'cloud'))
    return label, (NIGHT_ICONS.get(icon, icon) if night else icon)


def _columns(block, fields):
    """Open-Meteo's column arrays → one dict per row. A field it didn't send is None."""
    times = block['time']
    columns = {ours: block.get(theirs) or [None] * len(times) for theirs, ours in fields.items()}
    return [{'time': t, **{k: v[i] for k, v in columns.items()}} for i, t in enumerate(times)]


def fetch(station, session=None):
    """(days, hours) for the station, SI units, station-local dates and times:
    days [{date, code, high_c, low_c, rain_pct, rain_mm, wind_ms, gust_ms, wind_dir, uv, sunrise, sunset}],
    hours [{time, code, temp_c, feels_c, humidity, rain_pct, rain_mm, wind_ms, gust_ms, wind_dir, is_day}]."""
    params = {
        'latitude': round(float(station.latitude), 2), 'longitude': round(float(station.longitude), 2),
        'daily': ','.join(DAILY_FIELDS), 'hourly': ','.join(HOURLY_FIELDS), 'wind_speed_unit': 'ms',
        'timezone': station.timezone, 'forecast_days': DAYS,
    }
    response = (session or requests).get(settings.FORECAST_URL, params=params, timeout=TIMEOUT_S)
    response.raise_for_status()
    data = response.json()
    days = []
    for row in _columns(data['daily'], DAILY_FIELDS):
        row['date'] = row.pop('time')
        for key in ('sunrise', 'sunset'):           # '2026-10-04T07:03' → '07:03'
            row[key] = row[key][11:16] if row[key] else None
        days.append(row)
    return days, _columns(data.get('hourly') or {'time': []}, HOURLY_FIELDS)


def _stored(station, now, session=None):
    """The station's StationForecast, refreshed first if it's due; None when
    forecasts are off for it or what's stored is too old to show."""
    if not settings.FORECAST_URL or not station.forecast_enabled or station.latitude is None or station.longitude is None:
        return None
    stored, _ = StationForecast.objects.get_or_create(station=station)
    # A forecast stored before hourly data was fetched is refreshed straight away.
    due = stored.fetched_at is None or now - stored.fetched_at >= REFRESH or not stored.hours
    retry_ok = stored.attempted_at is None or now - stored.attempted_at >= RETRY
    if due and retry_ok:
        # Claim the attempt first so concurrent page loads don't all call the service.
        if StationForecast.objects.filter(pk=stored.pk, attempted_at=stored.attempted_at).update(attempted_at=now):
            try:
                days, hours = fetch(station, session)
            except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
                log.warning('Forecast for %s failed: %s', station, exc)
                StationForecast.objects.filter(pk=stored.pk).update(error=str(exc)[:200])
            else:
                StationForecast.objects.filter(pk=stored.pk).update(days=days, hours=hours, fetched_at=now, error='')
            stored.refresh_from_db()
    if stored.fetched_at is None or now - stored.fetched_at > STALE:
        return None
    return stored


def _today(station, now):
    return now.astimezone(station.tzinfo).date().isoformat()


def current(station, now=None, session=None):
    """The station's daily forecast from today on, refreshing it first if it's due.
    Returns [] when there's nothing worth showing."""
    now = now or timezone.now()
    stored = _stored(station, now, session)
    if stored is None:
        return []
    today = _today(station, now)
    return [d for d in stored.days if d['date'] >= today]


def day(station, date, now=None, session=None):
    """(day, hours) of the forecast for one station-local date (an ISO string), or
    (None, []) when that date isn't in the forecast (or is already past)."""
    now = now or timezone.now()
    stored = _stored(station, now, session)
    if stored is None or date < _today(station, now):
        return None, []
    found = next((d for d in stored.days if d['date'] == date), None)
    return found, [h for h in stored.hours if h['time'].startswith(date)] if found else []


def _trace(mm, prefs):
    """None for a trace: under 0.01 in / 0.1 mm says nothing useful."""
    return mm if mm and prefs.r(mm) >= (0.01 if prefs.rain == 'in' else 0.1) else None


def display(days, prefs, today):
    """Template-ready days in the viewer's units."""
    out = []
    for d in days:
        date = dt.date.fromisoformat(d['date'])
        label, icon = describe(d['code'])
        out.append({
            'day': 'Today' if date == today else date.strftime('%a'), 'date': date, 'label': label, 'icon': icon,
            'high_c': d['high_c'], 'low_c': d['low_c'], 'rain_pct': d['rain_pct'], 'rain_mm': _trace(d['rain_mm'], prefs),
            # Added in 0.10; a forecast stored by an older version lacks them until its next refresh.
            'wind_ms': d.get('wind_ms'), 'gust_ms': d.get('gust_ms'), 'wind_dir': d.get('wind_dir'),
            'uv': d.get('uv'), 'sunrise': _clock(d.get('sunrise')), 'sunset': _clock(d.get('sunset')),
        })
    return out


def _clock(hhmm):
    return dt.time.fromisoformat(hhmm) if hhmm else None


def temperature_bars(days):
    """Each day's low-to-high span as CSS percentages of the whole forecast's range,
    for the forecast page's range bars. Adds `bar_left` and `bar_width` in place."""
    temps = [t for d in days for t in (d['low_c'], d['high_c']) if t is not None]
    if not temps:
        return days
    lo, span = min(temps), (max(temps) - min(temps)) or 1.0
    for d in days:
        if d['low_c'] is not None and d['high_c'] is not None:
            d['bar_left'] = round((d['low_c'] - lo) / span * 100, 1)
            d['bar_width'] = max(round((d['high_c'] - d['low_c']) / span * 100, 1), 2.0)
    return days


def display_hours(hours, prefs, now_local=None):
    """Template-ready hours in the viewer's units. `past` marks hours that have
    ended, so today's pop-up can grey them out."""
    out = []
    for h in hours:
        time = dt.datetime.fromisoformat(h['time'])
        label, icon = describe(h['code'], night=h.get('is_day') == 0)
        out.append({
            'time': time, 'label': label, 'icon': icon,
            'temp_c': h['temp_c'], 'feels_c': h['feels_c'], 'humidity': h['humidity'],
            'rain_pct': h['rain_pct'], 'rain_mm': _trace(h['rain_mm'], prefs),
            'wind_ms': h['wind_ms'], 'gust_ms': h['gust_ms'], 'wind_dir': h['wind_dir'],
            'past': now_local is not None and time + dt.timedelta(hours=1) <= now_local,
        })
    return out


def hours_chart(hours, prefs):
    """The pop-up's two small charts (temperature, chance of rain), viewer units."""
    return {
        'units': prefs.as_json(),
        'hours': [h['time'].strftime('%H:%M') for h in hours],
        'temp': [None if h['temp_c'] is None else round(prefs.t(h['temp_c']), 1) for h in hours],
        'rain_pct': [h['rain_pct'] for h in hours],
    }
