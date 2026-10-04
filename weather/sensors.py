"""Extra sensors: the add-on probes a console reports besides the main outdoor
suite — extra temperature/humidity channels, soil, leaf wetness, air quality,
CO₂, lightning, leak detectors and batteries.

Ingest keeps them, unconverted, in `Observation.extra` / `LatestReading.extra`
under the console's own upload names. This module is the catalog that knows
what those names mean across the protocols WS4Free accepts:

  * Ambient Weather (API and custom-server push): temp1f, soilhum1, pm25,
    lightning_day, lightning_distance (miles), leak1, batt1 (1 = OK)…
  * Ecowitt (custom-server push): temp1f or temp1c, soilmoisture1, tf_ch1,
    pm25_ch1, lightning_num, lightning (km), leak_ch1, batt1 (1 = LOW)…
  * Wunderground protocol (e.g. WeeWX): soilmoisture, soiltempf, leafwetness,
    AqPM2.5 — the first channel has no number.

`describe(key)` turns an upload name into a Sensor (kind, channel, label, SI
value); `Station.sensors` remembers which ones a station has, with the names
and public/private choice its owner gave them.
"""
import datetime as dt
import re
from dataclasses import dataclass

from . import units

# kind → (label, unit kind, chartable, public by default)
#   Temperature and humidity channels are often indoors (a bedroom, a freezer), and
#   indoor air quality says when someone is home, so they start private.
KINDS = {
    'temperature': ('Temperature', 'temp', True, False),
    'humidity': ('Humidity', 'pct', True, False),
    'soil_temp': ('Soil temperature', 'temp', True, True),
    'soil_moisture': ('Soil moisture', 'pct', True, True),
    'soil_tension': ('Soil tension', 'cb', True, True),
    'leaf_wetness': ('Leaf wetness', 'pct', True, True),
    'pm25': ('PM2.5', 'pm', True, True),
    'pm10': ('PM10', 'pm', True, True),
    'co2': ('CO₂', 'ppm', True, False),
    'lightning': ('Lightning', 'count', True, True),
    'leak': ('Leak', 'leak', False, False),
}
KIND_ORDER = list(KINDS)


def _ident(v):
    return v


_F = units.f_to_c
_MI_TO_KM = 1.609344


@dataclass(frozen=True)
class Spec:
    pattern: re.Pattern
    kind: str
    label: str             # may contain {n}
    convert: object = _ident
    private: bool | None = None   # overrides the kind's default


def _s(regex, kind, label, convert=_ident, private=None):
    return Spec(re.compile(f'^{regex}$'), kind, label, convert, private)


SPECS = [
    # Extra temperature / humidity channels (Ambient WH31E, Ecowitt WH31; °F or °C upload).
    _s(r'temp(\d+)f', 'temperature', 'Temperature {n}', _F),
    _s(r'temp(\d+)c', 'temperature', 'Temperature {n}'),
    _s(r'humidity(\d+)', 'humidity', 'Humidity {n}'),
    _s(r'tf_co2', 'temperature', 'Air quality monitor temperature', _F),
    _s(r'tf_co2c', 'temperature', 'Air quality monitor temperature'),
    _s(r'humi_co2', 'humidity', 'Air quality monitor humidity'),
    _s(r'pm_in_temp_aqin', 'temperature', 'AQIN temperature', _F),
    _s(r'pm_in_humidity_aqin', 'humidity', 'AQIN humidity'),
    # Soil temperature: Ambient soiltempNf, Ecowitt WN34 tf_chN (also used for water), Wunderground soiltempf.
    _s(r'soiltemp(\d+)f', 'soil_temp', 'Soil temperature {n}', _F),
    _s(r'soiltempf', 'soil_temp', 'Soil temperature 1', _F),
    _s(r'tf_ch(\d+)', 'soil_temp', 'Probe temperature {n}', _F),
    _s(r'tf_ch(\d+)c', 'soil_temp', 'Probe temperature {n}'),
    # Soil moisture (%): Ambient soilhumN, Ecowitt soilmoistureN, Wunderground soilmoisture[N].
    _s(r'soilhum(\d+)', 'soil_moisture', 'Soil moisture {n}'),
    _s(r'soilmoisture(\d+)', 'soil_moisture', 'Soil moisture {n}'),
    _s(r'soilmoisture', 'soil_moisture', 'Soil moisture 1'),
    # Soil water tension (centibars; higher = drier): Ambient soiltensN, WeeWX soilMoistN (Davis Watermark).
    _s(r'soiltens(\d+)', 'soil_tension', 'Soil tension {n}'),
    # Leaf wetness (%).
    _s(r'leafwetness(\d+)', 'leaf_wetness', 'Leaf wetness {n}'),
    _s(r'leafwetness_ch(\d+)', 'leaf_wetness', 'Leaf wetness {n}'),
    _s(r'leafwetness', 'leaf_wetness', 'Leaf wetness 1'),
    # Particulates (µg/m³).
    _s(r'pm25', 'pm25', 'Outdoor PM2.5'),
    _s(r'AqPM2\.5', 'pm25', 'Outdoor PM2.5'),
    _s(r'pm25_ch(\d+)', 'pm25', 'PM2.5 {n}'),
    _s(r'pm25_in', 'pm25', 'Indoor PM2.5', private=True),
    _s(r'pm25_in_aqin', 'pm25', 'Indoor PM2.5 (AQIN)', private=True),
    _s(r'pm25_co2', 'pm25', 'Indoor PM2.5 (air quality monitor)', private=True),
    _s(r'AqPM10', 'pm10', 'Outdoor PM10'),
    _s(r'pm10_in_aqin', 'pm10', 'Indoor PM10 (AQIN)', private=True),
    _s(r'pm10_co2', 'pm10', 'Indoor PM10 (air quality monitor)', private=True),
    # CO₂ (ppm).
    _s(r'co2', 'co2', 'CO₂'),
    _s(r'co2in', 'co2', 'Console CO₂'),
    _s(r'co2_in_aqin', 'co2', 'Indoor CO₂ (AQIN)'),
    # Lightning: strikes so far today (both brands reset at midnight).
    _s(r'lightning_day', 'lightning', 'Lightning strikes'),
    _s(r'lightning_num', 'lightning', 'Lightning strikes'),
    # Leak detectors: Ambient 0 = dry, 1 = leak, 2 = offline; Ecowitt 0 = dry, 1 = leak.
    _s(r'leak(\d+)', 'leak', 'Leak sensor {n}'),
    _s(r'leak_ch(\d+)', 'leak', 'Leak sensor {n}'),
]

# 24-hour averages the consoles compute for the matching PM2.5 key (used for the
# air-quality category, which the US EPA defines on a 24-hour mean).
PM25_24H = {
    'pm25': 'pm25_24h', 'pm25_in': 'pm25_in_24h', 'pm25_in_aqin': 'pm25_in_24h_aqin',
    'pm25_co2': 'pm25_24h_co2', **{f'pm25_ch{n}': f'pm25_avg_24h_ch{n}' for n in range(1, 9)},
}


@dataclass(frozen=True)
class Sensor:
    key: str
    kind: str
    channel: int
    default_label: str
    convert: object
    private_default: bool

    @property
    def kind_label(self):
        return KINDS[self.kind][0]

    @property
    def unit_kind(self):
        return KINDS[self.kind][1]

    @property
    def chartable(self):
        return KINDS[self.kind][2]

    def si(self, raw):
        """Upload value → SI float (None if not a number)."""
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value != value or value in (float('inf'), float('-inf')):
            return None
        return self.convert(value)


_CACHE = {}


def describe(key):
    """Sensor for an upload key, or None if it isn't an extra sensor."""
    if key in _CACHE:
        return _CACHE[key]
    found = None
    for spec in SPECS:
        m = spec.pattern.match(key)
        if m:
            n = int(m.group(1)) if m.groups() else 1
            private = (not KINDS[spec.kind][3]) if spec.private is None else spec.private
            found = Sensor(key, spec.kind, n, spec.label.format(n=n), spec.convert, private)
            break
    _CACHE[key] = found
    return found


def sensor_keys(extra):
    """Catalogued sensor keys present (with a value) in an extra dict."""
    return [k for k, v in (extra or {}).items() if v not in (None, '') and describe(k) is not None]


def detect(station, extra):
    """Add sensors seen in `extra` that the station doesn't know yet. Returns the
    new keys (empty almost always — this runs on every upload)."""
    new = [k for k in sensor_keys(extra) if k not in station.sensors]
    if not new:
        return []
    from .ingest.store import mark_dirty
    from .models import Observation, Station
    sensors = dict(station.sensors)
    for key in new:
        s = describe(key)
        sensors[key] = {'name': '', 'public': not s.private_default}
    station.sensors = sensors
    Station.objects.filter(pk=station.pk).update(sensors=sensors)
    # A sensor that's new to WS4Free may have years of history already stored
    # (an upgrade from a version without sensor summaries): summarise it all once.
    first = Observation.objects.filter(station=station).order_by('timestamp').values_list('timestamp', flat=True).first()
    if first is not None:
        mark_dirty(station.pk, first)
    return new


def station_sensors(station, include_private=False):
    """[(Sensor, display name)] in kind then channel order."""
    out = []
    for key, conf in (station.sensors or {}).items():
        s = describe(key)
        if s is None or not (include_private or conf.get('public')):
            continue
        out.append((s, conf.get('name') or s.default_label))
    return sorted(out, key=lambda p: (KIND_ORDER.index(p[0].kind), p[0].channel, p[0].key))


# ── Display values ───────────────────────────────────────────────────────────

# US EPA PM2.5 AQI categories (2024 revision), on the 24-hour mean, µg/m³.
PM25_CATEGORIES = ((9.0, 'Good', 'good'), (35.4, 'Moderate', 'warn'), (55.4, 'Unhealthy for sensitive groups', 'warm'),
                   (125.4, 'Unhealthy', 'bad'), (225.4, 'Very unhealthy', 'bad'), (float('inf'), 'Hazardous', 'bad'))


def pm25_category(value):
    if value is None:
        return None
    for upper, label, tone in PM25_CATEGORIES:
        if value <= upper:
            return {'label': label, 'tone': tone}


def format_value(sensor, si, prefs):
    """Text for a sensor's current value in the viewer's units."""
    if si is None:
        return '—'
    k = sensor.unit_kind
    if k == 'temp':
        return f'{prefs.t(si):.1f}{prefs.label("temp")}'
    if k == 'cb':
        return f'{si:.0f} cb'
    if k == 'pct':
        return f'{si:.0f}%'
    if k == 'pm':
        return f'{si:.1f} µg/m³'
    if k == 'ppm':
        return f'{si:.0f} ppm'
    if k == 'count':
        return f'{si:.0f}'
    if k == 'leak':
        return {0: 'Dry', 1: 'Leak!', 2: 'Offline'}.get(int(si), '—')
    return f'{si:g}'


def lightning_last(extra, tzinfo):
    """(distance_km, when) of the last strike, from either brand's keys."""
    distance = None
    for key, factor in (('lightning', 1.0), ('lightning_distance', _MI_TO_KM), ('lightning_mi', _MI_TO_KM)):
        try:
            distance = float(extra[key]) * factor
            break
        except (KeyError, TypeError, ValueError):
            continue
    when = None
    raw = extra.get('lightning_time')
    if raw not in (None, ''):
        try:
            number = float(raw)
            when = dt.datetime.fromtimestamp(number / 1000 if number > 1e11 else number, dt.UTC)
        except (TypeError, ValueError):
            try:
                when = dt.datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            except ValueError:
                when = None
    if when is not None:
        when = when.astimezone(tzinfo)
    return distance, when


def distance_text(km, prefs):
    if km is None:
        return None
    if prefs.wind == 'mph' or prefs.rain == 'in':
        return f'{km / _MI_TO_KM:.0f} mi'
    return f'{km:.0f} km'


# ── Batteries ────────────────────────────────────────────────────────────────

_AMBIENT_OK_IS_1 = re.compile(r'^(battout|battin|batt\d+|battsm\d+|batt_co2|batt_25|batt_cellgateway)$')
_AMBIENT_LOW_IS_1 = re.compile(r'^(batt_lightning|batleak\d+)$')
_ECOWITT_LOW_IS_1 = re.compile(r'^(wh25batt|wh26batt|wh65batt|batt\d+)$')
_ECOWITT_VOLTS = re.compile(r'^(wh40batt|wh68batt|soilbatt\d+|tf_batt\d+|leaf_batt\d+|ldsbatt\d+|wn20batt|bgtbatt)$')
_ECOWITT_LEVEL = re.compile(r'^(pm25batt\d+|leakbatt\d+|co2_batt|wh57batt)$')

BATTERY_NAMES = {
    'battout': 'Outdoor sensor', 'wh65batt': 'Outdoor sensor', 'wh26batt': 'Outdoor sensor',
    'battin': 'Indoor sensor', 'wh25batt': 'Indoor sensor', 'batt_lightning': 'Lightning detector',
    'wh57batt': 'Lightning detector', 'wh40batt': 'Rain gauge', 'wh68batt': 'Wind sensor',
    'batt_co2': 'CO₂ monitor', 'co2_batt': 'Air quality monitor', 'batt_25': 'PM2.5 sensor',
    'batt_cellgateway': 'Cellular gateway',
}


LOW_VOLTS = 1.2         # an Ecowitt AA-cell sensor below this needs a new battery

# Sensors whose battery is reported in volts (Ecowitt), and that battery's key.
_VOLT_BATTERIES = [(re.compile(r'^soilmoisture(\d+)$'), 'soilbatt{}'), (re.compile(r'^tf_ch(\d+)c?$'), 'tf_batt{}'),
                   (re.compile(r'^leafwetness_ch(\d+)$'), 'leaf_batt{}')]


def battery_volts(key, extra):
    """The battery voltage reported for sensor `key`, or None if it has none."""
    for pattern, battery in _VOLT_BATTERIES:
        m = pattern.match(key)
        if m:
            try:
                volts = float((extra or {}).get(battery.format(m.group(1))))
            except (TypeError, ValueError):
                return None
            return volts if volts > 0 else None
    return None


def low_batteries(extra, source):
    """Names of sensors whose battery the console reports as low. The same key can
    mean opposite things per brand (Ambient batt1 = 1 is OK, Ecowitt batt1 = 1 is
    low), so the upload's source decides."""
    ecowitt = source == 'ecowitt_push'
    weewx = source == 'weewx_import'
    low = []
    for key, raw in (extra or {}).items():
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if weewx:
            is_low = key.endswith('BatteryStatus') and value == 1
        elif ecowitt:
            is_low = ((_ECOWITT_LOW_IS_1.match(key) and value == 1)
                      or (_ECOWITT_VOLTS.match(key) and 0 < value < LOW_VOLTS)
                      or (_ECOWITT_LEVEL.match(key) and value <= 1))
        else:
            # Ecowitt's voltage and level keys exist under no other brand's name, so
            # they mean the same whatever the row's source (e.g. a sensor gateway's).
            is_low = ((_AMBIENT_OK_IS_1.match(key) and value == 0)
                      or (_AMBIENT_LOW_IS_1.match(key) and value == 1)
                      or (_ECOWITT_VOLTS.match(key) and 0 < value < LOW_VOLTS)
                      or (_ECOWITT_LEVEL.match(key) and value <= 1))
        if is_low:
            low.append(BATTERY_NAMES.get(key) or WEEWX_BATTERY_NAMES.get(key) or _battery_label(key))
    return sorted(set(low))


# ── Sensor gateways ──────────────────────────────────────────────────────────

# Batteries of extra sensors (not of the main outdoor array, rain gauge, wind
# sensor or the gateway itself), in either brand's naming.
_EXTRA_BATTERIES = re.compile(r'^(batt\d+|battsm\d+|batleak\d+|batt_co2|batt_25|batt_lightning|soilbatt\d+|tf_batt\d+|'
                              r'leaf_batt\d+|pm25batt\d+|leakbatt\d+|co2_batt|wh57batt|ldsbatt\d+)$')
_CHANNEL_BATTERY = re.compile(r'^batt\d+$')
COMPANION_KEYS = set(PM25_24H.values()) | {'lightning', 'lightning_time', 'lightning_distance', 'lightning_mi'}


def gateway_extra(extra, upload_source, station_source):
    """What a sensor gateway's upload may add to its station: extra sensors, their
    batteries and companion values only. Channel batteries (batt1…) mean opposite
    things per brand (Ambient 1 = OK, Ecowitt 1 = low); they're rewritten to the
    station's own console's meaning, so its rows read them the same way."""
    kept = {k: v for k, v in (extra or {}).items()
            if v not in (None, '') and (describe(k) is not None or _EXTRA_BATTERIES.match(k) or k in COMPANION_KEYS)}
    if (upload_source == 'ecowitt_push') != (station_source == 'ecowitt'):
        for key, value in kept.items():
            if _CHANNEL_BATTERY.match(key):
                try:
                    kept[key] = 1 - int(float(value))
                except (TypeError, ValueError):
                    pass
    return kept


WEEWX_BATTERY_NAMES = {'outTempBatteryStatus': 'Outdoor sensor', 'inTempBatteryStatus': 'Indoor sensor',
                       'windBatteryStatus': 'Wind sensor', 'rainBatteryStatus': 'Rain gauge',
                       'txBatteryStatus': 'Transmitter', 'uvBatteryStatus': 'UV sensor'}


def _battery_label(key):
    m = re.search(r'(\d+)$', key)
    n = m.group(1) if m else ''
    for prefix, name in (('soilbatt', 'Soil moisture sensor'), ('battsm', 'Soil moisture sensor'),
                         ('tf_batt', 'Probe'), ('leaf_batt', 'Leaf wetness sensor'), ('pm25batt', 'PM2.5 sensor'),
                         ('leakbatt', 'Leak sensor'), ('batleak', 'Leak sensor'), ('ldsbatt', 'Depth sensor'),
                         ('batt', 'Sensor')):
        if key.startswith(prefix):
            return f'{name} {n}'.strip()
    return key


# ── Summaries (rollups) ──────────────────────────────────────────────────────

def summarise(rows, sensor_keys_):
    """rows: iterable of (interval_s, extra). Returns {key: [mean, min, max]} in SI,
    the mean weighted by interval like every other rollup average."""
    acc = {}
    wanted = set(sensor_keys_)
    for interval_s, extra in rows:
        for key in wanted.intersection(extra or ()):
            s = describe(key)
            value = s.si(extra[key]) if s else None
            if value is None:
                continue
            a = acc.get(key)
            if a is None:
                acc[key] = [value * interval_s, interval_s, value, value]
            else:
                a[0] += value * interval_s
                a[1] += interval_s
                a[2] = min(a[2], value)
                a[3] = max(a[3], value)
    return {k: [round(a[0] / a[1], 3), round(a[2], 3), round(a[3], 3)] for k, a in acc.items()}


# ── Dashboard ────────────────────────────────────────────────────────────────

def dashboard_groups(station, latest, today_extra, prefs, include_private=False, exclude=(), batteries=False):
    """Live tiles for the dashboard's "More sensors" section, grouped by kind:
    [{'label', 'items': [{'name', 'value', 'detail', 'tone', 'private', 'battery'}]}].
    `batteries` adds each sensor's battery voltage where it reports one (owner only)."""
    extra = {k: v for k, v in ((latest.extra if latest else None) or {}).items() if k not in exclude}
    groups = {}
    for sensor, name in station_sensors(station, include_private):
        raw = extra.get(sensor.key)
        si = sensor.si(raw)
        if si is None and sensor.kind != 'lightning':
            continue
        item = {'name': name, 'value': format_value(sensor, si, prefs), 'detail': '', 'tone': '',
                'private': not (station.sensors.get(sensor.key) or {}).get('public'), 'battery': None}
        volts = battery_volts(sensor.key, extra) if batteries else None
        if volts is not None:
            item['battery'] = {'volts': volts, 'low': volts < LOW_VOLTS}
        day = (today_extra or {}).get(sensor.key)
        if sensor.kind in ('temperature', 'soil_temp', 'humidity', 'soil_moisture', 'soil_tension') and day:
            lo, hi = min(day[1], si), max(day[2], si)
            if sensor.unit_kind == 'temp':
                item['detail'] = f'Today {prefs.t(lo):.0f}–{prefs.t(hi):.0f}{prefs.label("temp")}'
            else:
                item['detail'] = f'Today {lo:.0f}–{hi:.0f}' + (' cb' if sensor.unit_kind == 'cb' else '%')
        elif sensor.kind == 'pm25':
            avg = sensor.si(extra.get(PM25_24H.get(sensor.key, '')))
            category = pm25_category(avg if avg is not None else si)
            if category:
                item['tone'] = category['tone']
                item['detail'] = category['label'] + (' · 24-h average' if avg is not None else '')
        elif sensor.kind == 'leak':
            item['tone'] = {'Dry': 'good', 'Leak!': 'bad'}.get(item['value'], 'warn')
        elif sensor.kind == 'lightning':
            item['value'] = f'{si:.0f}' if si is not None else '—'
            km, when = lightning_last(extra, station.tzinfo)
            item['detail'] = 'strikes today'
            if when is not None and latest and (latest.timestamp - when) < dt.timedelta(days=2):
                where = distance_text(km, prefs)
                today = latest.timestamp.astimezone(station.tzinfo).date()
                day = '' if when.date() == today else ('yesterday ' if when.date() == today - dt.timedelta(days=1)
                                                       else f'{when:%a} ')
                item['detail'] += f' · last {day}{when:%-I:%M %p}' + (f', {where} away' if where else '')
        groups.setdefault(sensor.kind, {'label': sensor.kind_label, 'items': []})['items'].append(item)
    return [groups[k] for k in KIND_ORDER if k in groups]
