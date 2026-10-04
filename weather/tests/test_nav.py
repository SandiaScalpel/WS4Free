"""Header navigation: the Stations menu and the staff "Manage stations" link."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from .helpers import make_station


class NavTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user('admin', password='x' * 16, is_staff=True)
        self.user = User.objects.create_user('someone', password='x' * 16)
        for user in (self.staff, self.user):         # a second factor, so no 2FA reminder page
            TOTPDevice.objects.create(user=user, name='phone', confirmed=True)
        self.mesa = make_station(owner=self.staff, name='Mesa Ridge', slug='mesa', is_public=True)

    def test_staff_can_manage_stations_with_only_one(self):
        self.client.force_login(self.staff)
        html = self.client.get(reverse('weather:help')).content.decode()
        self.assertIn(f'href="{reverse("weather:stations")}" class="nav-link block" role="menuitem">Manage stations', html)
        self.assertNotIn('All stations', html)                       # one station: no Stations menu
        self.client.force_login(self.user)
        self.assertNotIn('Manage stations', self.client.get(reverse('weather:help')).content.decode())

    def test_stations_menu_lists_every_visible_station(self):
        make_station(owner=self.staff, name='Hill Farm', slug='hill', is_public=True, mac_address='00:00:00:00:00:02')
        make_station(owner=self.staff, name='Private Place', slug='private', mac_address='00:00:00:00:00:03')
        html = self.client.get(reverse('weather:station', args=['mesa'])).content.decode()
        self.assertIn('All stations', html)
        self.assertIn(f'href="{reverse("weather:station", args=["hill"])}"', html)
        self.assertIn(f'href="{reverse("weather:station", args=["mesa"])}" class="nav-link block truncate" role="menuitem" aria-current="page"', html)
        self.assertNotIn('Private Place', html)                      # visitors only see public stations
