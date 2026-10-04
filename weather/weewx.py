"""Import history from a WeeWX archive database (SQLite or MySQL).

WeeWX keeps one row per archive interval in its `archive` table, stamped with
the END of the interval (`dateTime`, epoch seconds) like WS4Free, in one of
three unit systems recorded per row (`usUnits`):

    1  US        °F, inHg, mph,  in,  in/h, miles
    16 METRIC    °C, mbar, km/h, cm,  cm/h, km
    17 METRICWX  °C, mbar, m/s,  mm,  mm/h, km

Rain is the amount that fell in the row's interval (not a running total), so
imported rows keep it as `rain_mm` directly (see weather.ingest.rain.is_direct).
Lightning strikes per interval become a daily running count, the form the
sensor catalog uses. Extra sensors are stored in `extra` under the catalog's
upload names (weather.sensors), so they appear like any console's.

This reads WeeWX's own database, so it uses plain SQL against it; WS4Free's
database is still only written through the ORM.
"""
import bisect
import datetime as dt
import sqlite3
from dataclasses import dataclass, field

from . import units

US, METRIC, METRICWX = 1, 16, 17
_KMH = lambda v: v / 3.6                     # noqa: E731
_IDENT = lambda v: v                         # noqa: E731

# Per unit system: (temperature → °C, pressure → hPa, speed → m/s, rain → mm, distance → km)
_CONVERT = {
    US: (units.f_to_c, units.inhg_to_hpa, units.mph_to_ms, units.in_to_mm, lambda v: v * 1.609344),
    METRIC: (_IDENT, _IDENT, _KMH, lambda v: v * 10.0, _IDENT),
    METRICWX: (_IDENT, _IDENT, _IDENT, _IDENT, _IDENT),
}

# WeeWX column → (Observation column, quantity)
COLUMNS = {
    'outTemp': ('temp_c', 'temp'), 'dewpoint': ('dewpoint_c', 'temp'), 'inTemp': ('temp_in_c', 'temp'),
    'outHumidity': ('humidity', None), 'inHumidity': ('humidity_in', None),
    'barometer': ('pressure_rel_hpa', 'pressure'), 'pressure': ('pressure_abs_hpa', 'pressure'),
    'windSpeed': ('wind_speed_ms', 'speed'), 'windGust': ('wind_gust_ms', 'speed'), 'windDir': ('wind_dir_deg', None),
    'rain': ('rain_mm', 'rain'), 'rainRate': ('rain_rate_mmh', 'rain'),
    'radiation': ('solar_wm2', None), 'UV': ('uv_index', None),
}
# WeeWX column patterns → extra key (catalog name) and quantity. Temperatures are
# stored in °F under the catalog's *f names whatever the archive's units.
_EXTRA_NUMBERED = (
    ('extraTemp', 'temp{n}f', 'temp_f'), ('extraHumid', 'humidity{n}', None),
    ('soilTemp', 'soiltemp{n}f', 'temp_f'), ('soilMoist', 'soiltens{n}', None),
)
_EXTRA_PLAIN = {'pm2_5': ('pm25', None), 'pm10_0': ('AqPM10', None), 'co2': ('co2', None),
                'lightning_distance': ('lightning', 'distance')}
# Bookkeeping and derived columns that are expected and deliberately not imported.
_SKIP = {'dateTime', 'usUnits', 'interval', 'altimeter', 'windGustDir', 'heatindex', 'windchill', 'appTemp',
         'humidex', 'cloudbase', 'ET', 'maxSolarRad', 'rxCheckPercent', 'lightning_strike_count'}


def _extra_name(column):
    if column in _EXTRA_PLAIN:
        return _EXTRA_PLAIN[column]
    for prefix, template, quantity in _EXTRA_NUMBERED:
        if column.startswith(prefix) and column[len(prefix):].isdigit():
            return template.format(n=column[len(prefix):]), quantity
    if column.endswith('BatteryStatus'):
        return column, None                 # 0 = OK, 1 = low (weather.sensors.low_batteries)
    return None


def plan(columns):
    """(mapped, extras, ignored) for a WeeWX archive's column names."""
    mapped = [c for c in columns if c in COLUMNS]
    extras = [c for c in columns if c not in COLUMNS and _extra_name(c)]
    ignored = [c for c in columns if c not in COLUMNS and c not in extras and c not in _SKIP]
    return mapped, extras, ignored


@dataclass
class Converter:
    """Turns archive rows into Observation kwargs. Keeps the running daily
    lightning count across rows, so feed it rows in time order."""
    tzinfo: object
    _strikes: tuple = field(default=(None, 0))

    def convert(self, row):
        unit_system = row.get('usUnits') or US
        if unit_system not in _CONVERT:
            raise ValueError(f'unknown WeeWX unit system {unit_system}')
        to_c, to_hpa, to_ms, to_mm, to_km = _CONVERT[unit_system]
        quantity = {'temp': to_c, 'pressure': to_hpa, 'speed': to_ms, 'rain': to_mm, 'distance': to_km,
                    'temp_f': lambda v: units.c_to_f(to_c(v)), None: _IDENT}
        when = dt.datetime.fromtimestamp(int(row['dateTime']), dt.UTC)
        values, extra = {}, {}
        for column, value in row.items():
            if value is None:
                continue
            if column in COLUMNS:
                target, q = COLUMNS[column]
                values[target] = float(quantity[q](float(value)))
            else:
                named = _extra_name(column)
                if named:
                    key, q = named
                    extra[key] = round(float(quantity[q](float(value))), 3)
        if values.get('wind_dir_deg') == 360:
            values['wind_dir_deg'] = 0.0
        if 'dewpoint_c' not in values:
            dp = units.dewpoint_c(values.get('temp_c'), values.get('humidity'))
            if dp is not None:
                values['dewpoint_c'] = dp
        strikes = row.get('lightning_strike_count')
        if strikes is not None:
            day = when.astimezone(self.tzinfo).date()
            last_day, count = self._strikes
            count = (count if day == last_day else 0) + int(strikes)
            self._strikes = (day, count)
            extra['lightning_day'] = count
        interval_s = int(row.get('interval') or 5) * 60
        return when, interval_s, values, extra


class ArchiveReader:
    """Rows of a WeeWX `archive` table, oldest first, in pages."""

    def __init__(self, source):
        self.source = source
        if source.startswith(('mysql://', 'mysql2://')):
            import environ
            conf = environ.Env.db_url_config(source)
            try:
                import MySQLdb as driver
            except ImportError:                                   # pragma: no cover
                import pymysql as driver
            self.conn = driver.connect(host=conf.get('HOST') or 'localhost', port=int(conf.get('PORT') or 3306),
                                       user=conf.get('USER') or '', passwd=conf.get('PASSWORD') or '',
                                       db=conf['NAME'], charset='utf8mb4')
            self.mark = '%s'
        else:
            self.conn = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
            self.mark = '?'

    def _query(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql.replace('?', self.mark), params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def columns(self):
        cur = self.conn.cursor()
        cur.execute('SELECT * FROM archive LIMIT 1')
        return [d[0] for d in cur.description]

    def span(self, since=None, until=None):
        where, params = self._where(since, until)
        row = self._query(f'SELECT COUNT(*) AS n, MIN(dateTime) AS first, MAX(dateTime) AS last FROM archive{where}', params)[0]
        return row['n'], row['first'], row['last']

    def _where(self, since, until, after=None):
        clauses, params = [], []
        for op, value in (('>=', since), ('<=', until), ('>', after)):
            if value is not None:
                clauses.append(f'dateTime {op} ?')
                params.append(value)
        return (' WHERE ' + ' AND '.join(clauses)) if clauses else '', params

    def pages(self, since=None, until=None, size=5000):
        after = None
        while True:
            where, params = self._where(since, until, after)
            rows = self._query(f'SELECT * FROM archive{where} ORDER BY dateTime LIMIT {int(size)}', params)
            if not rows:
                return
            yield rows
            after = rows[-1]['dateTime']

    def close(self):
        self.conn.close()


def overlaps_existing(existing, when, interval_s):
    """Whether (when − interval, when] overlaps any existing row. `existing` is a
    sorted list of (end, start) pairs."""
    start = when - dt.timedelta(seconds=interval_s)
    i = bisect.bisect_right(existing, (start, dt.datetime.max.replace(tzinfo=dt.UTC)))
    # the first existing row ending after our start overlaps if it begins before our end
    return i < len(existing) and existing[i][1] < when
