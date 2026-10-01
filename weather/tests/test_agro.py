import datetime as dt

from django.test import SimpleTestCase, TestCase

from weather import agro
from weather.models import DailyRollup, HourlyRollup

from .helpers import make_station


class Fao56Tests(SimpleTestCase):
    """Worked examples from FAO Irrigation and Drainage Paper 56 (Allen et al. 1998)."""

    def test_example_8_extraterrestrial_radiation(self):
        # 20° S on 3 September (day 246): Ra = 32.2 MJ m⁻² day⁻¹.
        self.assertAlmostEqual(agro.extraterrestrial_radiation(-20.0, 246), 32.2, delta=0.1)

    def test_example_14_wind_at_two_metres(self):
        # 3.2 m/s measured at 10 m → 2.4 m/s at 2 m.
        self.assertAlmostEqual(agro.wind_at_2m(3.2, 10.0), 2.4, delta=0.05)

    def test_example_18_penman_monteith_brussels(self):
        # Brussels (50°48' N, 100 m), 6 July: Tmax 21.5, Tmin 12.3 °C, RHmax 84, RHmin 63 %,
        # wind 10 km/h at 10 m, Rs 22.07 MJ m⁻² → ET₀ = 3.9 mm/day.
        et0 = agro.et0_penman_monteith(21.5, 12.3, 84, 63, 10 / 3.6, 22.07, 50.8, 100, 187, z_wind=10.0)
        self.assertAlmostEqual(et0, 3.9, delta=0.05)

    def test_hargreaves_is_in_the_same_range(self):
        # Hargreaves for the same day should land near Penman–Monteith (it's an approximation).
        self.assertAlmostEqual(agro.et0_hargreaves(21.5, 12.3, 50.8, 187), 3.9, delta=1.0)


class GddTests(SimpleTestCase):
    def test_modified_method(self):
        base, cap = agro.f_to_c(50), agro.f_to_c(86)
        # 90/45 °F → 86/50 → mean 68 → 18 °F·days = 10 °C·days
        self.assertAlmostEqual(agro.gdd_day(agro.f_to_c(90), agro.f_to_c(45), base, cap), 10.0)
        # Entirely below the base: 0, not negative.
        self.assertEqual(agro.gdd_day(agro.f_to_c(45), agro.f_to_c(30), base, cap), 0.0)
        # No cap (Winkler): 95/60 °F → mean 77.5 → 27.5 °F·days
        self.assertAlmostEqual(agro.gdd_day(agro.f_to_c(95), agro.f_to_c(60), base) * 1.8, 27.5)

    def test_chill_models(self):
        self.assertEqual([agro.chill_hour(agro.f_to_c(t)) for t in (31, 33, 45, 46)], [0, 1, 1, 0])
        self.assertEqual([agro.utah_units(agro.f_to_c(t)) for t in (30, 35, 40, 50, 58, 63, 70)],
                         [0, 0.5, 1, 0.5, 0, -0.5, -1])


class SeasonTests(TestCase):
    def setUp(self):
        self.station = make_station(latitude=40.015, elevation_m=1655)

    def test_gdd_season_skips_incomplete_days(self):
        for i, (hi, lo, cov) in enumerate(((30.0, 15.0, 1.0), (30.0, 15.0, 0.3), (30.0, 15.0, 1.0))):
            DailyRollup.objects.create(station=self.station, date=dt.date(2025, 5, 1) + dt.timedelta(days=i),
                                       temp_max_c=hi, temp_min_c=lo, temp_coverage=cov)
        (s,) = agro.gdd_seasons(self.station, 10.0, None, start=(5, 1), end=(5, 31), today=dt.date(2025, 5, 3))
        self.assertEqual(s['values'], [12.5, None, 25.0])
        self.assertEqual(s['missing'], 1)

    def test_average_to_date_uses_earlier_complete_seasons(self):
        seasons = [{'year': 2023, 'values': [1, 2, 3]}, {'year': 2024, 'values': [2, None, 5]},
                   {'year': 2025, 'values': [9, 9, 9]}]
        self.assertEqual(agro.average_to_date(seasons, 2, 2025), (4.0, 2))
        self.assertEqual(agro.average_to_date(seasons, 2, 2025, max_missing=0), (3.0, 1))

    def test_chill_season_wraps_new_year_in_local_time(self):
        # 03:00 local on Dec 1 and Jan 15 at 40 °F (chill); one July hour (outside the season).
        for local in (dt.datetime(2024, 12, 1, 3), dt.datetime(2025, 1, 15, 3), dt.datetime(2025, 7, 1, 3)):
            HourlyRollup.objects.create(station=self.station, period_start=local.replace(tzinfo=self.station.tzinfo),
                                        temp_avg_c=agro.f_to_c(40))
        seasons = agro.chill_seasons(self.station, today=dt.date(2025, 2, 28))
        (s,) = [x for x in seasons if x['year'] == 2024]
        self.assertEqual(s['label'], '2024–25')
        self.assertEqual(s['total'], 2.0)

    def test_et0_uses_penman_monteith_when_complete_else_hargreaves(self):
        full = DailyRollup(station=self.station, date=dt.date(2025, 7, 1), temp_max_c=35, temp_min_c=18,
                           humidity_max=50, humidity_min=12, wind_speed_avg_ms=3, solar_avg_wm2=330,
                           temp_coverage=1.0, coverage=1.0)
        value, method = agro.et0_for_rollup(full, self.station)
        self.assertEqual(method, 'pm')
        self.assertGreater(value, 6)          # a hot, dry, sunny high-desert summer day
        full.coverage = 0.6                   # solar/wind incomplete
        self.assertEqual(agro.et0_for_rollup(full, self.station)[1], 'hargreaves')
        full.temp_coverage = 0.6
        self.assertEqual(agro.et0_for_rollup(full, self.station), (None, None))


class GrowingPageTests(TestCase):
    def setUp(self):
        from django.urls import reverse
        self.station = make_station(latitude=40.015, elevation_m=1655, is_public=True)
        for i in range(10):
            DailyRollup.objects.create(station=self.station, date=dt.date(2026, 6, 1) + dt.timedelta(days=i),
                                       temp_max_c=33.0, temp_min_c=17.0, temp_avg_c=25.0, humidity_max=60, humidity_min=15,
                                       wind_speed_avg_ms=2.0, solar_avg_wm2=320, rain_mm=0.0,
                                       temp_coverage=1.0, rain_coverage=1.0, coverage=1.0)
        self.url = reverse('weather:station-growing', args=[self.station.slug])

    def test_renders_all_sections(self):
        for crop in ('general', 'grapes', 'bogus'):
            with self.subTest(crop=crop):
                response = self.client.get(self.url, {'crop': crop, 'chill': 'utah'})
                self.assertEqual(response.status_code, 200)
                for text in ('Growing degree days', 'Winter chill', 'Evapotranspiration', 'growing-data'):
                    self.assertContains(response, text)

    def test_report_columns(self):
        from weather import reports
        from weather.units import UnitPrefs
        r = reports.build(self.station, dt.date(2026, 6, 1), dt.date(2026, 6, 10), UnitPrefs.for_system('imperial'),
                          'month', ['gdd', 'et0'])
        gdd, et0 = r.summary
        self.assertAlmostEqual(gdd, 10 * (min(33.0 * 9 / 5 + 32, 86) + (17.0 * 9 / 5 + 32)) / 2 - 10 * 50, places=3)
        self.assertGreater(et0, 10 * 6 / 25.4)          # > 6 mm/day on hot, dry, sunny days, in inches
