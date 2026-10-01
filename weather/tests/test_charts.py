import datetime as dt
import json
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from weather import charts
from weather.models import DailyRollup, HourlyRollup, Observation, Station
from weather.units import UnitPrefs

from .helpers import make_station

UTC = dt.UTC
DENVER = ZoneInfo('America/Denver')
T0 = dt.datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
IMPERIAL = UnitPrefs.for_system('imperial')
METRIC = UnitPrefs.for_system('metric')


class ResolutionTests(SimpleTestCase):
    def test_boundaries(self):
        self.assertEqual(charts.resolution_for(T0, T0 + dt.timedelta(days=3)), 'raw')
        self.assertEqual(charts.resolution_for(T0, T0 + dt.timedelta(days=3, seconds=1)), 'hourly')
        self.assertEqual(charts.resolution_for(T0, T0 + dt.timedelta(days=120)), 'hourly')
        self.assertEqual(charts.resolution_for(T0, T0 + dt.timedelta(days=121)), 'daily')

    def test_rain_known(self):
        self.assertTrue(charts.rain_known(3.0, 0.1, 0.9))     # measured rain counts however partial the day
        self.assertFalse(charts.rain_known(0.0, 0.1, 0.9))    # an untrustworthy zero is not a dry day
        self.assertTrue(charts.rain_known(0.0, 0.95, 0.9))
        self.assertFalse(charts.rain_known(None, 1.0, 0.9))


class HistoryTests(TestCase):
    def setUp(self):
        self.station = make_station()

    def test_raw_series_converted_and_outage_breaks_the_line(self):
        for m in (5, 10, 15):
            Observation.objects.create(station=self.station, timestamp=T0 + dt.timedelta(minutes=m), source='api', temp_c=0.0)
        Observation.objects.create(station=self.station, timestamp=T0 + dt.timedelta(hours=6), source='api', temp_c=100.0)
        h = charts.history(self.station, T0, T0 + dt.timedelta(hours=7), IMPERIAL)
        self.assertEqual(h['resolution'], 'raw')
        self.assertEqual(h['series']['temp'], [32.0, 32.0, 32.0, None, 212.0])
        self.assertEqual(h['series']['time'][0], int(T0.timestamp() * 1000))   # plotted at the interval's start

    def test_hourly_uses_rollups(self):
        HourlyRollup.objects.create(station=self.station, period_start=T0, temp_avg_c=10.0, temp_min_c=8.0, temp_max_c=12.0, rain_mm=25.4)
        h = charts.history(self.station, T0, T0 + dt.timedelta(days=10), IMPERIAL)
        self.assertEqual(h['resolution'], 'hourly')
        self.assertEqual((h['series']['temp'][0], h['series']['temp_min'][0], h['series']['rain'][0]), (50.0, 46.4, 1.0))

    def test_daily_hides_poorly_covered_temperatures_but_keeps_measured_rain(self):
        DailyRollup.objects.create(station=self.station, date=dt.date(2026, 3, 1), temp_avg_c=10.0, temp_coverage=0.2,
                                   rain_mm=5.0, rain_coverage=0.2)
        DailyRollup.objects.create(station=self.station, date=dt.date(2026, 3, 2), temp_avg_c=12.0, temp_coverage=1.0,
                                   rain_mm=0.0, rain_coverage=0.3)
        start, _ = charts.local_range(self.station, dt.date(2026, 1, 1), dt.date(2026, 1, 1))
        _, end = charts.local_range(self.station, dt.date(2026, 6, 1), dt.date(2026, 6, 1))
        s = charts.history(self.station, start, end, METRIC)['series']
        self.assertEqual(s['temp'], [None, 12.0])
        self.assertEqual(s['rain'], [5.0, None])

    def test_csv_has_units_and_local_times(self):
        Observation.objects.create(station=self.station, timestamp=T0, source='api', temp_c=20.0)
        text = charts.history_csv(self.station, T0 - dt.timedelta(hours=1), T0, METRIC)
        header, first = text.splitlines()[:2]
        self.assertIn('time (America/Denver)', header)
        self.assertIn('temperature (°C)', header)
        self.assertTrue(first.startswith('2026-07-01 05:55,20.0'))   # interval start, in Denver time


class WindRoseTests(TestCase):
    def test_bins_and_shares(self):
        st = make_station()
        rows = [(350, 3.0), (10, 3.0), (180, 8.0), (90, 0.2)]   # two northerlies, a strong southerly, one calm
        for i, (direction, speed) in enumerate(rows, start=1):
            Observation.objects.create(station=st, timestamp=T0 + dt.timedelta(minutes=5 * i), source='api',
                                       wind_dir_deg=direction, wind_speed_ms=speed)
        rose = charts.wind_rose(st, T0, T0 + dt.timedelta(hours=1), IMPERIAL)
        n_total = sum(row[0] for row in rose['values'])
        s_bin = [row[8] for row in rose['values']]
        self.assertEqual(rose['calm_pct'], 25.0)
        self.assertEqual(n_total, 50.0)                         # 350° and 10° both land in N
        self.assertEqual(s_bin, [0, 0, 0, 25.0, 0])             # 8 m/s = 17.9 mph → 15–20 bin
        self.assertAlmostEqual(sum(map(sum, rose['values'])) + rose['calm_pct'], 100.0)


class CalendarAndYoyTests(TestCase):
    def setUp(self):
        self.station = make_station()
        for day, high, cov, rain in ((dt.date(2024, 2, 28), 10.0, 1.0, 2.0), (dt.date(2024, 2, 29), 11.0, 1.0, 3.0),
                                     (dt.date(2024, 3, 1), 12.0, 0.4, 0.0), (dt.date(2025, 3, 1), 9.0, 1.0, 4.0)):
            DailyRollup.objects.create(station=self.station, date=day, temp_max_c=high, temp_coverage=cov,
                                       rain_mm=rain, rain_coverage=cov)

    def test_calendar_skips_poor_days(self):
        cal = charts.calendar(self.station, 2024, 'high', METRIC)
        self.assertEqual([d for d, _ in cal['days']], ['2024-02-28', '2024-02-29'])

    def test_yoy_lines_up_days_across_leap_years(self):
        yoy = charts.year_over_year(self.station, 'rain', METRIC)
        by_year = {s['year']: s['values'] for s in yoy['series']}
        mar1 = (dt.date(2000, 3, 1) - dt.date(2000, 1, 1)).days
        self.assertEqual(by_year[2024][mar1 - 1], 5.0)          # Feb 29 has its own slot
        self.assertIsNone(by_year[2024][mar1])                  # a 40 %-covered zero is not a dry day
        self.assertEqual(by_year[2025][mar1], 4.0)              # Mar 1 is the same slot every year

    def test_years_available(self):
        self.assertEqual(charts.years_available(self.station), [2024, 2025])


class EndpointTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('owner', password='x' * 16)
        self.other = User.objects.create_user('other', password='x' * 16)
        for user in (self.owner, self.other):
            TOTPDevice.objects.create(user=user, name='phone', confirmed=True)
        self.station = make_station(owner=self.owner)
        self.data = reverse('weather:station-chart-data', args=[self.station.slug])
        self.page = reverse('weather:station-charts', args=[self.station.slug])

    def test_private_station_data_is_not_served(self):
        self.assertEqual(self.client.get(self.data).status_code, 404)
        self.assertEqual(self.client.get(self.page).status_code, 302)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.data).status_code, 404)
        self.assertEqual(self.client.get(self.page).status_code, 404)

    def test_public_station_all_kinds(self):
        Station.objects.update(is_public=True)
        DailyRollup.objects.create(station=self.station, date=dt.date(2026, 1, 1), temp_max_c=5.0, temp_coverage=1.0)
        for query in ('kind=history&range=24h', 'kind=history&start=2026-01-01&end=2026-01-31', 'kind=history&range=all',
                      'kind=rose&range=7d', 'kind=calendar&year=2026&metric=rain', 'kind=yoy&metric=high'):
            with self.subTest(query=query):
                response = self.client.get(f'{self.data}?{query}')
                self.assertEqual(response.status_code, 200)
                self.assertIn('units', json.loads(response.content))

    def test_csv_download(self):
        Station.objects.update(is_public=True)
        response = self.client.get(f'{self.data}?kind=history&start=2026-07-01&end=2026-07-02&format=csv')
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('test-station-20260701-20260703.csv', response['Content-Disposition'])

    def test_year_to_date_starts_at_local_new_year(self):
        from unittest import mock
        Station.objects.update(is_public=True)
        now = dt.datetime(2026, 10, 1, 18, 0, tzinfo=UTC)
        with mock.patch('weather.views.timezone.now', return_value=now):
            data = json.loads(self.client.get(f'{self.data}?kind=history&range=ytd').content)
        jan1 = dt.datetime(2026, 1, 1, tzinfo=DENVER)          # midnight in Denver = 07:00 UTC
        self.assertEqual(data['start'], int(jan1.timestamp() * 1000))
        self.assertEqual(data['end'], int(now.timestamp() * 1000))
        self.assertEqual(data['resolution'], 'daily')

    def test_reversed_custom_dates_are_swapped(self):
        Station.objects.update(is_public=True)
        data = json.loads(self.client.get(f'{self.data}?kind=history&start=2026-07-10&end=2026-07-01').content)
        self.assertLess(data['start'], data['end'])

    def test_bad_input(self):
        Station.objects.update(is_public=True)
        self.assertEqual(self.client.get(f'{self.data}?kind=nope').status_code, 400)
        self.assertEqual(self.client.get(f'{self.data}?kind=calendar&year=abc').status_code, 400)


class ChartsHomeTests(TestCase):
    def test_header_link_goes_to_the_public_station(self):
        make_station(is_public=True)
        self.assertRedirects(self.client.get(reverse('weather:charts')), '/stations/test-station/charts/',
                             fetch_redirect_response=False)

    def test_no_public_station_goes_home(self):
        self.assertRedirects(self.client.get(reverse('weather:charts')), '/', fetch_redirect_response=False)
