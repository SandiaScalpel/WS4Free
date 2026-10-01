import datetime as dt
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts.models import UserProfile
from weather import dashboard
from weather.models import DailyRollup, LatestReading, Observation, SiteSettings, Station
from weather.units import UNITS_COOKIE, UnitPrefs

from .helpers import make_station

DENVER = ZoneInfo('America/Denver')
NOW = dt.datetime(2026, 10, 15, 18, 0, tzinfo=dt.UTC)   # noon in Denver


def latest(station, **data):
    defaults = {'temp_c': 20.0, 'humidity': 30.0, 'rain_daily_mm': 2.0, 'pressure_rel_hpa': 1010.0,
                'temp_in_c': 22.0, 'humidity_in': 40.0}
    return LatestReading.objects.create(station=station, timestamp=data.pop('timestamp', NOW),
                                        source='ambient_push', data={**defaults, **data})


class BuildTests(TestCase):
    def setUp(self):
        self.station = make_station()
        latest(self.station)

    def build(self, **kw):
        return dashboard.build(self.station, UnitPrefs.for_system('imperial'), now=NOW, **kw)

    def test_today_high_low_include_the_live_reading(self):
        DailyRollup.objects.create(station=self.station, date=dt.date(2026, 10, 15), temp_max_c=18.0, temp_min_c=5.0)
        ctx = self.build()
        self.assertEqual((ctx['today']['high_c'], ctx['today']['low_c']), (20.0, 5.0))

    def test_month_and_year_rain_use_console_counter_for_today(self):
        for day, mm in ((dt.date(2026, 9, 30), 10.0), (dt.date(2026, 10, 1), 3.0), (dt.date(2026, 10, 14), 1.5),
                        (dt.date(2026, 10, 15), 99.0)):   # today's rollup is ignored in favour of the live counter
            DailyRollup.objects.create(station=self.station, date=day, rain_mm=mm)
        ctx = self.build()
        self.assertEqual(ctx['rain_month_mm'], 3.0 + 1.5 + 2.0)
        self.assertEqual(ctx['rain_year_mm'], 10.0 + 3.0 + 1.5 + 2.0)

    def test_pressure_trend_over_three_hours(self):
        Observation.objects.create(station=self.station, timestamp=NOW - dt.timedelta(hours=3), source='api',
                                   pressure_rel_hpa=1006.0)
        ctx = self.build()
        self.assertEqual(ctx['pressure_change_hpa'], 4.0)
        self.assertEqual(ctx['pressure_trend']['label'], 'Rising fast')

    def test_stale_after_ten_minutes(self):
        self.assertFalse(self.build()['stale'])
        LatestReading.objects.filter(station=self.station).update(timestamp=NOW - dt.timedelta(minutes=11))
        self.assertTrue(self.build()['stale'])

    def test_chart_series_converted_to_viewer_units(self):
        Observation.objects.create(station=self.station, timestamp=NOW - dt.timedelta(hours=1), source='api', temp_c=0.0)
        ctx = self.build()
        self.assertEqual(ctx['chart_data']['series']['temp'][0][1], 32.0)
        self.assertEqual(ctx['chart_data']['units']['labels']['temp'], '°F')


class AccessTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('owner', password='x' * 16)
        self.other = User.objects.create_user('other', password='x' * 16)
        for user in (self.owner, self.other):
            TOTPDevice.objects.create(user=user, name='phone', confirmed=True)
        self.station = make_station(owner=self.owner)
        latest(self.station, timestamp=timezone.now())
        self.url = reverse('weather:station', args=[self.station.slug])
        self.live = reverse('weather:station-live', args=[self.station.slug])

    def test_private_station(self):
        self.assertRedirects(self.client.get(self.url), f"{reverse('two_factor:login')}?next={self.url}",
                             fetch_redirect_response=False)
        self.assertEqual(self.client.get(self.live).status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.get(self.live).status_code, 404)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(self.url), 'Test Station')

    def test_public_station_on_home_page_for_visitors(self):
        Station.objects.update(is_public=True)
        response = self.client.get('/')
        self.assertContains(response, 'Test Station')
        self.assertContains(response, 'data-chart="temp"')
        self.assertContains(self.client.get(self.live), 'Temperature')

    def test_indoor_readings_only_for_owner(self):
        Station.objects.update(is_public=True)
        for client_user, visible in ((None, False), (self.other, False), (self.owner, True)):
            self.client.logout()
            if client_user:
                self.client.force_login(client_user)
            with self.subTest(user=client_user):
                for url in (self.url, self.live):
                    response = self.client.get(url)
                    (self.assertContains if visible else self.assertNotContains)(response, 'Indoors')
                    (self.assertContains if visible else self.assertNotContains)(response, '71.6°F')   # 22 °C indoors

    def test_live_poll_does_not_extend_the_session(self):
        Station.objects.update(is_public=True)
        self.client.force_login(self.owner)
        self.client.get(self.url)
        stamp = self.client.session['last_activity']
        self.client.get(self.live, HTTP_X_POLL='1')
        self.assertEqual(self.client.session['last_activity'], stamp)


class UnitsSwitchTests(TestCase):
    def test_visitor_gets_cookie(self):
        response = self.client.post(reverse('weather:set-units'), {'system': 'metric', 'next': '/stations/x/'})
        self.assertRedirects(response, '/stations/x/', fetch_redirect_response=False)
        self.assertEqual(response.cookies[UNITS_COOKIE].value, 'metric')

    def test_signed_in_user_profile_updated(self):
        user = get_user_model().objects.create_user('alice', password='x' * 16)
        TOTPDevice.objects.create(user=user, name='phone', confirmed=True)
        self.client.force_login(user)
        self.client.post(reverse('weather:set-units'), {'system': 'metric'})
        p = UserProfile.objects.get(user=user)
        self.assertEqual((p.unit_temp, p.unit_wind, p.unit_pressure, p.unit_rain), ('C', 'kmh', 'hpa', 'mm'))

    def test_next_cannot_point_offsite(self):
        response = self.client.post(reverse('weather:set-units'), {'system': 'metric', 'next': 'https://evil.example/'})
        self.assertRedirects(response, '/', fetch_redirect_response=False)


class SiteSettingsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user('admin', password='x' * 16, is_staff=True)
        self.user = User.objects.create_user('alice', password='x' * 16)
        for user in (self.staff, self.user):
            TOTPDevice.objects.create(user=user, name='phone', confirmed=True)
        self.url = reverse('weather:site-settings')

    def test_staff_only(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_title_shown_across_the_site(self):
        self.client.force_login(self.staff)
        self.client.post(self.url, {'site_title': 'Mesa Ridge Weather', 'tagline': '', 'about': 'Hello.'})
        self.assertEqual(SiteSettings.get().site_title, 'Mesa Ridge Weather')
        self.client.logout()
        response = self.client.get('/')
        self.assertContains(response, 'Mesa Ridge Weather')
        self.assertContains(response, 'powered by WS4Free')


class StationListTests(TestCase):
    """The home page lists the stations when there's more than one and no default."""

    def test_each_card_opens_its_station(self):
        from django.contrib.auth import get_user_model
        owner = get_user_model().objects.create_user('lister', password='x' * 16)
        a = make_station(owner=owner, name='House', mac_address='02:00:00:00:00:01', is_public=True)
        b = make_station(owner=owner, name='Vines', mac_address='02:00:00:00:00:02', is_public=True)
        response = self.client.get('/')
        for st in (a, b):
            self.assertContains(response, f'href="/stations/{st.slug}/"')

    def test_header_has_no_charts_link_and_help_beside_units(self):
        make_station(is_public=True)
        html = self.client.get('/').content.decode()
        header = html[html.index('<header'):html.index('</header>')]
        self.assertNotIn('href="/charts/"', header)
        self.assertLess(header.index('href="/help/"'), header.index('action="/units/"'))


class HeaderTests(TestCase):
    def test_stations_link_only_when_there_is_a_choice(self):
        make_station(is_public=True)
        header = lambda: (lambda h: h[h.index('<header'):h.index('</header>')])(self.client.get('/help/').content.decode())
        self.assertNotIn('href="/stations/"', header())
        make_station(name='Second', mac_address='02:00:00:00:00:03', is_public=True)
        self.assertIn('href="/stations/"', header())
        self.assertNotIn('>Dashboard<', header())

    def test_private_stations_count_only_for_their_owner(self):
        st = make_station(is_public=True)
        make_station(owner=st.owner, name='Hidden', mac_address='02:00:00:00:00:04')
        self.assertNotIn('href="/stations/"', self.client.get('/help/').content.decode())
        TOTPDevice.objects.create(user=st.owner, name='phone', confirmed=True)
        self.client.force_login(st.owner)
        self.assertIn('href="/stations/"', self.client.get('/help/').content.decode())

    def test_station_list_page(self):
        a = make_station(is_public=True)
        make_station(owner=a.owner, name='Hidden', mac_address='02:00:00:00:00:04')
        SiteSettings.get(); SiteSettings.objects.update(default_station=a)
        response = self.client.get(reverse('weather:stations'))
        self.assertContains(response, '<h1 class="text-3xl font-bold">Stations</h1>', html=False)
        self.assertContains(response, f'href="/stations/{a.slug}/"')
        self.assertNotContains(response, 'Hidden')

    def test_tagline_beside_title_not_on_station(self):
        make_station(is_public=True)
        SiteSettings.get(); SiteSettings.objects.update(tagline='Backyard weather since 2021')
        html = self.client.get('/').content.decode()
        header = html[html.index('<header'):html.index('</header>')]
        self.assertIn('Backyard weather since 2021', header)
        self.assertEqual(html.count('Backyard weather since 2021'), 1)


class TemperatureHumidityCardTests(TestCase):
    def test_series_include_humidity_and_dew_point(self):
        station = make_station(is_public=True)
        now = timezone.now()
        Observation.objects.create(station=station, timestamp=now - dt.timedelta(minutes=5), source='api',
                                   temp_c=20.0, humidity=55.0, dewpoint_c=10.8)
        series = dashboard.build(station, UnitPrefs.for_system('metric'), now=now)['chart_data']['series']
        self.assertEqual([p[1] for p in series['humidity']], [55])
        self.assertEqual([p[1] for p in series['dewpoint']], [10.8])
        response = self.client.get(reverse('weather:station-live', args=[station.slug]))
        self.assertContains(response, 'Temperature &amp; Humidity')
