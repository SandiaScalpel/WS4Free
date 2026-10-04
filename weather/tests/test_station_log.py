"""Station log (StationEvent): owner pages and chart markers."""
import datetime as dt

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from weather.models import Station, StationEvent

from .helpers import make_station

UTC = dt.UTC


class StationLogTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('owner', password='x' * 16)
        self.other = User.objects.create_user('other', password='x' * 16)
        for u in (self.owner, self.other):
            TOTPDevice.objects.create(user=u, name='phone', confirmed=True)
        self.station = make_station(owner=self.owner, is_public=True)        # America/Denver
        self.url = reverse('weather:station-log', args=[self.station.slug])

    def add(self, **data):
        base = {'date': '2026-10-03', 'kind': 'moved', 'title': 'Moved 80 ft SW', 'is_public': 'on'}
        return self.client.post(self.url, {**base, **data})

    def test_add_with_and_without_time_in_station_time(self):
        self.client.force_login(self.owner)
        self.assertRedirects(self.add(time='14:30'), self.url)
        e = StationEvent.objects.get()
        self.assertEqual((e.occurred_at, e.has_time, e.created_by), (dt.datetime(2026, 10, 3, 20, 30, tzinfo=UTC), True, self.owner))
        self.add(title='Battery', kind='battery', is_public='')
        e = StationEvent.objects.get(title='Battery')
        self.assertEqual((e.occurred_at, e.has_time, e.is_public), (dt.datetime(2026, 10, 3, 6, 0, tzinfo=UTC), False, False))
        page = self.client.get(self.url)
        self.assertContains(page, 'Moved 80 ft SW')
        self.assertContains(page, '2:30 PM')
        self.assertContains(page, 'Station log</a>')

    def test_edit_and_delete(self):
        self.client.force_login(self.owner)
        self.add()
        e = StationEvent.objects.get()
        edit_url = reverse('weather:station-log-edit', args=[self.station.slug, e.pk])
        self.assertContains(self.client.get(self.url, {'edit': e.pk}), 'value="2026-10-03"')
        self.client.post(edit_url, {'date': '2026-10-02', 'kind': 'moved', 'title': 'Moved (corrected date)', 'is_public': 'on'})
        e.refresh_from_db()
        self.assertEqual((e.title, e.occurred_at.date()), ('Moved (corrected date)', dt.date(2026, 10, 2)))
        self.client.post(reverse('weather:station-log-delete', args=[self.station.slug, e.pk]))
        self.assertFalse(StationEvent.objects.exists())

    def test_validation(self):
        self.client.force_login(self.owner)
        self.assertContains(self.add(date='2099-01-01'), 'in the future')
        self.assertContains(self.add(title=''), 'This field is required')
        self.assertFalse(StationEvent.objects.exists())

    def test_only_the_owner(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)              # sign in first
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.add().status_code, 404)
        e = StationEvent.objects.create(station=self.station, occurred_at=dt.datetime(2026, 10, 3, tzinfo=UTC), title='x')
        self.assertEqual(self.client.post(reverse('weather:station-log-delete', args=[self.station.slug, e.pk])).status_code, 404)
        self.assertTrue(StationEvent.objects.exists())

    def test_chart_markers_respect_privacy_and_range(self):
        mk = lambda d, title, public: StationEvent.objects.create(
            station=self.station, occurred_at=dt.datetime(2026, 10, d, 18, tzinfo=UTC), title=title, kind='moved', is_public=public)
        mk(3, 'Moved', True)
        mk(3, 'Private note', False)
        mk(20, 'Later', True)
        url = reverse('weather:station-chart-data', args=[self.station.slug])
        query = {'kind': 'history', 'start': '2026-10-01', 'end': '2026-10-05'}
        public = self.client.get(url, query).json()['events']
        self.assertEqual([e['title'] for e in public], ['Moved'])
        self.assertEqual((public[0]['t'], public[0]['kind']), (int(dt.datetime(2026, 10, 3, 18, tzinfo=UTC).timestamp() * 1000), 'Moved'))
        self.client.force_login(self.owner)
        self.assertEqual([e['title'] for e in self.client.get(url, query).json()['events']], ['Moved', 'Private note'])
