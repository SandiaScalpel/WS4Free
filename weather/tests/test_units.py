from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from weather import units as u
from weather.templatetags import weather_units as f


class DerivedValueTests(SimpleTestCase):
    def test_heat_index_matches_nws_table(self):
        # NWS heat index chart: 90 °F at 60 % RH → 100 °F; 100 °F at 40 % → 109 °F.
        self.assertAlmostEqual(u.c_to_f(u.feels_like_c(u.f_to_c(90), 60, 0)), 100, delta=1)
        self.assertAlmostEqual(u.c_to_f(u.feels_like_c(u.f_to_c(100), 40, 0)), 109, delta=1)

    def test_wind_chill_matches_nws_table(self):
        # NWS wind chill chart: 30 °F with 15 mph wind → 19 °F; 0 °F with 20 mph → −22 °F.
        self.assertAlmostEqual(u.c_to_f(u.feels_like_c(u.f_to_c(30), 50, u.mph_to_ms(15))), 19, delta=0.5)
        self.assertAlmostEqual(u.c_to_f(u.feels_like_c(u.f_to_c(0), 50, u.mph_to_ms(20))), -22, delta=0.5)

    def test_mild_weather_feels_like_air_temperature(self):
        self.assertEqual(u.feels_like_c(18.0, 50, 5.0), 18.0)
        self.assertEqual(u.feels_like_c(5.0, 50, 0.5), 5.0)   # cold but calm: no wind chill

    def test_compass_points(self):
        self.assertEqual([u.compass(d) for d in (0, 11, 12, 284, 340, 348.8, 359)], ['N', 'N', 'NNE', 'WNW', 'NNW', 'N', 'N'])

    def test_uv_categories(self):
        self.assertEqual([u.uv_category(x)['label'] for x in (0, 2.9, 3, 6, 8, 11)],
                         ['Low', 'Low', 'Moderate', 'High', 'Very high', 'Extreme'])

    def test_pressure_trend(self):
        self.assertEqual(u.pressure_trend(0.9)['label'], 'Steady')
        self.assertEqual(u.pressure_trend(-1.2)['label'], 'Falling')
        self.assertEqual(u.pressure_trend(4.0)['label'], 'Rising fast')


class FormattingTests(SimpleTestCase):
    imperial = u.UnitPrefs.for_system('imperial')
    metric = u.UnitPrefs.for_system('metric')

    def test_formats_in_each_system(self):
        self.assertEqual(f.temp(20.0, self.imperial), '68.0°F')
        self.assertEqual(f.temp(20.0, self.metric), '20.0°C')
        self.assertEqual(f.speed(10.0, self.imperial), '22.4 mph')
        self.assertEqual(f.speed(10.0, self.metric), '36 km/h')
        self.assertEqual(f.pressure(1013.25, self.imperial), '29.92 inHg')
        self.assertEqual(f.pressure(1013.25, self.metric), '1,013.2 hPa')
        self.assertEqual(f.rain(25.4, self.imperial), '1.00 in')
        self.assertEqual(f.rain(25.4, self.metric), '25.4 mm')

    def test_missing_values_render_as_dash(self):
        for flt in (f.temp, f.speed, f.pressure, f.rain, f.pct, f.whole):
            self.assertEqual(flt(None, self.imperial), '—')

    def test_system_detection(self):
        self.assertEqual(self.imperial.system, 'imperial')
        self.assertEqual(u.UnitPrefs(temp='F', wind='kn', pressure='hpa', rain='in').system, 'custom')


@override_settings(DEFAULT_UNIT_SYSTEM='imperial')
class PrefsResolutionTests(TestCase):
    def setUp(self):
        self.rf = RequestFactory()

    def request(self, user=None, cookie=None):
        from django.contrib.auth.models import AnonymousUser
        req = self.rf.get('/')
        req.user = user or AnonymousUser()
        if cookie:
            req.COOKIES[u.UNITS_COOKIE] = cookie
        return req

    def test_visitor_default_and_cookie(self):
        self.assertEqual(u.prefs_for_request(self.request()).system, 'imperial')
        self.assertEqual(u.prefs_for_request(self.request(cookie='metric')).system, 'metric')
        self.assertEqual(u.prefs_for_request(self.request(cookie='bogus')).system, 'imperial')

    def test_signed_in_user_can_mix_units(self):
        user = get_user_model().objects.create_user('alice', password='x' * 16)
        user.profile.unit_pressure = 'hpa'
        user.profile.save()
        user.refresh_from_db()
        prefs = u.prefs_for_request(self.request(user))
        self.assertEqual((prefs.temp, prefs.pressure), ('F', 'hpa'))
