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


class UserManagementTests(TestCase):
    """Users page (staff): add with a welcome link, edit, new link, deactivate."""

    def setUp(self):
        from weather.tests.helpers import make_station
        self.make_station = make_station
        User = get_user_model()
        self.admin = User.objects.create_user('admin', password='x' * 16, is_staff=True)
        self.member = User.objects.create_user('member', password='x' * 16)
        for u in (self.admin, self.member):
            TOTPDevice.objects.create(user=u, name='phone', confirmed=True)
        self.client.force_login(self.admin)

    def _create(self, **data):
        return self.client.post(reverse('accounts:user-create'),
                                {'username': 'newbie', 'first_name': 'New', 'last_name': 'Person', **data}, follow=True)

    def test_only_staff(self):
        self.client.force_login(self.member)
        for url in (reverse('accounts:users'), reverse('accounts:user-create'),
                    reverse('accounts:user-detail', args=[self.admin.pk])):
            self.assertEqual(self.client.get(url).status_code, 302, url)        # to sign-in

    def test_add_user_and_welcome_link(self):
        response = self._create()
        newbie = get_user_model().objects.get(username='newbie')
        self.assertFalse(newbie.has_usable_password())
        link = response.context['link']
        self.assertIn('/account/welcome/', link)
        # Shown once only.
        self.assertIsNone(self.client.get(reverse('accounts:user-detail', args=[newbie.pk])).context['link'])
        # The newcomer chooses a password.
        self.client.logout()
        path = link.split('testserver', 1)[1]
        form_page = self.client.get(path, follow=True)
        self.assertTrue(form_page.context['validlink'])
        set_url = form_page.redirect_chain[-1][0]
        done = self.client.post(set_url, {'new_password1': 'correct horse battery staple',
                                          'new_password2': 'correct horse battery staple'})
        self.assertRedirects(done, reverse('two_factor:login'), fetch_redirect_response=False)
        newbie.refresh_from_db()
        self.assertTrue(newbie.check_password('correct horse battery staple'))
        # Used once: the link no longer works.
        self.client.cookies.clear()
        self.assertFalse(self.client.get(path, follow=True).context['validlink'])

    def test_forged_and_inactive_links_fail(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode
        uid = urlsafe_base64_encode(force_bytes(self.member.pk))
        self.client.logout()
        bad = self.client.get(reverse('accounts:welcome', args=[uid, 'abc-123']), follow=True)
        self.assertFalse(bad.context['validlink'])
        token = default_token_generator.make_token(self.member)
        self.member.is_active = False
        self.member.save()
        self.assertFalse(self.client.get(reverse('accounts:welcome', args=[uid, token]), follow=True).context['validlink'])

    def test_duplicate_username_case_insensitive(self):
        response = self._create(username='MEMBER')
        self.assertFormError(response.context['form'], 'username', 'Someone already has this username.')

    def test_new_link(self):
        response = self.client.post(reverse('accounts:user-new-link', args=[self.member.pk]), follow=True)
        self.assertIn('/account/welcome/', response.context['link'])

    def test_staff_rules(self):
        url = reverse('accounts:user-detail', args=[self.admin.pk])
        response = self.client.post(url, {'first_name': '', 'last_name': '', 'email': ''})   # is_staff unticked
        self.assertFormError(response.context['form'], 'is_staff', 'You can\'t remove your own administrator access.')
        self.assertTrue(get_user_model().objects.get(pk=self.admin.pk).is_staff)
        # Promote the member, then demote them: fine while another administrator remains.
        member_url = reverse('accounts:user-detail', args=[self.member.pk])
        self.client.post(member_url, {'first_name': 'M', 'last_name': '', 'email': '', 'is_staff': 'on'})
        self.assertTrue(get_user_model().objects.get(pk=self.member.pk).is_staff)
        self.client.post(member_url, {'first_name': 'M', 'last_name': '', 'email': ''})
        self.assertFalse(get_user_model().objects.get(pk=self.member.pk).is_staff)

    def test_cannot_deactivate_self(self):
        self.client.post(reverse('accounts:user-set-active', args=[self.admin.pk]), {'active': '0'})
        self.assertTrue(get_user_model().objects.get(pk=self.admin.pk).is_active)

    def test_deactivate_transfers_stations_and_removes_access(self):
        from weather.models import Station, StationAccess, StationEvent
        owned = self.make_station(owner=self.member)
        other = self.make_station(owner=self.admin, name='Other', slug='other', mac_address=None)
        StationAccess.objects.create(station=other, user=self.member, role='manager')
        url = reverse('accounts:user-set-active', args=[self.member.pk])
        self.client.post(url, {'active': '0'})                       # owns a station: refused
        self.assertTrue(get_user_model().objects.get(pk=self.member.pk).is_active)
        self.client.post(url, {'active': '0', 'take_stations': '1'})
        member = get_user_model().objects.get(pk=self.member.pk)
        self.assertFalse(member.is_active)
        self.assertEqual(Station.objects.get(pk=owned.pk).owner, self.admin)
        self.assertFalse(StationAccess.objects.exists())             # not even kept on as a manager
        self.assertTrue(StationEvent.objects.filter(station=owned).exists())
        # Signed out: their session no longer authenticates.
        self.client.force_login(self.admin)
        other_client = self.client_class()
        other_client.force_login(member)
        self.assertEqual(other_client.get(reverse('accounts:settings')).status_code, 302)
        # Reactivate.
        self.client.post(url, {'active': '1'})
        self.assertTrue(get_user_model().objects.get(pk=self.member.pk).is_active)
