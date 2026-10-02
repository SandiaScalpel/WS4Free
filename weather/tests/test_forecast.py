"""Daily forecast from Open-Meteo (weather.forecast)."""
import datetime as dt
from decimal import Decimal
from unittest import mock

import requests
from django.test import TestCase, override_settings
from django.urls import reverse

from weather import dashboard, forecast
from weather.models import LatestReading, StationForecast
from weather.units import UnitPrefs

from .helpers import make_station

UTC = dt.UTC
NOW = dt.datetime(2026, 10, 2, 18, 0, tzinfo=UTC)          # noon in Denver
IMPERIAL = UnitPrefs.for_system('imperial')

DAILY = {'daily': {
    'time': ['2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04'],
    'weather_code': [0, 2, 95, 61],
    'temperature_2m_max': [24.0, 25.0, 20.0, 15.0],
    'temperature_2m_min': [9.0, 10.0, 12.0, 8.0],
    'precipitation_probability_max': [0, 5, 70, 90],
    'precipitation_sum': [0.0, 0.0, 6.35, 0.1],
}}


def session(payload=DAILY, error=None):
    s = mock.Mock()
    if error:
        s.get.side_effect = error
    else:
        s.get.return_value = mock.Mock(json=mock.Mock(return_value=payload), raise_for_status=mock.Mock())
    return s


@override_settings(FORECAST_URL='https://forecast.test/v1/forecast')
class ForecastTests(TestCase):
    def setUp(self):
        self.station = make_station(latitude=Decimal('35.123456'), longitude=Decimal('-106.654321'), is_public=True)

    def test_fetch_rounds_location_and_asks_for_local_days(self):
        s = session()
        days = forecast.fetch(self.station, s)
        self.assertEqual(s.get.call_args.args[0], 'https://forecast.test/v1/forecast')
        params = s.get.call_args.kwargs['params']
        self.assertEqual((params['latitude'], params['longitude'], params['timezone']), (35.12, -106.65, 'America/Denver'))
        self.assertEqual(days[2], {'date': '2026-10-03', 'code': 95, 'high_c': 20.0, 'low_c': 12.0, 'rain_pct': 70, 'rain_mm': 6.35})

    def test_current_starts_today_and_refreshes_hourly(self):
        s = session()
        days = forecast.current(self.station, now=NOW, session=s)
        self.assertEqual([d['date'] for d in days], ['2026-10-02', '2026-10-03', '2026-10-04'])
        forecast.current(self.station, now=NOW + dt.timedelta(minutes=59), session=s)
        self.assertEqual(s.get.call_count, 1)                       # still fresh
        forecast.current(self.station, now=NOW + dt.timedelta(minutes=61), session=s)
        self.assertEqual(s.get.call_count, 2)

    def test_failure_keeps_last_forecast_and_backs_off(self):
        forecast.current(self.station, now=NOW, session=session())
        failing = session(error=requests.ConnectionError('down'))
        later = NOW + dt.timedelta(hours=2)
        days = forecast.current(self.station, now=later, session=failing)
        self.assertEqual(len(days), 3)                              # previous forecast still shown
        self.assertIn('down', StationForecast.objects.get().error)
        forecast.current(self.station, now=later + dt.timedelta(minutes=5), session=failing)
        self.assertEqual(failing.get.call_count, 1)                 # no retry within 10 minutes
        forecast.current(self.station, now=later + dt.timedelta(minutes=11), session=failing)
        self.assertEqual(failing.get.call_count, 2)
        # Too old to trust: show nothing rather than yesterday's forecast.
        self.assertEqual(forecast.current(self.station, now=NOW + dt.timedelta(hours=13), session=failing), [])

    def test_site_wide_off_when_no_url(self):
        s = session()
        with override_settings(FORECAST_URL=''):
            self.assertEqual(forecast.current(self.station, now=NOW, session=s), [])
        s.get.assert_not_called()

    def test_off_switch_and_missing_location(self):
        s = session()
        self.station.forecast_enabled = False
        self.assertEqual(forecast.current(self.station, now=NOW, session=s), [])
        self.station.forecast_enabled, self.station.latitude = True, None
        self.assertEqual(forecast.current(self.station, now=NOW, session=s), [])
        s.get.assert_not_called()

    def test_display_in_viewer_units(self):
        days = forecast.display([{'date': '2026-10-02', 'code': 95, 'high_c': 20.0, 'low_c': 12.0, 'rain_pct': 70, 'rain_mm': 6.35},
                                 {'date': '2026-10-03', 'code': 61, 'high_c': 15.0, 'low_c': 8.0, 'rain_pct': 90, 'rain_mm': 0.1}],
                                IMPERIAL, dt.date(2026, 10, 2))
        self.assertEqual((days[0]['day'], days[0]['label'], days[0]['icon']), ('Today', 'Thunderstorms', 'thunder'))
        self.assertEqual(days[1]['day'], 'Sat')
        self.assertEqual(days[0]['rain_mm'], 6.35)
        self.assertIsNone(days[1]['rain_mm'])                       # 0.004 in: a trace, not shown

    def test_every_wmo_code_has_an_icon_the_template_draws(self):
        template = open(__file__.replace('tests/test_forecast.py', 'templates/weather/partials/forecast_icon.html')).read()
        for code, (label, icon) in forecast.CODES.items():
            with self.subTest(code=code):
                self.assertTrue(label)
                self.assertTrue(icon == 'cloud' or f"'{icon}'" in template, icon)

    @mock.patch('weather.forecast.requests')
    def test_dashboard_strip(self, req):
        req.get.return_value = mock.Mock(json=mock.Mock(return_value=DAILY), raise_for_status=mock.Mock())
        req.RequestException = requests.RequestException
        LatestReading.objects.create(station=self.station, timestamp=NOW, source='api', data={'temp_c': 20.0})
        ctx = dashboard.build(self.station, IMPERIAL, now=NOW)
        self.assertEqual([d['day'] for d in ctx['forecast']], ['Today', 'Sat', 'Sun'])
        html = self.client.get(reverse('weather:station-live', args=[self.station.slug])).content.decode()
        self.assertIn('Weather data by Open-Meteo.com', html)
        self.assertIn('aria-label="Thunderstorms"', html)
        self.assertIn('70%', html)
