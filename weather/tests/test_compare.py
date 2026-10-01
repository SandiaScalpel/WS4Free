"""Station comparison (weather.compare and its views)."""
import datetime as dt

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from weather import compare
from weather.models import DailyRollup, Observation
from weather.units import UnitPrefs

from .helpers import make_station

UTC = dt.UTC
METRIC = UnitPrefs.for_system('metric')


class CompareTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('owner2', password='x' * 16)
        self.house = make_station(owner=self.owner, name='House', mac_address='02:00:00:00:00:11', is_public=True)
        self.field = make_station(owner=self.owner, name='Field', mac_address='02:00:00:00:00:12', is_public=True)
        self.secret = make_station(owner=self.owner, name='Secret', mac_address='02:00:00:00:00:13')
        self.t0 = dt.datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
        for i in range(3):
            ts = self.t0 + dt.timedelta(minutes=5 * (i + 1))
            Observation.objects.create(station=self.house, timestamp=ts, source='api', temp_c=20.0 + i, rain_mm=0.2)
            Observation.objects.create(station=self.field, timestamp=ts, source='api', temp_c=18.0 + i)
        local = dt.date(2026, 7, 1)
        DailyRollup.objects.create(station=self.house, date=local, temp_max_c=30, temp_min_c=15, temp_avg_c=22,
                                   rain_mm=0.6, coverage=1, temp_coverage=1, rain_coverage=1)
        DailyRollup.objects.create(station=self.field, date=local, temp_max_c=28, temp_min_c=11, temp_avg_c=19,
                                   rain_mm=0.0, coverage=0.5, temp_coverage=0.5, rain_coverage=0.5)

    def test_series_line_up_and_summaries_follow_report_rules(self):
        data = compare.compare([self.house, self.field], self.t0, self.t0 + dt.timedelta(hours=1), METRIC)
        self.assertEqual(data['resolution'], 'raw')
        house, field = data['stations']
        self.assertEqual(house['series']['time'], field['series']['time'])      # same buckets → subtractable
        self.assertEqual(house['series']['temp'], [20.0, 21.0, 22.0])
        self.assertEqual(field['series']['temp'], [18.0, 19.0, 20.0])
        self.assertTrue(data['aligned'])
        self.assertEqual((house['summary']['temp_high'], house['summary']['temp_mean']), (30.0, 22.0))
        # Half a day of data: its extremes count, its mean doesn't (≥ 90 % rule), and it shows as incomplete.
        self.assertEqual(field['summary']['temp_low'], 11.0)
        self.assertIsNone(field['summary']['temp_mean'])
        self.assertEqual((field['summary']['days_with_data'], field['summary']['days']), (0, 1))

    def test_daily_buckets_in_different_time_zones_are_not_aligned(self):
        self.field.timezone = 'Europe/London'
        self.field.save()
        data = compare.compare([self.house, self.field], self.t0 - dt.timedelta(days=200), self.t0, METRIC)
        self.assertEqual(data['resolution'], 'daily')
        self.assertFalse(data['aligned'])

    def test_data_endpoint_only_returns_stations_the_viewer_may_see(self):
        url = reverse('weather:compare-data')
        query = f'?s={self.house.slug}&s={self.secret.slug}&s={self.field.slug}&start=2026-07-01'
        names = [s['name'] for s in self.client.get(url + query).json()['stations']]
        self.assertEqual(names, ['House', 'Field'])                       # in the order chosen, private one dropped
        self.assertEqual(self.client.get(url + f'?s={self.secret.slug}').status_code, 400)
        TOTPDevice.objects.create(user=self.owner, name='phone', confirmed=True)
        self.client.force_login(self.owner)
        names = [s['name'] for s in self.client.get(url + query).json()['stations']]
        self.assertEqual(names, ['House', 'Secret', 'Field'])

    def test_all_range_starts_at_the_earliest_chosen_station(self):
        DailyRollup.objects.create(station=self.field, date=dt.date(2025, 3, 1), coverage=1)
        data = self.client.get(reverse('weather:compare-data') + f'?s={self.house.slug}&s={self.field.slug}&range=all').json()
        self.assertEqual(dt.datetime.fromtimestamp(data['start'] / 1000, UTC).date(), dt.date(2025, 3, 1))

    def test_page_preselects_two_and_honours_a_station_link(self):
        response = self.client.get(reverse('weather:compare') + f'?s={self.field.slug}')
        self.assertContains(response, f'<script id="compare-chosen" type="application/json">["{self.field.slug}", "{self.house.slug}"]</script>', html=False)
        self.assertNotContains(response, 'Secret')

    def test_compare_links_appear_only_with_several_stations(self):
        charts_url = reverse('weather:station-charts', args=[self.house.slug])
        self.assertContains(self.client.get(charts_url), f'href="/compare/?s={self.house.slug}"')
        self.assertContains(self.client.get('/help/'), 'href="/compare/"')
        self.field.delete()
        self.assertNotContains(self.client.get(charts_url), '/compare/')
        self.assertContains(self.client.get(reverse('weather:compare')), 'needs at least two stations')
