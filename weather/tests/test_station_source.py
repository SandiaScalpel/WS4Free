"""Station source type (Ambient, Ecowitt, WeeWX, other) and adding stations in the app."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from weather.models import Station

from .fixtures import MAC
from .helpers import make_station


def user(name, staff=False):
    u = get_user_model().objects.create_user(name, password='x' * 16, is_staff=staff)
    TOTPDevice.objects.create(user=u, name='phone', confirmed=True)
    return u


class SourceTests(TestCase):
    def setUp(self):
        self.owner = user('owner', staff=True)       # the MAC / Ambient API settings are for administrators' stations
        self.client.force_login(self.owner)

    def settings_post(self, station, **data):
        base = {'name': station.name, 'timezone': station.timezone, 'anemometer_height': '6.6'}
        return self.client.post(reverse('weather:station-settings', args=[station.slug]), {**base, **data})

    def test_station_without_mac(self):
        st = make_station(owner=self.owner, mac_address='', source=Station.SOURCE_WEEWX)
        self.assertIsNone(st.mac_address)
        self.assertEqual(st.passkey_candidates(), set())
        self.assertFalse(st.uses_ambient_api)
        make_station(owner=self.owner, name='Second', mac_address=None, source=Station.SOURCE_OTHER)   # several MAC-less stations are fine

    def test_setup_page_follows_source(self):
        st = make_station(owner=self.owner, mac_address=None, source=Station.SOURCE_WEEWX)
        url = reverse('weather:station-setup', args=[st.slug])
        page = self.client.get(url)
        self.assertContains(page, '[[Wunderground]]')
        self.assertContains(page, f'server_url = http://testserver/ingest/ambient/{st.push_token}/')
        self.assertContains(page, f'import_weewx {st.slug}')
        self.assertContains(page, 'Uploader ID')
        Station.objects.filter(pk=st.pk).update(source=Station.SOURCE_ECOWITT)
        page = self.client.get(url)
        self.assertContains(page, 'WS View Plus')
        self.assertContains(page, "proto: 'ecowitt'")
        self.assertNotContains(page, '[[Wunderground]]')

    def test_header_shows_source_and_mac_only_if_set(self):
        st = make_station(owner=self.owner, mac_address=None, source=Station.SOURCE_WEEWX)
        page = self.client.get(reverse('weather:station-setup', args=[st.slug]))
        self.assertContains(page, 'WeeWX · America/Denver')

    def test_settings_change_source_and_add_mac_once(self):
        st = make_station(owner=self.owner, mac_address=None, source=Station.SOURCE_OTHER)
        self.settings_post(st, source='ambient', mac_address='48-3f-da-12-34-56', ambient_api_enabled='on')
        st.refresh_from_db()
        self.assertEqual((st.source, st.mac_address, st.ambient_api_enabled), ('ambient', MAC, True))
        self.settings_post(st, source='ambient', mac_address='00:11:22:33:44:55')
        st.refresh_from_db()
        self.assertEqual(st.mac_address, MAC)                                  # fixed once set

    def test_ambient_api_needs_a_mac(self):
        st = make_station(owner=self.owner, mac_address=None, source=Station.SOURCE_OTHER)
        response = self.settings_post(st, source='ambient', mac_address='', ambient_api_enabled='on')
        self.assertContains(response, 'needs the station')
        self.assertEqual(Station.objects.get(pk=st.pk).source, 'other')

    def test_settings_post_without_source_or_mac_keeps_them(self):
        st = make_station(owner=self.owner, source=Station.SOURCE_ECOWITT)
        self.settings_post(st)
        st.refresh_from_db()
        self.assertEqual((st.source, st.mac_address), ('ecowitt', MAC))

    @mock.patch('weather.management.commands.poll_ambient.AmbientClient')
    def test_poller_skips_stations_that_dont_use_the_api(self, client_cls):
        make_station(owner=self.owner, mac_address=None, source=Station.SOURCE_WEEWX, ambient_api_enabled=True)
        make_station(owner=self.owner, name='Eco', mac_address='02:00:00:00:00:31', source=Station.SOURCE_ECOWITT,
                     ambient_api_enabled=True)
        call_command('poll_ambient')
        client_cls.assert_not_called()


class CreateStationTests(TestCase):
    def setUp(self):
        self.admin = user('admin', staff=True)
        self.url = reverse('weather:station-create')

    def test_staff_adds_a_weewx_station(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.url, {'name': 'Hill Farm', 'source': 'weewx', 'mac_address': '',
                                               'timezone': 'Europe/London', 'latitude': '51.5', 'longitude': '-0.1'})
        st = Station.objects.get()
        self.assertRedirects(response, reverse('weather:station-setup', args=[st.slug]))
        self.assertEqual((st.owner, st.source, st.mac_address, st.ambient_api_enabled, st.is_public),
                         (self.admin, 'weewx', None, False, False))

    def test_ambient_station_with_mac_polls_the_api(self):
        self.client.force_login(self.admin)
        self.client.post(self.url, {'name': 'Yard', 'source': 'ambient', 'mac_address': '483fda123456', 'timezone': 'UTC'})
        st = Station.objects.get()
        self.assertEqual((st.mac_address, st.ambient_api_enabled), (MAC, True))
        response = self.client.post(self.url, {'name': 'Dup', 'source': 'ambient', 'mac_address': MAC, 'timezone': 'UTC'})
        self.assertContains(response, 'Another station already has this MAC')

    def test_only_staff(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(user('plain'))
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_links(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get('/'), 'href="/stations/new/"')            # no stations yet
        make_station(owner=self.admin, is_public=True)
        self.assertContains(self.client.get(reverse('weather:stations')), 'href="/stations/new/"')
