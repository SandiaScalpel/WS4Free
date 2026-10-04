"""Importing a WeeWX archive (weather.weewx, manage.py import_weewx)."""
import datetime as dt
import io
import sqlite3
import tempfile
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import TestCase

from weather import units, weewx
from weather.ingest.rain import compute_rain_increments
from weather.models import DailyRollup, LatestReading, Observation, Station
from weather.rollups import refresh_station
from weather.sensors import low_batteries

from .helpers import make_station

UTC = dt.UTC
T0 = dt.datetime(2025, 7, 1, 18, 0, tzinfo=UTC)      # noon in Denver
COLUMNS = ('dateTime', 'usUnits', 'interval', 'outTemp', 'outHumidity', 'dewpoint', 'barometer', 'pressure',
           'altimeter', 'windSpeed', 'windGust', 'windDir', 'rain', 'rainRate', 'radiation', 'UV', 'inTemp',
           'extraTemp1', 'soilMoist1', 'leafWet1', 'lightning_strike_count', 'lightning_distance',
           'outTempBatteryStatus', 'pm2_5')


def make_archive(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(f'CREATE TABLE archive ({", ".join(c + (" INTEGER PRIMARY KEY" if c == "dateTime" else " REAL") for c in COLUMNS)})')
    for r in rows:
        conn.execute(f'INSERT INTO archive ({", ".join(r)}) VALUES ({", ".join("?" * len(r))})', list(r.values()))
    conn.commit()
    conn.close()


def us_row(minutes, **v):
    base = {'dateTime': int((T0 + dt.timedelta(minutes=minutes)).timestamp()), 'usUnits': 1, 'interval': 5,
            'outTemp': 86.0, 'outHumidity': 20.0, 'barometer': 30.0, 'pressure': 24.9, 'windSpeed': 10.0,
            'windGust': 15.0, 'windDir': 360.0, 'rain': 0.0, 'rainRate': 0.0, 'radiation': 800.0, 'UV': 7.0,
            'inTemp': 72.0, 'extraTemp1': 50.0, 'soilMoist1': 25.0, 'leafWet1': 3.0,
            'lightning_strike_count': 0.0, 'outTempBatteryStatus': 0.0}
    return {**base, **v}


class ConverterTests(TestCase):
    def test_unit_systems(self):
        c = weewx.Converter(dt.UTC)
        _, _, us, extra = c.convert(us_row(5, rain=0.1, rainRate=1.2, lightning_distance=5.0))
        self.assertAlmostEqual(us['temp_c'], 30.0)
        self.assertAlmostEqual(us['pressure_rel_hpa'], units.inhg_to_hpa(30.0))
        self.assertAlmostEqual(us['wind_speed_ms'], units.mph_to_ms(10.0))
        self.assertEqual(us['wind_dir_deg'], 0.0)
        self.assertAlmostEqual(us['rain_mm'], 2.54)
        self.assertAlmostEqual(us['rain_rate_mmh'], 30.48)
        self.assertAlmostEqual(us['dewpoint_c'], units.dewpoint_c(30.0, 20.0))         # derived when absent
        self.assertEqual((extra['temp1f'], extra['soiltens1'], extra['lightning']), (50.0, 25.0, 8.047))
        self.assertNotIn('leafWet1', extra)
        _, _, metric, _ = c.convert({'dateTime': 1, 'usUnits': 16, 'interval': 10, 'outTemp': 30.0, 'barometer': 1013.0,
                                     'windSpeed': 36.0, 'rain': 0.2})
        self.assertEqual((metric['temp_c'], metric['pressure_rel_hpa'], metric['wind_speed_ms'], metric['rain_mm']),
                         (30.0, 1013.0, 10.0, 2.0))
        when, interval_s, wx, extra = c.convert({'dateTime': 1, 'usUnits': 17, 'windSpeed': 3.0, 'rain': 1.5,
                                                  'extraTemp2': 10.0})
        self.assertEqual((wx['wind_speed_ms'], wx['rain_mm'], interval_s, extra['temp2f']), (3.0, 1.5, 300, 50.0))
        with self.assertRaises(ValueError):
            c.convert({'dateTime': 1, 'usUnits': 99})

    def test_lightning_becomes_a_daily_count(self):
        tz = dt.timezone(dt.timedelta(hours=-6))
        c = weewx.Converter(tz)
        counts = [c.convert({'dateTime': int((dt.datetime(2025, 7, 1, h, tzinfo=tz)).timestamp()), 'usUnits': 1,
                             'lightning_strike_count': n})[3]['lightning_day']
                  for h, n in ((22, 2), (23, 3))] + [
                 c.convert({'dateTime': int(dt.datetime(2025, 7, 2, 1, tzinfo=tz).timestamp()), 'usUnits': 1,
                            'lightning_strike_count': 4})[3]['lightning_day']]
        self.assertEqual(counts, [2, 5, 4])

    def test_plan_and_batteries(self):
        mapped, extras, ignored = weewx.plan(list(COLUMNS) + ['hail', 'windGustDir'])
        self.assertIn('outTemp', mapped)
        self.assertEqual(set(extras), {'extraTemp1', 'soilMoist1', 'lightning_distance', 'outTempBatteryStatus', 'pm2_5'})
        self.assertEqual(set(ignored), {'leafWet1', 'hail'})
        self.assertEqual(low_batteries({'outTempBatteryStatus': 1, 'windBatteryStatus': 0}, 'weewx_import'), ['Outdoor sensor'])

    def test_overlap(self):
        existing = sorted((T0 + dt.timedelta(minutes=m), T0 + dt.timedelta(minutes=m - 5)) for m in (10, 15))
        at = lambda m, iv=300: weewx.overlaps_existing(existing, T0 + dt.timedelta(minutes=m), iv)   # noqa: E731
        self.assertFalse(at(5))          # (0, 5] before (5, 15]
        self.assertTrue(at(10))
        self.assertTrue(at(12, 60))      # one-minute record inside an existing 5-minute one
        self.assertFalse(at(20))
        self.assertTrue(at(20, 600))     # ten-minute record reaching back into (10, 15]


class ImportCommandTests(TestCase):
    def setUp(self):
        self.station = make_station(source=Station.SOURCE_WEEWX, mac_address=None)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = str(Path(self.dir.name) / 'weewx.sdb')
        rows = [us_row(5 * i, rain=0.01 if i in (2, 3) else 0.0, lightning_strike_count=1 if i == 4 else 0,
                       outTempBatteryStatus=1 if i >= 5 else 0) for i in range(1, 9)]
        rows.append(us_row(5 * 12, usUnits=17, outTemp=25.0, barometer=1010.0, pressure=845.0, windSpeed=2.0,
                           windGust=3.0, rain=0.5, rainRate=0.0, inTemp=22.0, extraTemp1=10.0))
        make_archive(self.path, rows)
        # WS4Free already has the 12:30 record (a console push), so the archive's 12:30 row is skipped.
        Observation.objects.create(station=self.station, timestamp=T0 + dt.timedelta(minutes=30), source='ambient_push',
                                   temp_c=31.0, rain_daily_mm=5.0)

    def run_import(self, *args):
        out = io.StringIO()
        call_command('import_weewx', self.station.slug, self.path, *args, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        out = self.run_import('--dry-run')
        self.assertIn('9 records', out)
        self.assertIn('Not imported (no WS4Free equivalent yet): leafWet1', out)
        self.assertEqual(Observation.objects.count(), 1)

    def test_import(self):
        out = self.run_import()
        self.assertIn('Imported 8 records', out)
        self.assertIn('1 skipped', out)
        rows = Observation.objects.filter(source='weewx_import').order_by('timestamp')
        self.assertEqual(rows.count(), 8)
        self.assertEqual(rows.first().timestamp, T0 + dt.timedelta(minutes=5))
        metric = rows.last()
        self.assertEqual((metric.temp_c, metric.wind_speed_ms, metric.rain_mm), (25.0, 2.0, 0.5))
        self.assertEqual(Observation.objects.get(source='ambient_push').temp_c, 31.0)        # untouched
        self.assertEqual(rows.get(timestamp=T0 + dt.timedelta(minutes=20)).extra['lightning_day'], 1)
        st = Station.objects.get()
        self.assertEqual(set(st.sensors), {'temp1f', 'soiltens1', 'lightning_day'})   # batteries aren't sensors
        self.assertEqual(st.rollup_dirty_from, T0 + dt.timedelta(minutes=5))
        self.assertEqual(LatestReading.objects.get().timestamp, T0 + dt.timedelta(minutes=60))

        # The rain logic keeps the archive's per-record amounts, and summaries add them up.
        compute_rain_increments(st)
        self.assertEqual([r.rain_mm for r in rows.filter(rain_mm__gt=0)], [0.254, 0.254, 0.5])
        refresh_station(st, now=T0 + dt.timedelta(hours=2))
        self.assertAlmostEqual(DailyRollup.objects.get().rain_mm, 1.008)

        # Re-running adds nothing.
        self.assertIn('Imported 0 records', self.run_import())

    def test_errors(self):
        with self.assertRaises(CommandError):
            call_command('import_weewx', 'nope', self.path)
        with self.assertRaises(CommandError):
            call_command('import_weewx', self.station.slug, str(Path(self.dir.name) / 'missing.sdb'))
