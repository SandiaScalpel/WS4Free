import datetime as dt
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from weather import almanac
from weather.models import DailyRollup, Observation, Station

from .helpers import make_station

DENVER = ZoneInfo('America/Denver')


def day(station, date, **kw):
    defaults = {'temp_coverage': 1.0, 'rain_coverage': 1.0, 'coverage': 1.0}
    return DailyRollup.objects.create(station=station, date=date, **{**defaults, **kw})


class RecordTests(TestCase):
    def setUp(self):
        self.station = make_station()

    def by_key(self, year=None):
        return {r.key: r for r in almanac.records(self.station, year)}

    def test_extremes_count_even_on_partial_days(self):
        day(self.station, dt.date(2025, 7, 1), temp_max_c=40.0, temp_min_c=20.0, temp_coverage=0.2)
        day(self.station, dt.date(2025, 7, 2), temp_max_c=35.0, temp_min_c=22.0)
        r = self.by_key()
        self.assertEqual((r['high'].value, r['high'].date), (40.0, dt.date(2025, 7, 1)))

    def test_day_statistics_need_full_coverage(self):
        # A 3-hour fragment with a "daily low" of 30 °C must not become the warmest night.
        day(self.station, dt.date(2025, 7, 1), temp_max_c=36.0, temp_min_c=30.0, temp_coverage=0.125)
        day(self.station, dt.date(2025, 7, 2), temp_max_c=35.0, temp_min_c=22.0)
        self.assertEqual(self.by_key()['warm_night'].date, dt.date(2025, 7, 2))

    def test_time_of_record_from_raw_readings(self):
        d = dt.date(2025, 7, 1)
        day(self.station, d, temp_max_c=40.0, temp_min_c=20.0)
        peak = dt.datetime(2025, 7, 1, 16, 35, tzinfo=DENVER)
        for ts, t in ((peak - dt.timedelta(minutes=5), 39.0), (peak, 40.0), (peak + dt.timedelta(minutes=5), 38.0)):
            Observation.objects.create(station=self.station, timestamp=ts, source='api', temp_c=t)
        self.assertEqual(self.by_key()['high'].time, peak)

    def test_year_scope(self):
        day(self.station, dt.date(2024, 7, 1), temp_max_c=42.0, temp_min_c=20.0)
        day(self.station, dt.date(2025, 7, 1), temp_max_c=38.0, temp_min_c=20.0)
        self.assertEqual(self.by_key()['high'].value, 42.0)
        self.assertEqual(self.by_key(2025)['high'].value, 38.0)

    def test_wettest_month(self):
        day(self.station, dt.date(2025, 8, 1), rain_mm=10.0)
        day(self.station, dt.date(2025, 8, 20), rain_mm=15.0)
        day(self.station, dt.date(2025, 9, 5), rain_mm=20.0)
        r = self.by_key()['wet_month']
        self.assertEqual((r.value, r.date, r.end_date), (25.0, dt.date(2025, 8, 1), dt.date(2025, 8, 31)))


class DrySpellTests(TestCase):
    def test_outage_ends_a_dry_spell(self):
        st = make_station()
        for i in range(10):
            day(st, dt.date(2025, 5, 1) + dt.timedelta(days=i), rain_mm=0.0)
        # May 11–15 missing entirely (outage), then 3 more dry days.
        for i in range(3):
            day(st, dt.date(2025, 5, 16) + dt.timedelta(days=i), rain_mm=0.0)
        self.assertEqual(almanac.longest_dry_spell(DailyRollup.objects.filter(station=st)),
                         (10, dt.date(2025, 5, 1), dt.date(2025, 5, 10)))

    def test_trace_below_gauge_resolution_is_dry_but_measurable_rain_is_not(self):
        st = make_station()
        for i, rain in enumerate((0.0, 0.1, 0.0, 0.254, 0.0)):
            day(st, dt.date(2025, 5, 1) + dt.timedelta(days=i), rain_mm=rain)
        self.assertEqual(almanac.longest_dry_spell(DailyRollup.objects.filter(station=st))[0], 3)


class OnThisDayTests(TestCase):
    def test_years_records_and_rain_count(self):
        st = make_station()
        day(st, dt.date(2023, 10, 1), temp_max_c=20.0, temp_min_c=5.0, rain_mm=2.0)
        day(st, dt.date(2024, 10, 1), temp_max_c=25.0, temp_min_c=8.0, rain_mm=0.0)
        day(st, dt.date(2025, 10, 1), temp_max_c=22.0, temp_min_c=3.0, rain_mm=0.0, temp_coverage=0.4, rain_coverage=0.4)
        otd = almanac.on_this_day(st, 10, 1)
        self.assertEqual([y.year for y in otd.years], [2025, 2024, 2023])
        self.assertEqual((otd.record_high.year, otd.record_low.year), (2024, 2025))
        self.assertTrue(otd.years[0].partial)
        self.assertIsNone(otd.years[0].rain_mm)        # an untrustworthy zero
        self.assertEqual(otd.rain_years, 1)
        self.assertAlmostEqual(otd.normal_high_c, 22.5)  # partial 2025 excluded from normals
        self.assertEqual(otd.normal_years, 2)

    def test_feb_29_normals_in_common_years(self):
        st = make_station()
        day(st, dt.date(2024, 2, 29), temp_max_c=10.0, temp_min_c=0.0)
        day(st, dt.date(2025, 2, 28), temp_max_c=14.0, temp_min_c=2.0)
        otd = almanac.on_this_day(st, 2, 29)
        self.assertEqual(otd.normal_years, 2)
        self.assertAlmostEqual(otd.normal_high_c, 12.0)


class FrostTests(TestCase):
    def setUp(self):
        self.station = make_station(latitude=40.0)

    def year_of_lows(self, year, lows, coverage=None):
        """lows: {date: °C}; every other day of the year is a warm, fully covered 15 °C."""
        d = dt.date(year, 1, 1)
        while d.year == year:
            day(self.station, d, temp_min_c=lows.get(d, 15.0), temp_coverage=(coverage or {}).get(d, 1.0))
            d += dt.timedelta(days=1)

    def test_last_spring_and_first_fall(self):
        self.year_of_lows(2025, {dt.date(2025, 3, 2): -3.0, dt.date(2025, 4, 7): -0.5,
                                 dt.date(2025, 11, 6): -1.0, dt.date(2025, 12, 1): -5.0})
        (s,) = almanac.frost_dates(self.station)
        self.assertEqual((s.last_spring, s.first_fall, s.season_days), (dt.date(2025, 4, 7), dt.date(2025, 11, 6), 212))
        self.assertFalse(s.spring_uncertain or s.fall_uncertain)
        (hard,) = almanac.frost_dates(self.station, almanac.HARD_FREEZE_C)
        self.assertEqual((hard.last_spring, hard.first_fall), (dt.date(2025, 3, 2), dt.date(2025, 12, 1)))

    def test_outage_after_last_frost_makes_it_uncertain(self):
        self.year_of_lows(2025, {dt.date(2025, 4, 7): -0.5, dt.date(2025, 11, 6): -1.0})
        DailyRollup.objects.filter(date__range=(dt.date(2025, 4, 20), dt.date(2025, 4, 25))).delete()
        (s,) = almanac.frost_dates(self.station)
        self.assertTrue(s.spring_uncertain)
        self.assertFalse(s.fall_uncertain)

    def test_warm_partial_day_cannot_hide_a_frost(self):
        # Half of a July day missing, but its recorded low was 15 °C: no frost could hide there.
        self.year_of_lows(2025, {dt.date(2025, 4, 7): -0.5, dt.date(2025, 11, 6): -1.0},
                          coverage={dt.date(2025, 7, 10): 0.6})
        (s,) = almanac.frost_dates(self.station)
        self.assertFalse(s.fall_uncertain)

    def test_chilly_partial_day_with_missing_dawn_is_uncertain(self):
        self.year_of_lows(2025, {dt.date(2025, 4, 7): -0.5, dt.date(2025, 4, 20): 3.0, dt.date(2025, 11, 6): -1.0},
                          coverage={dt.date(2025, 4, 20): 0.6})
        (s,) = almanac.frost_dates(self.station)
        self.assertTrue(s.spring_uncertain)

    def test_chilly_partial_day_with_covered_dawn_is_certain(self):
        self.year_of_lows(2025, {dt.date(2025, 4, 7): -0.5, dt.date(2025, 4, 20): 3.0, dt.date(2025, 11, 6): -1.0},
                          coverage={dt.date(2025, 4, 20): 0.6})
        start = dt.datetime(2025, 4, 20, 0, 0, tzinfo=DENVER)
        for i in range(1, 12 * 10 + 7):       # every 5 min from 00:05 to 10:30
            Observation.objects.create(station=self.station, timestamp=start + dt.timedelta(minutes=5 * i),
                                       source='api', temp_c=4.0)
        (s,) = almanac.frost_dates(self.station)
        self.assertFalse(s.spring_uncertain)

    def test_southern_hemisphere_season_runs_july_to_june(self):
        Station.objects.update(latitude=-33.9)
        self.station.refresh_from_db()
        d = dt.date(2025, 7, 1)
        lows = {dt.date(2025, 9, 15): -1.0, dt.date(2026, 5, 10): -0.5}
        while d <= dt.date(2026, 6, 30):
            day(self.station, d, temp_min_c=lows.get(d, 12.0))
            d += dt.timedelta(days=1)
        seasons = [s for s in almanac.frost_dates(self.station) if s.year == 2025]
        self.assertEqual((seasons[0].last_spring, seasons[0].first_fall), (dt.date(2025, 9, 15), dt.date(2026, 5, 10)))

    def test_summary_uses_certain_years_only(self):
        seasons = [almanac.FrostSeason(2023, dt.date(2023, 4, 1), None, spring_uncertain=True),
                   almanac.FrostSeason(2024, dt.date(2024, 4, 10)), almanac.FrostSeason(2025, dt.date(2025, 4, 20))]
        summary = almanac.frost_summary(seasons)['spring']
        self.assertEqual(summary['years'], 2)
        self.assertEqual(summary['average'].strftime('%m-%d'), '04-15')


class PageTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('owner', password='x' * 16)
        TOTPDevice.objects.create(user=self.owner, name='phone', confirmed=True)
        self.station = make_station(owner=self.owner)
        day(self.station, dt.date(2025, 10, 1), temp_max_c=20.0, temp_min_c=5.0, rain_mm=1.0)
        self.url = reverse('weather:station-almanac', args=[self.station.slug])

    def test_private_station_needs_login(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_public_page_renders_all_sections(self):
        Station.objects.update(is_public=True)
        response = self.client.get(self.url, {'date': '2026-10-01', 'records': '2025', 'frost': 'freeze'})
        for text in ('On this day', 'Records', 'Frost dates', 'Hard freeze', '68.0°F', 'Wettest day'):
            self.assertContains(response, text)

    def test_bad_parameters_fall_back(self):
        Station.objects.update(is_public=True)
        self.assertEqual(self.client.get(self.url, {'date': 'nope', 'records': '1066'}).status_code, 200)
