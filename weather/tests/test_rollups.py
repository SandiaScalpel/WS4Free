import datetime as dt
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase

from weather.ingest.store import mark_dirty
from weather.models import DailyRollup, HourlyRollup, Observation, Station
from weather.rollups import refresh_station

from .helpers import make_station

DENVER = ZoneInfo('America/Denver')
UTC = dt.UTC
H0 = dt.datetime(2026, 7, 1, 12, 0, tzinfo=UTC)     # 06:00 MDT


def add(station, ts, interval=300, **values):
    return Observation.objects.create(station=station, timestamp=ts, interval_s=interval,
                                      source=Observation.SOURCE_API, **values)


# Rain increments are recomputed from counters at the start of every refresh
# (tested in test_rain); these tests set rain_mm directly, so that step is stubbed.
NO_RAIN_PASS = mock.patch('weather.rollups.compute_rain_increments', new=lambda *args, **kwargs: 0)


@NO_RAIN_PASS
class RollupMathTests(TestCase):
    def setUp(self):
        self.station = make_station()

    def refresh(self, now=H0 + dt.timedelta(days=2)):
        mark_dirty(self.station.pk, Observation.objects.order_by('timestamp').first().timestamp)
        return refresh_station(self.station, now=now)

    def hour(self, start=H0):
        return HourlyRollup.objects.get(station=self.station, period_start=start)

    def test_row_stamped_on_the_hour_belongs_to_the_previous_hour(self):
        add(self.station, H0, temp_c=10.0)                          # covers 11:55–12:00
        add(self.station, H0 + dt.timedelta(minutes=5), temp_c=20.0)
        self.refresh()
        self.assertEqual(self.hour(H0 - dt.timedelta(hours=1)).temp_avg_c, 10.0)
        self.assertEqual(self.hour(H0).temp_avg_c, 20.0)

    def test_averages_weighted_by_interval(self):
        # A downsampled 15-min row at 30 °C and a raw 5-min row at 10 °C:
        # time-weighted mean = (30·900 + 10·300) / 1200 = 25, not 20.
        add(self.station, H0 + dt.timedelta(minutes=15), interval=900, temp_c=30.0, temp_min_c=8.0, temp_max_c=33.0)
        add(self.station, H0 + dt.timedelta(minutes=20), temp_c=10.0)
        self.refresh()
        h = self.hour()
        self.assertAlmostEqual(h.temp_avg_c, 25.0)
        self.assertEqual((h.temp_min_c, h.temp_max_c), (8.0, 33.0))    # the downsampled row's own extremes count
        self.assertEqual(h.covered_s, 1200)
        self.assertAlmostEqual(h.coverage, 1200 / 3600)

    def test_rain_summed_peaks_maxed(self):
        for i, (rain, rate, gust) in enumerate([(0.254, 3.0, 5.0), (0.508, 9.0, 12.5), (None, 0.0, 7.0)], start=1):
            add(self.station, H0 + dt.timedelta(minutes=5 * i), rain_mm=rain, rain_rate_mmh=rate, wind_gust_ms=gust)
        self.refresh()
        h = self.hour()
        self.assertAlmostEqual(h.rain_mm, 0.762)
        self.assertEqual((h.rain_rate_max_mmh, h.wind_gust_max_ms), (9.0, 12.5))

    def test_no_rain_data_is_null_not_zero(self):
        add(self.station, H0 + dt.timedelta(minutes=5), temp_c=10.0)
        self.refresh()
        self.assertIsNone(self.hour().rain_mm)

    def test_wind_direction_is_a_vector_mean(self):
        # 350° and 10° average to north, not 180°.
        add(self.station, H0 + dt.timedelta(minutes=5), wind_speed_ms=4.0, wind_dir_deg=350.0)
        add(self.station, H0 + dt.timedelta(minutes=10), wind_speed_ms=4.0, wind_dir_deg=10.0)
        self.refresh()
        self.assertIn(self.hour().wind_dir_avg_deg, (0.0, 360.0))

    def test_wind_direction_weighted_by_speed(self):
        # A strong east wind outweighs a light north breeze.
        add(self.station, H0 + dt.timedelta(minutes=5), wind_speed_ms=1.0, wind_dir_deg=0.0)
        add(self.station, H0 + dt.timedelta(minutes=10), wind_speed_ms=9.0, wind_dir_deg=90.0)
        self.refresh()
        self.assertAlmostEqual(self.hour().wind_dir_avg_deg, 83.7, places=1)   # atan2(9, 1)

    def test_calm_has_no_direction(self):
        add(self.station, H0 + dt.timedelta(minutes=5), wind_speed_ms=0.0, wind_dir_deg=200.0)
        self.refresh()
        self.assertIsNone(self.hour().wind_dir_avg_deg)


@NO_RAIN_PASS
class DailyRollupTests(TestCase):
    def setUp(self):
        self.station = make_station()   # America/Denver

    def test_local_day_and_midnight_row(self):
        midnight = dt.datetime(2026, 7, 2, 0, 0, tzinfo=DENVER)
        add(self.station, midnight - dt.timedelta(minutes=5), temp_c=20.0, rain_mm=1.0)
        add(self.station, midnight, temp_c=30.0, rain_mm=2.0)               # 23:55–00:00 → July 1
        add(self.station, midnight + dt.timedelta(minutes=5), temp_c=5.0, rain_mm=4.0)
        mark_dirty(self.station.pk, midnight - dt.timedelta(minutes=5))
        refresh_station(self.station, now=midnight + dt.timedelta(days=1))
        july1 = DailyRollup.objects.get(date=dt.date(2026, 7, 1))
        july2 = DailyRollup.objects.get(date=dt.date(2026, 7, 2))
        self.assertEqual((july1.rain_mm, july1.temp_max_c), (3.0, 30.0))
        self.assertEqual((july2.rain_mm, july2.temp_min_c), (4.0, 5.0))

    def test_full_day_coverage_including_dst_days(self):
        for day, hours in ((dt.date(2026, 7, 1), 24), (dt.date(2026, 3, 8), 23), (dt.date(2026, 11, 1), 25)):
            # Step in UTC: aware-datetime arithmetic in a zoneinfo zone follows the
            # wall clock and would repeat an hour when DST ends.
            start = dt.datetime.combine(day, dt.time(), tzinfo=DENVER).astimezone(UTC)
            end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=DENVER).astimezone(UTC)
            ts = start + dt.timedelta(minutes=5)
            while ts <= end:
                add(self.station, ts, temp_c=10.0)
                ts += dt.timedelta(minutes=5)
            mark_dirty(self.station.pk, start)
            refresh_station(self.station, now=end + dt.timedelta(hours=1))
            with self.subTest(day=day):
                d = DailyRollup.objects.get(date=day)
                self.assertEqual(d.sample_count, hours * 12)
                self.assertAlmostEqual(d.coverage, 1.0)

    def test_per_sensor_coverage(self):
        # The console kept reporting all day but the outdoor sensor dropped out at
        # noon and the rain gauge never reported: the day is "complete" overall
        # yet useless for a temperature record or a rain total.
        start = dt.datetime(2026, 7, 1, 0, 0, tzinfo=DENVER).astimezone(UTC)
        for i in range(1, 289):
            add(self.station, start + dt.timedelta(minutes=5 * i), temp_in_c=22.0,
                temp_c=15.0 if i <= 144 else None)
        mark_dirty(self.station.pk, start)
        refresh_station(self.station, now=start + dt.timedelta(days=2))
        d = DailyRollup.objects.get()
        self.assertAlmostEqual(d.coverage, 1.0)
        self.assertAlmostEqual(d.temp_coverage, 0.5)
        self.assertEqual(d.rain_coverage, 0)

    def test_partial_day_coverage(self):
        start = dt.datetime(2026, 7, 1, 0, 0, tzinfo=DENVER)
        for i in range(1, 37):                                   # 3 hours of data
            add(self.station, start + dt.timedelta(minutes=5 * i), temp_c=10.0)
        mark_dirty(self.station.pk, start)
        refresh_station(self.station, now=start + dt.timedelta(days=1))
        self.assertAlmostEqual(DailyRollup.objects.get().coverage, 3 / 24)


class RefreshTests(TestCase):
    def setUp(self):
        self.station = make_station()
        for i in range(1, 49):
            add(self.station, H0 + dt.timedelta(minutes=5 * i), temp_c=float(i))
        mark_dirty(self.station.pk, H0)
        refresh_station(self.station, now=H0 + dt.timedelta(hours=5))

    def test_clears_watermark_and_is_noop_when_clean(self):
        self.assertIsNone(Station.objects.get().rollup_dirty_from)
        self.assertIsNone(refresh_station(self.station))

    def test_incremental_refresh_only_touches_dirty_periods(self):
        first_hour = HourlyRollup.objects.get(period_start=H0)
        Observation.objects.filter(timestamp__lte=H0 + dt.timedelta(hours=1)).update(temp_c=-50.0)  # not marked dirty
        late = H0 + dt.timedelta(hours=1, minutes=30)
        Observation.objects.filter(timestamp=late).update(temp_c=99.0)
        mark_dirty(self.station.pk, late)
        refresh_station(self.station, now=H0 + dt.timedelta(hours=5))
        self.assertEqual(HourlyRollup.objects.get(period_start=H0).temp_avg_c, first_hour.temp_avg_c)
        self.assertEqual(HourlyRollup.objects.get(period_start=H0 + dt.timedelta(hours=1)).temp_max_c, 99.0)

    def test_write_during_refresh_is_not_lost(self):
        later = H0 + dt.timedelta(hours=4, minutes=30)

        def concurrent_push(*args, **kwargs):
            add(self.station, later, temp_c=55.0)
            mark_dirty(self.station.pk, later)
            return 0

        mark_dirty(self.station.pk, H0)
        with mock.patch('weather.rollups.compute_rain_increments', side_effect=concurrent_push):
            refresh_station(self.station, now=H0 + dt.timedelta(hours=5))
        self.assertEqual(Station.objects.get().rollup_dirty_from, later)

    def test_failure_restores_watermark(self):
        mark_dirty(self.station.pk, H0)
        with mock.patch('weather.rollups.rebuild_daily', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                refresh_station(self.station, now=H0 + dt.timedelta(hours=5))
        self.assertEqual(Station.objects.get().rollup_dirty_from, H0)

    def test_refresh_recomputes_rain_first(self):
        # Rain amounts depend on the time zone; a tz change marks history dirty and
        # the refresh must recompute increments before summing them.
        with mock.patch('weather.rollups.compute_rain_increments') as rain:
            mark_dirty(self.station.pk, H0)
            refresh_station(self.station, now=H0 + dt.timedelta(hours=5))
        rain.assert_called_once()
        self.assertEqual(rain.call_args.kwargs['since'], H0)
