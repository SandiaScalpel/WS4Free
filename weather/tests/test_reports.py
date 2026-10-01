import datetime as dt

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from weather import reports
from weather.models import DailyRollup, Station
from weather.units import UnitPrefs, f_to_c, in_to_mm

from .helpers import make_station

IMP = UnitPrefs.for_system('imperial')
MET = UnitPrefs.for_system('metric')


def day(station, date, hi_f=None, lo_f=None, rain_in=None, cov=1.0, **kw):
    return DailyRollup.objects.create(
        station=station, date=date, temp_max_c=f_to_c(hi_f) if hi_f is not None else None,
        temp_min_c=f_to_c(lo_f) if lo_f is not None else None,
        temp_avg_c=f_to_c((hi_f + lo_f) / 2) if hi_f is not None and lo_f is not None else None,
        rain_mm=in_to_mm(rain_in) if rain_in is not None else None,
        temp_coverage=cov, rain_coverage=cov, coverage=cov, **kw)


def col(report, key, row=None):
    i = [c.key for c in report.columns].index(key)
    return report.summary[i] if row is None else report.rows[row].values[i]


class ReportMathTests(TestCase):
    def setUp(self):
        self.st = make_station()
        day(self.st, dt.date(2025, 1, 1), 45, 25, 0.10)   # mid 35 → HDD 30, freeze day
        day(self.st, dt.date(2025, 1, 2), 55, 35, 0.00)   # mid 45 → HDD 20
        day(self.st, dt.date(2025, 7, 1), 100, 70, 0.50)  # mid 85 → CDD 20, hot day
        day(self.st, dt.date(2025, 7, 2), 95, 69, 0.0, cov=0.3)  # partial: no degree days / counts, extremes still count

    def build(self, group, keys, start=dt.date(2025, 1, 1), end=dt.date(2025, 12, 31), prefs=IMP):
        return reports.build(self.st, start, end, prefs, group, keys)

    def test_degree_days_and_counts(self):
        r = self.build('year', ['hdd', 'cdd', 'days_hot', 'days_freeze', 'days_with_data'])
        self.assertAlmostEqual(col(r, 'hdd'), 50.0, places=6)
        self.assertAlmostEqual(col(r, 'cdd'), 20.0, places=6)
        self.assertEqual((col(r, 'days_hot'), col(r, 'days_freeze'), col(r, 'days_with_data')), (1, 1, 3))

    def test_extremes_use_partial_days_means_do_not(self):
        r = self.build('month', ['temp_high', 'temp_avg_high'], start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31))
        self.assertAlmostEqual(col(r, 'temp_high'), 100.0)
        self.assertAlmostEqual(col(r, 'temp_avg_high'), 100.0)   # the 30 %-covered day is left out of the mean
        self.assertTrue(r.rows[0].partial)

    def test_period_mean_is_a_mean_of_days_not_of_rows(self):
        # January's two full days average a 50 °F high, July's one full day 100 °F:
        # the year's average high is (45 + 55 + 100) / 3, not (50 + 100) / 2.
        r = self.build('month', ['temp_avg_high'])
        self.assertAlmostEqual(col(r, 'temp_avg_high'), 200 / 3)

    def test_rain_total_and_rain_days(self):
        r = self.build('year', ['rain', 'rain_days'])
        self.assertAlmostEqual(col(r, 'rain'), 0.60)
        self.assertEqual(col(r, 'rain_days'), 2)

    def test_monthly_rows_cover_every_month_in_range(self):
        r = self.build('month', ['rain'])
        self.assertEqual(len(r.rows), 12)
        self.assertEqual(r.rows[0].label, 'January 2025')
        self.assertIsNone(r.rows[3].values[0])                    # April: no data at all

    def test_daily_report_hides_period_only_columns(self):
        r = self.build('day', ['temp_high', 'rain_days', 'days_hot'], start=dt.date(2025, 1, 1), end=dt.date(2025, 1, 3))
        self.assertEqual([c.key for c in r.columns], ['temp_high'])
        self.assertEqual(len(r.rows), 3)
        self.assertIsNone(r.rows[2].values[0])                     # Jan 3 has no data

    def test_metric_uses_metric_base_and_thresholds(self):
        r = self.build('year', ['hdd', 'days_hot'], prefs=MET)
        # mids in °C: 1.67, 7.22, 29.44 → HDD base 18 = 16.33 + 10.78; hot day ≥ 32 °C: 100 °F = 37.8 °C
        self.assertAlmostEqual(col(r, 'hdd'), 27.11, places=1)
        self.assertEqual(col(r, 'days_hot'), 1)

    def test_auto_grouping(self):
        self.assertEqual(reports.auto_group(dt.date(2025, 1, 1), dt.date(2025, 2, 28)), 'day')
        self.assertEqual(reports.auto_group(dt.date(2025, 1, 1), dt.date(2025, 12, 31)), 'month')
        self.assertEqual(reports.auto_group(dt.date(2020, 1, 1), dt.date(2025, 12, 31)), 'year')

    def test_csv(self):
        text = reports.to_csv(self.build('month', ['temp_high', 'rain']))
        lines = text.splitlines()
        self.assertEqual(lines[0], 'Month,High (°F),Rain (in)')
        self.assertEqual(lines[1], '2025-01,55.0,0.10')
        self.assertEqual(lines[-1], 'Whole period,100.0,0.60')


class ReportPageTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user('owner')
        self.st = make_station(is_public=True)
        day(self.st, dt.date(2025, 1, 1), 45, 25, 0.10)
        self.url = reverse('weather:station-reports', args=[self.st.slug])

    def test_page_and_csv(self):
        response = self.client.get(self.url, {'period': 'custom', 'start': '2025-01-01', 'end': '2025-01-31', 'col': ['temp_high', 'rain']})
        self.assertContains(response, 'Daily summary')
        self.assertContains(response, '45.0')
        csv = self.client.get(self.url, {'period': 'custom', 'start': '2025-01-01', 'end': '2025-01-31', 'col': ['rain'], 'format': 'csv'})
        self.assertEqual(csv['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('test-station-day-report-20250101-20250131.csv', csv['Content-Disposition'])

    def test_private_station(self):
        Station.objects.update(is_public=False)
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_bad_input_falls_back(self):
        self.assertEqual(self.client.get(self.url, {'period': 'custom', 'start': 'x', 'group': 'decade', 'col': 'bogus'}).status_code, 200)
