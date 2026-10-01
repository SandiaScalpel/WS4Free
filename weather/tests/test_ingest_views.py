from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from weather.models import IngestCapture, LatestReading, Observation, Station

from .fixtures import AMBIENT_QUERY, ECOWITT_BODY, ECOWITT_PASSKEY, MAC, PUSH_NOW, WU_QUERY
from .helpers import make_station

NOW = mock.patch('weather.ingest.views.timezone.now', return_value=PUSH_NOW)


@override_settings(SECURE_SSL_REDIRECT=True)   # as in production: ingest must still not redirect
class PushEndpointTests(TestCase):
    def setUp(self):
        NOW.start()
        self.addCleanup(NOW.stop)
        self.station = make_station()
        self.ambient = f'/ingest/ambient/{self.station.push_token}/'
        self.ecowitt = f'/ingest/ecowitt/{self.station.push_token}/'

    def test_ambient_get(self):
        response = self.client.get(f'{self.ambient}?{AMBIENT_QUERY}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'success\n')
        row = Observation.objects.get()
        self.assertEqual(row.source, Observation.SOURCE_AMBIENT_PUSH)
        self.assertEqual(row.timestamp.isoformat(), '2026-09-30T12:05:00+00:00')   # 12:03:17 → interval ending 12:05
        self.assertTrue(LatestReading.objects.filter(station=self.station).exists())

    def test_ambient_params_appended_without_question_mark(self):
        response = self.client.get(f'{self.ambient}&{AMBIENT_QUERY}')
        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(Observation.objects.get().humidity, 80)

    def test_params_glued_straight_onto_path(self):
        # Exactly what a WS-2902 (firmware AMBWeatherV4.3.8, User-Agent ESP8266)
        # sends: no '?' and no '&' between the configured path and its parameters.
        response = self.client.get(
            f'{self.ambient}stationtype=AMBWeatherV4.3.8&PASSKEY={MAC}&dateutc=2026-09-30+12:03:31'
            '&tempinf=73.0&humidityin=43&baromrelin=29.566&baromabsin=24.983&tempf=59.4&battout=1'
            '&humidity=79&winddir=338&windspeedmph=4.0&windgustmph=5.8&maxdailygust=18.3'
            '&hourlyrainin=0.000&eventrainin=2.559&dailyrainin=0.118&weeklyrainin=2.567'
            '&monthlyrainin=5.890&totalrainin=47.571&solarradiation=0.00&uv=0'
        )
        self.assertEqual(response.status_code, 200)
        row = Observation.objects.get()
        self.assertAlmostEqual(row.temp_c, 15.222, places=3)
        self.assertAlmostEqual(row.rain_daily_mm, 0.118 * 25.4)
        self.assertEqual(Station.objects.get().push_passkey, MAC)

    def test_path_configured_with_trailing_question_mark(self):
        response = self.client.get(f'{self.ambient}??{AMBIENT_QUERY}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Station.objects.get().push_passkey, MAC)

    def test_wunderground_protocol(self):
        self.assertEqual(self.client.get(f'{self.ambient}?{WU_QUERY}').status_code, 200)

    def test_ecowitt_post(self):
        response = self.client.post(self.ecowitt, ECOWITT_BODY, content_type='application/x-www-form-urlencoded')
        self.assertEqual(response.status_code, 200)
        row = Observation.objects.get()
        self.assertEqual(row.extra['soilmoisture1'], 34)
        self.assertEqual(Station.objects.get().push_passkey, ECOWITT_PASSKEY)

    def test_no_csrf_token_or_session_needed(self):
        client = self.client_class(enforce_csrf_checks=True)
        response = client.post(self.ecowitt, ECOWITT_BODY, content_type='application/x-www-form-urlencoded')
        self.assertEqual(response.status_code, 200)

    def test_unknown_token_is_404_and_not_logged(self):
        response = self.client.get(f'/ingest/ambient/not-a-token/?{AMBIENT_QUERY}')
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Observation.objects.exists())
        self.assertFalse(IngestCapture.objects.exists())

    def test_passkey_learned_then_enforced(self):
        self.client.get(f'{self.ambient}?{AMBIENT_QUERY}')
        imposter = AMBIENT_QUERY.replace(MAC, '00:11:22:33:44:55')
        response = self.client.get(f'{self.ambient}?{imposter}')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Observation.objects.get().sample_count, 1)
        self.assertTrue(IngestCapture.objects.filter(accepted=False).exists())

    def test_unexpected_passkey_is_accepted_but_flagged(self):
        query = AMBIENT_QUERY.replace(MAC, 'SOMETHING-ELSE')
        self.assertEqual(self.client.get(f'{self.ambient}?{query}').status_code, 200)
        self.assertIn("does not look like", IngestCapture.objects.get().note)

    def test_unparseable_upload_is_400_and_logged(self):
        response = self.client.get(f'{self.ambient}?stationtype=x')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(IngestCapture.objects.get().accepted)

    def test_capture_redacts_token(self):
        with override_settings(INGEST_CAPTURE=True):
            self.client.get(f'{self.ambient}?{AMBIENT_QUERY}')
        capture = IngestCapture.objects.get()
        self.assertNotIn(self.station.push_token, capture.path)
        self.assertTrue(capture.accepted)

    def test_rotating_token_disables_old_url(self):
        old = self.ambient
        self.station.rotate_push_token()
        self.assertEqual(self.client.get(f'{old}?{AMBIENT_QUERY}').status_code, 404)


class StationSetupPageTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('owner', password='x' * 16)
        self.other = User.objects.create_user('other', password='x' * 16)
        for user in (self.owner, self.other):
            TOTPDevice.objects.create(user=user, name='phone', confirmed=True)
        self.station = make_station(owner=self.owner)

    def test_owner_sees_upload_path(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('weather:station-setup', args=[self.station.slug]))
        self.assertContains(response, f'/ingest/ambient/{self.station.push_token}/')

    def test_other_user_gets_404(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('weather:station-setup', args=[self.station.slug])).status_code, 404)
        self.assertEqual(self.client.post(reverse('weather:station-rotate-token', args=[self.station.slug])).status_code, 404)

    def test_private_station_hidden_from_home(self):
        self.assertNotContains(self.client.get('/'), 'Test Station')
        Station.objects.update(is_public=True)
        self.assertContains(self.client.get('/'), 'Test Station')
