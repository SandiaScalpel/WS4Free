from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from weather.models import Station

from .fixtures import MAC
from .helpers import T0, make_station, obs


def _post(station, **overrides):
    data = {'name': station.name, 'timezone': station.timezone, 'latitude': '40.015000',
            'longitude': '-105.270500', 'elevation': '5430', 'anemometer_height': '10', 'ambient_api_enabled': 'on'}
    if station.is_public:
        data['is_public'] = 'on'
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


@override_settings(DEFAULT_UNIT_SYSTEM='imperial')
class StationSettingsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('owner', password='x' * 16)
        self.other = User.objects.create_user('other', password='x' * 16)
        for user in (self.owner, self.other):
            TOTPDevice.objects.create(user=user, name='phone', confirmed=True)
        self.station = make_station(owner=self.owner)
        self.url = reverse('weather:station-settings', args=[self.station.slug])
        self.client.force_login(self.owner)

    def test_owner_makes_station_public(self):
        response = self.client.post(self.url, _post(self.station, is_public='on'), follow=True)
        self.assertContains(response, 'is now public')
        self.assertTrue(Station.objects.get().is_public)
        # …and an anonymous visitor now sees it.
        self.client.logout()
        self.assertContains(self.client.get('/'), 'Test Station')

    def test_unchecking_makes_it_private_again(self):
        Station.objects.update(is_public=True)
        self.station.refresh_from_db()
        self.client.post(self.url, _post(self.station, is_public=None))
        self.assertFalse(Station.objects.get().is_public)

    def test_other_users_and_visitors_cannot_reach_it(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, _post(self.station, is_public='on')).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertFalse(Station.objects.get().is_public)

    def test_elevation_entered_in_feet_stored_in_metres(self):
        self.client.post(self.url, _post(self.station, elevation='5430'))
        self.assertAlmostEqual(Station.objects.get().elevation_m, 1655.1, places=1)
        self.assertAlmostEqual(Station.objects.get().anemometer_height_m, 3.048, places=3)   # 10 ft
        self.assertContains(self.client.get(self.url), 'value="5430"')

    def test_identity_fields_cannot_be_changed(self):
        self.client.post(self.url, _post(self.station, slug='hacked', mac_address='00:00:00:00:00:01',
                                         archive_interval_s='60', push_passkey='x'))
        st = Station.objects.get()
        self.assertEqual((st.slug, st.mac_address, st.archive_interval_s, st.push_passkey),
                         ('test-station', MAC, 300, ''))

    def test_invalid_input_rejected_and_header_shows_saved_name(self):
        response = self.client.post(self.url, _post(self.station, name='New name', timezone='Mars/Olympus', latitude='95'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'not an IANA time zone')
        self.assertContains(response, 'Latitude must be between')
        self.assertContains(response, '<h1 class="text-2xl font-bold">Test Station</h1>', html=True)
        self.assertEqual(Station.objects.get().name, 'Test Station')

    def test_timezone_change_marks_whole_history_for_recompute(self):
        obs(self.station, 0, temp_c=10.0)
        obs(self.station, 60, temp_c=11.0)
        Station.objects.update(rollup_dirty_from=None)
        self.client.post(self.url, _post(self.station, timezone='America/Phoenix'))
        st = Station.objects.get()
        self.assertEqual(st.timezone, 'America/Phoenix')
        self.assertEqual(st.rollup_dirty_from, T0)

    def test_other_edits_do_not_mark_history_dirty(self):
        obs(self.station, 0, temp_c=10.0)
        Station.objects.update(rollup_dirty_from=None)
        self.client.post(self.url, _post(self.station, name='Renamed'))
        self.assertIsNone(Station.objects.get().rollup_dirty_from)

    def test_forget_passkey(self):
        Station.objects.update(push_passkey=MAC)
        self.client.post(reverse('weather:station-forget-passkey', args=[self.station.slug]))
        self.assertEqual(Station.objects.get().push_passkey, '')
