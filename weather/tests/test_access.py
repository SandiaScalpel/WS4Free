"""Roles (weather.access): what visitors, viewers, managers, owners and staff can see
and do, the People tab, and ownership transfers."""
from django.contrib.auth import get_user_model
from django.db.models import ProtectedError
from django.test import TestCase, override_settings
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from weather import access
from weather.models import LatestReading, Station, StationAccess, StationEvent

from .helpers import T0, make_station

User = get_user_model()


def _user(username, **kwargs):
    user = User.objects.create_user(username, password='x' * 16, **kwargs)
    TOTPDevice.objects.create(user=user, name='phone', confirmed=True)
    return user


@override_settings(DEFAULT_UNIT_SYSTEM='imperial')
class RoleTests(TestCase):
    def setUp(self):
        self.owner = _user('owner')
        self.viewer = _user('viewer')
        self.manager = _user('manager')
        self.stranger = _user('stranger')
        self.staff = _user('admin', is_staff=True)
        self.station = make_station(owner=self.owner, is_public=False,
                                    sensors={'soilmoisture1': {'name': 'Garden', 'public': False}})
        StationAccess.objects.create(station=self.station, user=self.viewer, role=StationAccess.VIEWER)
        StationAccess.objects.create(station=self.station, user=self.manager, role=StationAccess.MANAGER)
        LatestReading.objects.create(station=self.station, timestamp=T0, source='ecowitt_push',
                                     data={'temp_c': 20.0, 'temp_in_c': 22.0, 'humidity_in': 40},
                                     extra={'soilmoisture1': 34.0, 'soilbatt1': 1.1})

    def as_(self, user):
        self.client.logout()
        if user is not None:
            self.client.force_login(user)

    def test_roles(self):
        r = lambda u: access.role(u, Station.objects.get(pk=self.station.pk))   # noqa: E731 (fresh user cache)
        self.assertEqual([r(User.objects.get(pk=u.pk)) for u in (self.viewer, self.manager, self.owner, self.staff, self.stranger)],
                         ['viewer', 'manager', 'owner', 'staff', None])
        self.stranger.is_active = False
        self.assertIsNone(access.role(self.stranger, self.station))

    def test_visible_stations(self):
        public = make_station(name='Public', slug='public', mac_address=None, is_public=True)
        for user, expected in ((self.viewer, {'Test Station', 'Public'}), (self.stranger, {'Public'}),
                               (self.staff, {'Test Station', 'Public'})):
            self.assertEqual({s.name for s in access.visible_stations(user)}, expected, user)
        self.assertEqual(access.visible_stations(self.viewer).count(), 2)       # no duplicates from the join
        self.assertTrue(public.is_public)

    def test_private_station_pages(self):
        dash = reverse('weather:station', args=[self.station.slug])
        self.as_(None)
        self.assertEqual(self.client.get(dash).status_code, 302)                 # sign in
        self.as_(self.stranger)
        self.assertEqual(self.client.get(dash).status_code, 404)
        for user in (self.viewer, self.manager, self.owner, self.staff):
            self.as_(user)
            self.assertEqual(self.client.get(dash).status_code, 200, user)

    def test_dashboard_privacy_levels(self):
        dash = reverse('weather:station', args=[self.station.slug])
        self.as_(self.viewer)
        ctx = self.client.get(dash).context
        self.assertTrue(ctx['show_indoor'])
        self.assertEqual([i['name'] for g in ctx['sensor_groups'] for i in g['items']], ['Garden'])
        self.assertFalse(ctx['can_manage'])
        self.assertIsNone(next(i for g in ctx['sensor_groups'] for i in g['items']).get('battery'))
        self.assertEqual(ctx['low_batteries'], [])
        self.as_(self.manager)
        ctx = self.client.get(dash).context
        self.assertTrue(ctx['can_manage'])
        self.assertIsNotNone(next(i for g in ctx['sensor_groups'] for i in g['items']).get('battery'))
        # Public station, signed-in user without access: a visitor.
        Station.objects.filter(pk=self.station.pk).update(is_public=True)
        self.as_(self.stranger)
        ctx = self.client.get(dash).context
        self.assertFalse(ctx['show_indoor'])
        self.assertEqual(ctx['sensor_groups'], [])

    def test_management_pages(self):
        pages = [reverse(f'weather:station-{p}', args=[self.station.slug])
                 for p in ('settings', 'setup', 'quality', 'log')]
        people = reverse('weather:station-people', args=[self.station.slug])
        for user, manage, administer in ((self.viewer, False, False), (self.manager, True, False),
                                         (self.owner, True, True), (self.staff, True, True)):
            self.as_(user)
            for url in pages:
                self.assertEqual(self.client.get(url).status_code, 200 if manage else 404, (user, url))
            self.assertEqual(self.client.get(people).status_code, 200 if administer else 404, user)
            if manage:
                self.assertEqual('People' in self.client.get(pages[0]).content.decode(), administer, user)

    def test_manager_cannot_change_visibility(self):
        url = reverse('weather:station-settings', args=[self.station.slug])
        data = {'name': 'Renamed', 'timezone': 'America/Denver', 'anemometer_height': '2', 'is_public': 'on'}
        self.as_(self.manager)
        self.assertNotContains(self.client.get(url), 'name="is_public"')
        self.client.post(url, data)
        station = Station.objects.get(pk=self.station.pk)
        self.assertEqual((station.name, station.is_public), ('Renamed', False))
        self.as_(self.owner)
        self.client.post(url, data)
        self.assertTrue(Station.objects.get(pk=self.station.pk).is_public)

    def test_station_list_manage_links(self):
        self.as_(self.manager)
        self.assertContains(self.client.get(reverse('weather:stations')), 'Manage station')
        self.as_(self.viewer)
        self.assertNotContains(self.client.get(reverse('weather:stations')), 'Manage station')


class PeopleTests(TestCase):
    def setUp(self):
        self.owner = _user('owner')
        self.friend = _user('friend', first_name='Pat')
        self.staff = _user('admin', is_staff=True)
        self.station = make_station(owner=self.owner)
        self.url = reverse('weather:station-people', args=[self.station.slug])
        self.client.force_login(self.owner)

    def test_add_change_remove(self):
        self.client.post(self.url, {'action': 'add', 'username': 'FRIEND', 'role': 'viewer'})
        grant = StationAccess.objects.get()
        self.assertEqual((grant.user, grant.role, grant.added_by), (self.friend, 'viewer', self.owner))
        self.client.post(self.url, {'action': 'role', 'pk': grant.pk, 'role': 'manager'})
        self.assertEqual(StationAccess.objects.get().role, 'manager')
        self.client.post(self.url, {'action': 'remove', 'pk': grant.pk})
        self.assertFalse(StationAccess.objects.exists())

    def test_add_refuses_unknown_owner_staff_and_inactive(self):
        _user('gone', is_active=False)
        for username in ('nobody', 'owner', 'admin', 'gone'):
            response = self.client.post(self.url, {'action': 'add', 'username': username, 'role': 'viewer'}, follow=True)
            self.assertEqual(len(response.context['messages']), 1)
        self.assertFalse(StationAccess.objects.exists())

    def test_other_station_grant_cannot_be_edited(self):
        other = make_station(name='Other', slug='other', mac_address=None, owner=self.staff)
        grant = StationAccess.objects.create(station=other, user=self.friend, role='viewer')
        self.assertEqual(self.client.post(self.url, {'action': 'remove', 'pk': grant.pk}).status_code, 404)
        self.assertTrue(StationAccess.objects.filter(pk=grant.pk).exists())

    def test_transfer_keeping_previous_owner(self):
        self.client.post(self.url, {'action': 'transfer', 'username': 'friend', 'keep_previous': '1'})
        station = Station.objects.get()
        self.assertEqual(station.owner, self.friend)
        self.assertEqual(StationAccess.objects.get().user, self.owner)
        self.assertEqual(StationAccess.objects.get().role, 'manager')
        event = StationEvent.objects.get()
        self.assertFalse(event.is_public)
        self.assertIn('Pat (friend)', event.title)
        self.assertIn('stays on as a manager', event.notes)
        # Now a manager: the People tab is gone.
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_transfer_without_keeping_loses_access(self):
        response = self.client.post(self.url, {'action': 'transfer', 'username': 'friend'})
        self.assertRedirects(response, reverse('weather:home'), fetch_redirect_response=False)
        self.assertFalse(StationAccess.objects.exists())
        self.assertEqual(self.client.get(reverse('weather:station', args=[self.station.slug])).status_code, 404)

    def test_transfer_to_a_current_viewer_drops_their_grant(self):
        StationAccess.objects.create(station=self.station, user=self.friend, role='viewer')
        self.client.post(self.url, {'action': 'transfer', 'username': 'friend', 'keep_previous': '1'})
        self.assertEqual(list(StationAccess.objects.values_list('user__username', flat=True)), ['owner'])

    def test_staff_get_a_list_of_users_owners_type_a_name(self):
        self.assertIsNone(self.client.get(self.url).context['candidates'])
        self.client.force_login(self.staff)
        self.assertEqual([u.username for u in self.client.get(self.url).context['candidates']], ['admin', 'friend'])

    def test_deleting_an_owner_is_refused(self):
        with self.assertRaises(ProtectedError):
            self.owner.delete()

    def test_staff_choose_owner_when_adding_a_station(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('weather:station-create'), {
            'name': 'New', 'source': 'other', 'timezone': 'UTC', 'owner': self.friend.pk})
        self.assertEqual(Station.objects.get(name='New').owner, self.friend)


class AdvancedViewerTests(TestCase):
    """Everything a manager sees, read-only, without the upload paths; every change refused."""

    def setUp(self):
        from weather.models import DataExclusion, Neighbour, SensorGateway
        self.owner = _user('owner')
        self.advanced = _user('advanced')
        self.station = make_station(owner=self.owner, is_public=False)
        StationAccess.objects.create(station=self.station, user=self.advanced, role=StationAccess.ADVANCED)
        self.gateway = SensorGateway.objects.create(station=self.station, name='Soil')
        self.event = StationEvent.objects.create(station=self.station, occurred_at=T0, title='Moved', is_public=False)
        self.neighbour = Neighbour.objects.create(station=self.station, wu_id='KXX1')
        self.exclusion = DataExclusion.objects.create(station=self.station, start=T0, end=T0, groups=['temp'], status='applied')
        self.client.force_login(self.advanced)

    def url(self, name, *args):
        return reverse(f'weather:station-{name}', args=[self.station.slug, *args])

    def test_role(self):
        self.assertEqual(access.role(self.advanced, self.station), 'advanced')
        self.assertTrue(access.can_see_management(self.advanced, self.station))
        self.assertFalse(access.can_manage(self.advanced, self.station))

    def test_reads_every_manage_tab_without_forms(self):
        User.objects.filter(pk=self.owner.pk).update(is_staff=True)     # neighbours: administrators' stations only
        for name in ('settings', 'setup', 'quality', 'log', 'neighbours'):
            response = self.client.get(self.url(name))
            self.assertEqual(response.status_code, 200, name)
            page = response.content.decode().split('aria-label="Station"')[-1]       # below the header's forms
            if name != 'settings':                                                  # its form is shown, disabled
                self.assertNotIn('method="post"', page, name)
        self.assertEqual(self.client.get(self.url('people')).status_code, 404)
        self.assertContains(self.client.get(self.url('log')), 'Moved')                 # private entries too
        settings_page = self.client.get(self.url('settings'))
        self.assertContains(settings_page, '<fieldset class="min-w-0 space-y-6" disabled>')
        self.assertNotContains(settings_page, 'Save settings')

    def test_upload_paths_hidden(self):
        html = self.client.get(self.url('setup')).content.decode()
        self.assertNotIn(self.station.push_token, html)
        self.assertNotIn(self.gateway.push_token, html)
        self.assertIn('Only those who manage the station see them', html)
        self.client.force_login(self.owner)
        html = self.client.get(self.url('setup')).content.decode()
        self.assertIn(self.station.push_token, html)
        self.assertIn(self.gateway.push_token, html)

    def test_every_change_refused(self):
        token = self.station.push_token
        posts = [
            (self.url('settings'), {'name': 'Hacked', 'timezone': 'UTC', 'anemometer_height': '2'}),
            (self.url('rotate-token'), {}), (self.url('forget-passkey'), {}),
            (self.url('gateway-add'), {'name': 'X'}), (self.url('gateway-rotate', self.gateway.pk), {}),
            (self.url('gateway-delete', self.gateway.pk), {}),
            (self.url('quality'), {'start': '2026-07-01T00:00', 'end': '2026-07-02T00:00', 'groups': ['temp']}),
            (self.url('quality-remove', self.exclusion.pk), {}),
            (self.url('log'), {'date': '2026-07-01', 'kind': 'other', 'title': 'X'}),
            (self.url('log-edit', self.event.pk), {'date': '2026-07-01', 'kind': 'other', 'title': 'X'}),
            (self.url('log-delete', self.event.pk), {}),
            (self.url('neighbours'), {'wu_id': 'KXX2'}),
            (self.url('neighbour-include', self.neighbour.pk), {}),
            (self.url('neighbour-delete', self.neighbour.pk), {}),
            (self.url('people'), {'action': 'add', 'username': 'owner', 'role': 'viewer'}),
        ]
        for url, data in posts:
            self.assertEqual(self.client.post(url, data).status_code, 404, url)
        station = Station.objects.get(pk=self.station.pk)
        self.assertEqual((station.name, station.push_token), ('Test Station', token))
        self.assertEqual(self.station.gateways.count(), 1)
        self.assertEqual(self.station.events.get().title, 'Moved')
        self.assertTrue(self.station.neighbours.get().include)
        self.assertEqual(self.station.exclusions.get().status, 'applied')

    def test_dashboard_shows_manage_and_batteries(self):
        LatestReading.objects.create(station=self.station, timestamp=T0, source='ecowitt_push',
                                     data={'temp_c': 20.0}, extra={'soilmoisture1': 34.0, 'soilbatt1': 1.1})
        Station.objects.filter(pk=self.station.pk).update(sensors={'soilmoisture1': {'name': 'Garden', 'public': True}})
        response = self.client.get(reverse('weather:station', args=[self.station.slug]))
        self.assertTrue(response.context['can_see_management'])
        self.assertFalse(response.context['can_manage'])
        self.assertContains(response, '>Manage</a>')
        self.assertIsNotNone(next(i for g in response.context['sensor_groups'] for i in g['items'])['battery'])


class OwnStationsTests(TestCase):
    """Users an administrator allows to add stations of their own, up to a limit."""

    def setUp(self):
        self.admin = _user('admin', is_staff=True)
        self.user = _user('pat')
        self.create = reverse('weather:station-create')
        self.client.force_login(self.user)

    def allow(self, limit=1):
        self.user.profile.may_add_stations = True
        self.user.profile.station_limit = limit
        self.user.profile.save()

    def add(self, name='Pat Station', **extra):
        return self.client.post(self.create, {'name': name, 'source': 'ambient', 'timezone': 'America/Denver', **extra})

    def test_not_allowed_by_default(self):
        self.assertEqual(self.client.get(self.create).status_code, 404)
        self.assertNotContains(self.client.get(reverse('weather:stations')), 'Add a station')

    def test_add_own_station_up_to_the_limit(self):
        self.allow(1)
        page = self.client.get(self.create)
        self.assertNotIn('owner', page.context['form'].fields)
        self.assertNotIn('mac_address', page.context['form'].fields)
        self.assertContains(self.client.get(reverse('weather:stations')), 'Add a station')
        self.add(owner=self.admin.pk, mac_address='48:3F:DA:12:34:56')        # both ignored
        station = Station.objects.get(name='Pat Station')
        self.assertEqual((station.owner, station.mac_address, station.ambient_api_enabled), (self.user, None, False))
        self.assertFalse(station.uses_site_services)
        # At the limit now.
        response = self.add('Second')
        self.assertRedirects(response, reverse('weather:stations'), fetch_redirect_response=False)
        self.assertFalse(Station.objects.filter(name='Second').exists())
        self.assertNotContains(self.client.get(reverse('weather:stations')), 'Add a station')

    def test_limit_counts_transferred_stations(self):
        self.allow(2)
        make_station(owner=self.user, name='Given', slug='given', mac_address=None)
        self.add()
        self.assertEqual(self.add('Third').status_code, 302)
        self.assertEqual(Station.objects.filter(owner=self.user).count(), 2)

    def test_staff_set_the_allowance_on_the_users_page(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('accounts:user-detail', args=[self.user.pk]),
                         {'first_name': '', 'last_name': '', 'email': '', 'a-may_add_stations': 'on', 'a-station_limit': '3'})
        self.user.profile.refresh_from_db()
        self.assertEqual((self.user.profile.may_add_stations, self.user.profile.station_limit), (True, 3))
        response = self.client.post(reverse('accounts:user-detail', args=[self.user.pk]),
                                    {'first_name': '', 'last_name': '', 'email': '', 'a-may_add_stations': 'on', 'a-station_limit': '0'})
        self.assertFormError(response.context['allowance'], 'station_limit', 'Between 1 and 100.')

    def test_no_site_api_keys_for_users_stations(self):
        from weather import neighbours
        from weather.models import Neighbour
        station = make_station(owner=self.user, mac_address=None, is_public=True)
        Neighbour.objects.create(station=station, wu_id='KXX1')
        theirs = make_station(owner=self.admin, name='Admin', slug='admin-station', mac_address='02:00:00:00:00:01')
        Neighbour.objects.create(station=theirs, wu_id='KXX2')
        self.assertEqual([n.wu_id for n in neighbours.due()], ['KXX2'])
        self.assertEqual(neighbours.calls_per_day(), neighbours.calls_per_day(1))
        self.assertEqual(self.client.get(reverse('weather:station-neighbours', args=[station.slug])).status_code, 404)
        settings_page = self.client.get(reverse('weather:station-settings', args=[station.slug]))
        self.assertNotIn('mac_address', settings_page.context['form'].fields)
        self.assertNotContains(settings_page, 'station-neighbours')            # no Neighbours tab


class HomeStationTests(TestCase):
    """Visitors and administrators land on the site's default station; owners on their own."""

    def setUp(self):
        from weather.models import SiteSettings
        self.admin = _user('admin', is_staff=True)
        self.user = _user('pat')
        self.main = make_station(owner=self.admin, name='Main', slug='main', is_public=True)
        self.theirs = make_station(owner=self.user, name='Pat', slug='pat', mac_address=None, is_public=True)
        site = SiteSettings.get()
        site.default_station = self.main
        site.save()

    def home_station(self):
        return self.client.get('/').context['station'].slug

    def test_home(self):
        self.assertEqual(self.home_station(), 'main')                 # visitor
        self.client.force_login(self.admin)
        self.assertEqual(self.home_station(), 'main')                 # administrator: the site setting
        self.client.force_login(self.user)
        self.assertEqual(self.home_station(), 'pat')                  # owner: their own
        self.assertRedirects(self.client.get(reverse('weather:charts')), reverse('weather:station-charts', args=['pat']),
                             fetch_redirect_response=False)
        # Someone with access but no station of their own gets the site's default.
        viewer = _user('viewer')
        StationAccess.objects.create(station=self.theirs, user=viewer, role='viewer')
        self.client.force_login(viewer)
        self.assertEqual(self.home_station(), 'main')
