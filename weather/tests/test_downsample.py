import datetime as dt
import math
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.test import TestCase, override_settings

from weather.downsample import StaleRollups, downsample_day, downsample_station, merge_rows
from weather.ingest.store import mark_dirty
from weather.models import DailyRollup, HourlyRollup, Observation, Station
from weather.rollups import refresh_station

from .helpers import make_station

DENVER = ZoneInfo('America/Denver')
DAY = dt.date(2020, 7, 1)
DAY_START = dt.datetime.combine(DAY, dt.time(), tzinfo=DENVER).astimezone(dt.UTC)
TODAY = dt.date(2026, 10, 1)


def fill_day(station, day_start=DAY_START):
    """One day of realistic 5-minute data with cumulative rain counters."""
    daily = 0.0
    for i in range(1, 289):
        ts = day_start + dt.timedelta(minutes=5 * i)
        hour = i / 12
        rain = 0.254 if 70 <= i < 90 else 0.0
        daily += rain
        Observation.objects.create(
            station=station, timestamp=ts, source=Observation.SOURCE_BACKFILL,
            temp_c=15 + 10 * math.sin((hour - 9) / 24 * 2 * math.pi) + (i % 7) * 0.1,
            humidity=40 + (i % 11), pressure_rel_hpa=1010 + (i % 5) * 0.3,
            wind_speed_ms=2 + (i % 4), wind_gust_ms=4 + (i % 9), wind_dir_deg=(200 + i * 3) % 360,
            rain_daily_mm=round(daily, 3), rain_rate_mmh=12.0 if rain else 0.0,
            solar_wm2=max(0.0, 800 * math.sin((hour - 6) / 12 * math.pi)), uv_index=(i % 10) / 2,
        )


def rollup_snapshot(station):
    fields = ('temp_avg_c', 'temp_min_c', 'temp_max_c', 'humidity_min', 'humidity_max', 'pressure_avg_hpa',
              'pressure_min_hpa', 'pressure_max_hpa', 'wind_gust_max_ms', 'rain_mm', 'rain_rate_max_mmh',
              'solar_max_wm2', 'uv_max', 'covered_s')
    return {
        'daily': list(DailyRollup.objects.filter(station=station).values(*fields)),
        'hourly': list(HourlyRollup.objects.filter(station=station).order_by('period_start').values(*fields)),
    }


class MergeTests(TestCase):
    def test_merge_rules(self):
        st = make_station()
        t = dt.datetime(2020, 7, 1, 12, 15, tzinfo=dt.UTC)
        rows = [
            Observation(station=st, timestamp=t - dt.timedelta(minutes=10), interval_s=300, temp_c=10.0, humidity=50,
                        wind_speed_ms=4.0, wind_dir_deg=350.0, wind_gust_ms=6.0, rain_mm=0.254, rain_rate_mmh=3.0,
                        rain_daily_mm=1.0, solar_wm2=100.0, uv_index=1.0),
            Observation(station=st, timestamp=t - dt.timedelta(minutes=5), interval_s=300, temp_c=13.0, humidity=60,
                        wind_speed_ms=4.0, wind_dir_deg=10.0, wind_gust_ms=9.0, rain_mm=0.508, rain_rate_mmh=6.0,
                        rain_daily_mm=1.508, solar_wm2=300.0, uv_index=2.0),
            Observation(station=st, timestamp=t, interval_s=300, temp_c=16.0, humidity=70,
                        wind_speed_ms=0.0, wind_dir_deg=180.0, wind_gust_ms=2.0, rain_mm=None, rain_rate_mmh=0.0,
                        rain_daily_mm=1.508, solar_wm2=200.0, uv_index=3.0),
        ]
        m = merge_rows(rows, t)
        self.assertEqual((m.interval_s, m.sample_count, m.source), (900, 3, 'downsampled'))
        self.assertEqual((m.temp_c, m.temp_min_c, m.temp_max_c), (13.0, 10.0, 16.0))
        self.assertEqual((m.humidity_min, m.humidity_max), (50, 70))
        self.assertEqual((m.wind_gust_ms, m.rain_rate_mmh), (9.0, 6.0))
        self.assertAlmostEqual(m.rain_mm, 0.762)
        self.assertEqual(m.rain_daily_mm, 1.508)            # counters keep the bucket's last value
        self.assertIn(m.wind_dir_deg, (0.0, 360.0))          # vector mean of 350° and 10°; calm row ignored
        self.assertEqual((m.solar_wm2, m.solar_max_wm2, m.uv_max), (200.0, 300.0, 3.0))


class DownsampleTests(TestCase):
    def setUp(self):
        self.station = make_station()
        fill_day(self.station)
        mark_dirty(self.station.pk, DAY_START)
        refresh_station(self.station, now=DAY_START + dt.timedelta(days=2))

    def test_day_merges_three_to_one(self):
        self.assertEqual(downsample_day(self.station, DAY), (288, 96))
        self.assertEqual(Observation.objects.filter(interval_s=900).count(), 96)
        self.assertFalse(Observation.objects.filter(interval_s=300).exists())

    def test_rollups_rebuilt_from_merged_rows_match_raw(self):
        before = rollup_snapshot(self.station)
        downsample_day(self.station, DAY)
        mark_dirty(self.station.pk, DAY_START)
        refresh_station(self.station, now=DAY_START + dt.timedelta(days=2))
        after = rollup_snapshot(self.station)
        for kind in ('daily', 'hourly'):
            self.assertEqual(len(before[kind]), len(after[kind]))
            for b, a in zip(before[kind], after[kind]):
                for field, value in b.items():
                    with self.subTest(kind=kind, field=field):
                        if value is None:
                            self.assertIsNone(a[field])
                        else:
                            self.assertAlmostEqual(a[field], value, places=6)

    def test_second_run_changes_nothing(self):
        downsample_day(self.station, DAY)
        self.assertEqual(downsample_day(self.station, DAY), (96, 96))

    def test_failure_leaves_raw_rows_intact(self):
        with mock.patch('weather.downsample.Observation.objects.bulk_create', side_effect=RuntimeError('disk full')):
            with self.assertRaises(RuntimeError):
                downsample_day(self.station, DAY)
        self.assertEqual(Observation.objects.filter(interval_s=300).count(), 288)

    def test_only_days_older_than_retention(self):
        recent_start = dt.datetime.combine(dt.date(2024, 7, 1), dt.time(), tzinfo=DENVER).astimezone(dt.UTC)
        fill_day(self.station, recent_start)
        mark_dirty(self.station.pk, recent_start)
        refresh_station(self.station, now=recent_start + dt.timedelta(days=2))
        days, before, after = downsample_station(self.station, 5, today=TODAY)   # cutoff 2021-10-01
        self.assertEqual((before, after), (288, 96))
        self.assertEqual(Observation.objects.filter(timestamp__gt=recent_start, interval_s=300).count(), 288)

    def test_refuses_while_rollups_are_stale(self):
        mark_dirty(self.station.pk, DAY_START)
        with self.assertRaises(StaleRollups):
            downsample_station(self.station, 5, today=TODAY)
        self.assertEqual(Observation.objects.filter(interval_s=300).count(), 288)

    def test_dry_run_changes_nothing(self):
        days, before, after = downsample_station(self.station, 5, today=TODAY, dry_run=True)
        self.assertEqual((before, after), (288, 96))
        self.assertEqual(Observation.objects.count(), 288)

    def test_command_is_off_by_default(self):
        out = StringIO()
        with override_settings(RAW_RETENTION_YEARS=0):
            call_command('downsample_observations', stdout=out)
        self.assertIn('off', out.getvalue())
        self.assertEqual(Observation.objects.count(), 288)
