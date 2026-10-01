"""Turn console uploads and Ambient API records into SI readings.

Three inputs share most field names (they all descend from the Wunderground
upload protocol) but differ in the details that matter:

  * Ambient API records and Ambient-protocol pushes: ``hourlyrainin`` is the
    rain RATE in in/h; ``dateutc`` is epoch ms (API) or 'YYYY-MM-DD HH:MM:SS'.
  * Ecowitt-protocol pushes: the rate is ``rainratein``, and ``hourlyrainin`` is
    the rain that fell in the last hour — not a rate. ``PASSKEY`` is an MD5 of the
    MAC rather than the MAC itself.
  * Wunderground-protocol pushes: ``baromin``/``absbaromin``, ``dewptf``,
    ``indoortempf``, ``UV``; ``rainin`` is last-hour rain, with no rate at all.

Everything not mapped to an Observation column is kept, unconverted, in
``extra`` (extra temperature probes, soil moisture, leaf wetness, batteries…),
minus identifiers and fields that are derived elsewhere.
"""
import datetime as dt
import math
from dataclasses import dataclass, field

from .. import units

# Keys never stored in `extra`: identifiers/credentials, timestamps, and
# console-side aggregates we compute ourselves.
_DROP_KEYS = {
    'PASSKEY', 'passkey', 'ID', 'PASSWORD', 'MAC', 'mac', 'macAddress',
    'dateutc', 'date', 'time', 'tz', 'loc', 'lastRain', 'action', 'realtime', 'rtfreq',
    'softwaretype', 'stationtype', 'model', 'freq', 'interval', 'runtime', 'heap',
    'feelsLike', 'feelsLikein', 'dewPointin', 'windchillf', 'heatindexf',
    'weeklyrainin', 'monthlyrainin', 'yearlyrainin', 'hourlyrainin', 'rainin', 'rainratein',
}

# (upload key, Observation column, converter) — first key present wins per column.
_COMMON = [
    ('tempf', 'temp_c', units.f_to_c),
    ('humidity', 'humidity', float),
    ('dewPoint', 'dewpoint_c', units.f_to_c),       # Ambient API
    ('dewptf', 'dewpoint_c', units.f_to_c),         # Wunderground protocol
    ('baromrelin', 'pressure_rel_hpa', units.inhg_to_hpa),
    ('baromin', 'pressure_rel_hpa', units.inhg_to_hpa),
    ('baromabsin', 'pressure_abs_hpa', units.inhg_to_hpa),
    ('absbaromin', 'pressure_abs_hpa', units.inhg_to_hpa),
    ('windspeedmph', 'wind_speed_ms', units.mph_to_ms),
    ('windgustmph', 'wind_gust_ms', units.mph_to_ms),
    ('winddir', 'wind_dir_deg', float),
    ('eventrainin', 'rain_event_mm', units.in_to_mm),
    ('dailyrainin', 'rain_daily_mm', units.in_to_mm),
    ('totalrainin', 'rain_counter_mm', units.in_to_mm),
    ('solarradiation', 'solar_wm2', float),
    ('uv', 'uv_index', float),
    ('UV', 'uv_index', float),
    ('tempinf', 'temp_in_c', units.f_to_c),
    ('indoortempf', 'temp_in_c', units.f_to_c),
    ('humidityin', 'humidity_in', float),
    ('indoorhumidity', 'humidity_in', float),
]
_AMBIENT_RATE = [('hourlyrainin', 'rain_rate_mmh', units.in_to_mm)]
_ECOWITT_RATE = [('rainratein', 'rain_rate_mmh', units.in_to_mm)]

# Physically plausible SI ranges; anything outside is a sensor glitch or a
# "no sensor" sentinel (e.g. -9999) and is stored as NULL.
_VALID_RANGE = {
    'temp_c': (-90, 65), 'dewpoint_c': (-100, 40), 'temp_in_c': (-40, 70),
    'humidity': (0, 100), 'humidity_in': (0, 100),
    'pressure_rel_hpa': (850, 1100), 'pressure_abs_hpa': (500, 1100),
    'wind_speed_ms': (0, 115), 'wind_gust_ms': (0, 115), 'wind_dir_deg': (0, 360),
    'rain_rate_mmh': (0, 2000), 'rain_event_mm': (0, 5000), 'rain_daily_mm': (0, 2000),
    'rain_counter_mm': (0, 1e7), 'solar_wm2': (0, 2000), 'uv_index': (0, 25),
}


class ParseError(ValueError):
    pass


@dataclass
class Reading:
    timestamp: dt.datetime                    # aware, UTC
    values: dict = field(default_factory=dict)   # Observation column -> SI float (None dropped)
    extra: dict = field(default_factory=dict)
    passkey: str = ''                         # PASSKEY/ID as sent, for station verification
    clock_adjusted: bool = False              # console timestamp was implausible; server time used


def _number(raw):
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _map(params, table, values):
    for key, column, convert in table:
        if column in values:
            continue
        number = _number(params.get(key))
        if number is None:
            continue
        si = convert(number)
        lo, hi = _VALID_RANGE.get(column, (-math.inf, math.inf))
        if lo <= si <= hi:
            values[column] = si


def _extras(params, mapped_keys):
    extra = {}
    for key, raw in params.items():
        if key in _DROP_KEYS or key in mapped_keys:
            continue
        number = _number(raw)
        extra[key] = number if number is not None else str(raw)[:100]
    return extra


def _finish(values):
    if 'dewpoint_c' not in values:
        dp = units.dewpoint_c(values.get('temp_c'), values.get('humidity'))
        if dp is not None:
            values['dewpoint_c'] = dp
    if values.get('wind_dir_deg') == 360:
        values['wind_dir_deg'] = 0.0
    return values


def parse_dateutc(raw, now, max_skew_s):
    """Console timestamp → aware UTC datetime.

    Accepts 'now', 'YYYY-MM-DD HH:MM:SS' (URL '+' already decoded to a space, but
    tolerated either way) and epoch seconds/milliseconds. Returns (timestamp,
    adjusted): a missing, malformed or implausible value (console clock off by more
    than max_skew_s, typically a console that lost its time after a power cut)
    falls back to the server's receive time with adjusted=True.
    """
    if raw is None or str(raw).strip().lower() in ('', 'now'):
        return now, False
    text = str(raw).strip().replace('+', ' ')
    parsed = None
    number = _number(text)
    if number is not None:
        parsed = dt.datetime.fromtimestamp(number / 1000 if number > 1e11 else number, dt.UTC)
    else:
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M'):
            try:
                parsed = dt.datetime.strptime(text, fmt).replace(tzinfo=dt.UTC)
                break
            except ValueError:
                continue
    if parsed is None or abs((parsed - now).total_seconds()) > max_skew_s:
        return now, True
    return parsed, False


def _parse_push(params, rate_table, now, max_skew_s, passkey_keys):
    params = {k: v for k, v in params.items() if v is not None}
    timestamp, adjusted = parse_dateutc(params.get('dateutc'), now, max_skew_s)
    values = {}
    _map(params, _COMMON + rate_table, values)
    if not values:
        raise ParseError('no recognised weather fields')
    mapped = {key for key, _, _ in _COMMON + rate_table}
    passkey = next((str(params[k]).strip() for k in passkey_keys if params.get(k)), '')
    return Reading(timestamp, _finish(values), _extras(params, mapped), passkey, adjusted)


def parse_ambient_push(params, now, max_skew_s=900):
    """Ambient-protocol or Wunderground-protocol custom-server upload (GET params)."""
    return _parse_push(params, _AMBIENT_RATE, now, max_skew_s, ('PASSKEY', 'MAC', 'ID'))


def parse_ecowitt_push(params, now, max_skew_s=900):
    """Ecowitt-protocol custom-server upload (form POST)."""
    return _parse_push(params, _ECOWITT_RATE, now, max_skew_s, ('PASSKEY',))


def parse_api_record(record):
    """One record from GET /v1/devices/{mac}. Its dateutc (epoch ms) is the END
    of the console's 5-minute archive interval — the record stamped 00:00 holds
    the rain that fell 23:55–00:00 — which is the convention Observation uses."""
    try:
        timestamp = dt.datetime.fromtimestamp(int(record['dateutc']) / 1000, dt.UTC)
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError(f'record has no usable dateutc: {exc}') from exc
    values = {}
    _map(record, _COMMON + _AMBIENT_RATE, values)
    mapped = {key for key, _, _ in _COMMON + _AMBIENT_RATE}
    return Reading(timestamp, _finish(values), _extras(record, mapped))
