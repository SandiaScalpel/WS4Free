from unittest import mock

from django.core.checks import run_checks
from django.test import TestCase, override_settings
from django.urls import reverse

from weather.checks import TZ_CHECK_ID, probe_local_day_truncation


class TimeZoneCheckTests(TestCase):
    databases = {'default'}

    def _tz_warnings(self):
        return [m for m in run_checks(databases=['default']) if m.id == TZ_CHECK_ID]

    def test_probe_truncates_to_local_day(self):
        # 03:00 UTC on Jan 15 is still Jan 14 in New York.
        day = probe_local_day_truncation()
        self.assertEqual((day.year, day.month, day.day), (2026, 1, 14))

    def test_no_warning_when_database_converts_time_zones(self):
        self.assertEqual(self._tz_warnings(), [])

    def test_warns_when_truncation_returns_null(self):
        # What MySQL does when mysql_tzinfo_to_sql was never run.
        with mock.patch('weather.checks.probe_local_day_truncation', return_value=None):
            warnings = self._tz_warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn('mysql_tzinfo_to_sql', warnings[0].hint)


class HomePageTests(TestCase):
    def test_visitor_sees_no_public_stations_message(self):
        response = self.client.get(reverse('weather:home'))
        self.assertContains(response, "doesn't have any public weather stations")
        self.assertNotContains(response, 'Connect a station')

    def test_signed_in_user_without_stations_gets_setup_steps(self):
        from django.contrib.auth import get_user_model
        from django_otp.plugins.otp_totp.models import TOTPDevice
        user = get_user_model().objects.create_user('alice', password='x' * 16)
        TOTPDevice.objects.create(user=user, name='phone', confirmed=True)
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse('weather:home')), 'Connect a station')


class IngestRedirectTests(TestCase):
    @override_settings(SECURE_SSL_REDIRECT=True)
    def test_ingest_is_not_redirected_to_https(self):
        # Consoles upload over plain HTTP and do not follow redirects.
        response = self.client.get('/ingest/ambient/x/')
        self.assertNotEqual(response.status_code, 301)

    @override_settings(SECURE_SSL_REDIRECT=True)
    def test_other_pages_are_redirected(self):
        self.assertEqual(self.client.get('/').status_code, 301)
