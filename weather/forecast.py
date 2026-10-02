"""Daily forecast from Open-Meteo (https://open-meteo.com): free, no API key,
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

DAYS = 14                      # more than ever fit; the strip shows as many as its width allows
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


def describe(code):
    return CODES.get(code, ('', 'cloud'))


def fetch(station, session=None):
    """Daily forecast for the station: [{date, code, high_c, low_c, rain_pct, rain_mm}], SI units."""
    params = {
        'latitude': round(float(station.latitude), 2), 'longitude': round(float(station.longitude), 2),
        'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum',
        'timezone': station.timezone, 'forecast_days': DAYS,
    }
    response = (session or requests).get(settings.FORECAST_URL, params=params, timeout=TIMEOUT_S)
    response.raise_for_status()
    daily = response.json()['daily']
    keys = ('weather_code', 'temperature_2m_max', 'temperature_2m_min', 'precipitation_probability_max', 'precipitation_sum')
    return [{'date': d, 'code': code, 'high_c': hi, 'low_c': lo, 'rain_pct': pct, 'rain_mm': mm}
            for d, code, hi, lo, pct, mm in zip(daily['time'], *(daily[k] for k in keys))]


def current(station, now=None, session=None):
    """The station's forecast from today on, refreshing it first if it's due.
    Returns [] when there's nothing worth showing."""
    if not settings.FORECAST_URL or not station.forecast_enabled or station.latitude is None or station.longitude is None:
        return []
    now = now or timezone.now()
    stored, _ = StationForecast.objects.get_or_create(station=station)
    due = stored.fetched_at is None or now - stored.fetched_at >= REFRESH
    retry_ok = stored.attempted_at is None or now - stored.attempted_at >= RETRY
    if due and retry_ok:
        # Claim the attempt first so concurrent page loads don't all call the service.
        if StationForecast.objects.filter(pk=stored.pk, attempted_at=stored.attempted_at).update(attempted_at=now):
            try:
                days = fetch(station, session)
            except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
                log.warning('Forecast for %s failed: %s', station, exc)
                StationForecast.objects.filter(pk=stored.pk).update(error=str(exc)[:200])
            else:
                StationForecast.objects.filter(pk=stored.pk).update(days=days, fetched_at=now, error='')
            stored.refresh_from_db()
    if stored.fetched_at is None or now - stored.fetched_at > STALE:
        return []
    today = now.astimezone(station.tzinfo).date().isoformat()
    return [d for d in stored.days if d['date'] >= today]


def display(days, prefs, today):
    """Template-ready days in the viewer's units."""
    out = []
    for d in days:
        date = dt.date.fromisoformat(d['date'])
        label, icon = describe(d['code'])
        rain = prefs.r(d['rain_mm'])
        out.append({
            'day': 'Today' if date == today else date.strftime('%a'), 'date': date, 'label': label, 'icon': icon,
            'high_c': d['high_c'], 'low_c': d['low_c'], 'rain_pct': d['rain_pct'],
            # Hide a trace: under 0.01 in / 0.1 mm says nothing useful on a small tile.
            'rain_mm': d['rain_mm'] if d['rain_mm'] and rain >= (0.01 if prefs.rain == 'in' else 0.1) else None,
        })
    return out
