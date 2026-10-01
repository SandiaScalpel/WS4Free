import datetime as dt
from zoneinfo import ZoneInfo

from django.test import TestCase

from weather.ingest.rain import compute_rain_increments
from weather.models import Observation

from .helpers import T0, make_station, obs

DENVER = ZoneInfo('America/Denver')


class RainIncrementTests(TestCase):
    def setUp(self):
        self.station = make_station()

    def rain(self):
        return list(Observation.objects.order_by('timestamp').values_list('rain_mm', flat=True))

    def test_total_counter_deltas_when_no_daily_counter(self):
        for m, total in ((0, 100.0), (5, 100.0), (10, 101.27), (15, 103.81)):
            obs(self.station, m, rain_counter_mm=total)
        compute_rain_increments(self.station)
        self.assertEqual(self.rain(), [None, 0.0, 1.27, 2.54])

    def test_daily_counter_preferred_within_a_day(self):
        # Real WS-2902 behaviour: totalrainin (0.001 in) drifts from dailyrainin
        # (0.01 in); daily totals must match the console's daily counter.
        for m, daily, total in ((0, 0.254, 1180.0), (5, 0.508, 1180.3), (10, 0.762, 1180.5)):
            obs(self.station, m, rain_daily_mm=daily, rain_counter_mm=total)
        compute_rain_increments(self.station)
        self.assertEqual(self.rain(), [None, 0.254, 0.254])

    def test_rain_in_last_interval_before_midnight_is_kept(self):
        # Actual records around local midnight (inches): the 00:00 record already
        # shows the daily reset, but event and total prove 0.01 in fell 23:55–00:00.
        before = dt.datetime(2026, 9, 28, 23, 55, tzinfo=DENVER)
        for ts, daily, event, total in ((before, 1.47, 1.46, 46.472),
                                        (before + dt.timedelta(minutes=5), 0.0, 1.47, 46.484),
                                        (before + dt.timedelta(minutes=10), 0.0, 1.47, 46.484)):
            Observation.objects.create(station=self.station, timestamp=ts, source='api',
                                       rain_daily_mm=daily * 25.4, rain_event_mm=event * 25.4,
                                       rain_counter_mm=total * 25.4)
        compute_rain_increments(self.station)
        self.assertEqual(self.rain(), [None, 0.254, 0.0])   # event delta, 0.01 in

    def test_summing_increments_equals_counter_change(self):
        totals = [50.0, 50.254, 50.508, 51.27, 51.27, 52.54]
        for i, total in enumerate(totals):
            obs(self.station, 5 * i, rain_counter_mm=total)
        compute_rain_increments(self.station)
        self.assertAlmostEqual(sum(r for r in self.rain() if r), totals[-1] - totals[0], places=6)

    def test_daily_counter_fallback_resets_at_local_midnight(self):
        # 23:50 and 00:05 Denver time (MDT, UTC-6) straddle the daily reset.
        before = dt.datetime(2026, 7, 1, 23, 50, tzinfo=DENVER)
        for ts, daily in ((before, 10.16), (before + dt.timedelta(minutes=5), 10.414),
                          (before + dt.timedelta(minutes=15), 0.254), (before + dt.timedelta(minutes=20), 0.508)):
            Observation.objects.create(station=self.station, timestamp=ts, source='api', rain_daily_mm=daily)
        compute_rain_increments(self.station)
        self.assertEqual(self.rain(), [None, 0.254, 0.254, 0.254])

    def test_new_day_total_larger_than_yesterdays_is_not_a_delta(self):
        # 1 mm by 23:55; a gap; 2 mm since midnight by 00:30. The counter went UP
        # across the reset, so only the date tells us it reset: rain is 2 mm, not 1.
        before = dt.datetime(2026, 7, 1, 23, 55, tzinfo=DENVER)
        Observation.objects.create(station=self.station, timestamp=before, source='api', rain_daily_mm=1.0)
        Observation.objects.create(station=self.station, timestamp=before + dt.timedelta(minutes=35),
                                   source='api', rain_daily_mm=2.0)
        compute_rain_increments(self.station)
        self.assertEqual(self.rain()[1], 2.0)

    def test_records_without_counters_are_bridged_not_double_counted(self):
        # Real pattern from Nov 4, 2024: Ambient's archive alternates records with and
        # without rain counters. Rain across the blanks must be counted exactly once.
        pattern = [(0, 3.302), (5, None), (10, 3.302), (15, None), (20, None), (25, 3.556), (30, None), (35, 4.064)]
        for m, daily in pattern:
            obs(self.station, m, rain_daily_mm=daily)
        compute_rain_increments(self.station)
        rain = self.rain()
        self.assertAlmostEqual(sum(r for r in rain if r), 4.064 - 3.302, places=6)
        self.assertEqual(rain[5], 0.254)     # 3.302 → 3.556 across two blank records
        self.assertIsNone(rain[1])

    def test_total_counter_reset_falls_back_to_daily(self):
        obs(self.station, 0, rain_counter_mm=500.0, rain_daily_mm=2.0)
        obs(self.station, 5, rain_counter_mm=0.0, rain_daily_mm=2.254)   # console was reset
        compute_rain_increments(self.station)
        self.assertEqual(self.rain()[1], 0.254)

    def test_long_outage_does_not_dump_days_of_rain_into_one_row(self):
        obs(self.station, 0, rain_counter_mm=100.0, rain_daily_mm=0.0)
        obs(self.station, 3 * 24 * 60, rain_counter_mm=160.0, rain_daily_mm=5.0)
        compute_rain_increments(self.station)
        self.assertEqual(self.rain()[1], 5.0)   # rain since local midnight, not the 60 mm total

    def test_implausible_jump_is_null(self):
        obs(self.station, 0, rain_counter_mm=100.0)
        obs(self.station, 5, rain_counter_mm=900.0)
        compute_rain_increments(self.station)
        self.assertIsNone(self.rain()[1])

    def test_recompute_from_midrange_seeds_from_previous_row(self):
        for m, total in ((0, 10.0), (5, 11.0), (10, 13.0)):
            obs(self.station, m, rain_counter_mm=total)
        compute_rain_increments(self.station, since=T0 + dt.timedelta(minutes=10))
        self.assertEqual(self.rain()[2], 2.0)

    def test_pages_through_large_ranges(self):
        for i in range(25):
            obs(self.station, 5 * i, rain_counter_mm=float(i))
        compute_rain_increments(self.station, batch_size=7)
        self.assertEqual(self.rain(), [None] + [1.0] * 24)
