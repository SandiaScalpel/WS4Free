from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from .middleware import Require2FASetupMiddleware
from .models import LoginAttempt, UserProfile

User = get_user_model()
PASSWORD = 'correct-horse-battery-staple'


class ProfileTests(TestCase):
    def test_profile_created_with_user(self):
        user = User.objects.create_user('alice', password=PASSWORD)
        self.assertEqual(user.profile.theme, 'auto')


class LoginRateLimitTests(TestCase):
    def setUp(self):
        User.objects.create_user('alice', password=PASSWORD)

    def _login(self, password):
        return self.client.post(reverse('two_factor:login'), {
            'login_view-current_step': 'auth',
            'auth-username': 'alice',
            'auth-password': password,
        }, REMOTE_ADDR='203.0.113.5')

    def test_failures_are_recorded(self):
        self._login('wrong')
        self.assertTrue(LoginAttempt.objects.filter(username='alice', successful=False).exists())

    @override_settings(LOGIN_FAILURE_LIMIT=3)
    def test_locks_out_after_limit_even_with_right_password(self):
        for _ in range(3):
            self._login('wrong')
        response = self._login(PASSWORD)
        self.assertRedirects(response, reverse('two_factor:login'), fetch_redirect_response=False)
        self.assertNotIn('_auth_user_id', self.client.session)


class TwoFactorNagTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('alice', password=PASSWORD)
        self.client.force_login(self.user)

    def test_user_without_device_is_sent_to_nag(self):
        response = self.client.get(reverse('weather:home'))
        self.assertRedirects(response, reverse('accounts:2fa-nag'))

    def test_skip_lasts_for_the_session(self):
        self.client.get(reverse('accounts:2fa-skip'))
        self.assertEqual(self.client.get(reverse('weather:home')).status_code, 200)

    def test_user_with_device_is_not_nagged(self):
        TOTPDevice.objects.create(user=self.user, name='phone', confirmed=True)
        self.assertEqual(self.client.get(reverse('weather:home')).status_code, 200)

    def test_ingest_paths_are_never_redirected(self):
        # Consoles post without a session, but a misbehaving proxy could attach one;
        # the nag must never turn an upload into a redirect.
        request = RequestFactory().get('/ingest/ambient/token/')
        request.user = self.user
        request.session = {}
        sentinel = object()
        self.assertIs(Require2FASetupMiddleware(lambda r: sentinel)(request), sentinel)


class ThemeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('alice', password=PASSWORD)
        TOTPDevice.objects.create(user=self.user, name='phone', confirmed=True)

    def test_toggle_saves_for_signed_in_user(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('accounts:set-theme'), {'theme': 'dark'})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(UserProfile.objects.get(user=self.user).theme, 'dark')

    def test_toggle_rejects_unknown_value(self):
        self.client.force_login(self.user)
        self.client.post(reverse('accounts:set-theme'), {'theme': "x';alert(1)//"})
        self.assertEqual(UserProfile.objects.get(user=self.user).theme, 'auto')

    def test_toggle_is_noop_for_anonymous(self):
        self.assertEqual(self.client.post(reverse('accounts:set-theme'), {'theme': 'dark'}).status_code, 204)

    def test_saved_theme_is_applied_before_paint(self):
        UserProfile.objects.filter(user=self.user).update(theme='dark')
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('weather:home')), "var pref = 'dark';")

    def test_settings_page_changes_theme(self):
        self.client.force_login(self.user)
        self.client.post(reverse('accounts:settings'), {'theme': 'light'})
        self.assertEqual(UserProfile.objects.get(user=self.user).theme, 'light')


class PasswordChangeTests(TestCase):
    def test_clears_forced_change_flag(self):
        user = User.objects.create_user('alice', password=PASSWORD)
        TOTPDevice.objects.create(user=user, name='phone', confirmed=True)
        UserProfile.objects.filter(user=user).update(force_password_change=True)
        self.client.force_login(user)

        self.assertRedirects(self.client.get(reverse('weather:home')), reverse('accounts:password-change'))

        new = 'a-much-longer-new-passphrase'
        self.client.post(reverse('accounts:password-change'), {
            'old_password': PASSWORD, 'new_password1': new, 'new_password2': new,
        })
        self.assertFalse(UserProfile.objects.get(user=user).force_password_change)
        self.assertEqual(self.client.get(reverse('weather:home')).status_code, 200)


class PageRenderTests(TestCase):
    def test_login_page(self):
        self.assertContains(self.client.get(reverse('two_factor:login')), 'Sign in')

    def test_settings_page(self):
        user = User.objects.create_user('alice', password=PASSWORD)
        TOTPDevice.objects.create(user=user, name='phone', confirmed=True)
        self.client.force_login(user)
        response = self.client.get(reverse('accounts:settings'))
        self.assertContains(response, 'Passkeys')
        self.assertContains(response, 'Enabled')


class PasskeySignInRedirectTests(TestCase):
    """The passkey script sends you to the value of the field its nextFieldSelector names.
    By default that's the first input[name='next'] on the page: the header units
    switch's, which on the sign-in page is the sign-in page itself."""

    def config(self, url):
        import json
        import re
        html = self.client.get(url).content.decode()
        config = json.loads(re.search(r'<script id="otp_webauthn_config" type="application/json">(.*?)</script>', html).group(1))
        target = re.search(r'id="passkey-next" value="([^"]*)"', html).group(1)
        return config, target

    def test_passkey_goes_to_the_requested_page_not_back_to_sign_in(self):
        config, target = self.config('/account/login/?next=/stations/')
        self.assertEqual(config['nextFieldSelector'], '#passkey-next')
        self.assertEqual(target, '/stations/')

    def test_without_next_it_goes_home(self):
        config, target = self.config('/account/login/')
        self.assertEqual((config['nextFieldSelector'], target), ('#passkey-next', ''))
