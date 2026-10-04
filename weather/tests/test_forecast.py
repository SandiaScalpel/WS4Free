"""Daily and hourly forecast from Open-Meteo (weather.forecast)."""
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



def _hours(dates):
    """Hourly columns for whole days: 10 °C at midnight rising 1 °C an hour, a
    40 % chance of rain at 15:00, night before 07:00 and from 19:00."""
    times = [f'{d}T{h:02d}:00' for d in dates for h in range(24)]
    hours = [int(t[11:13]) for t in times]
    return {
        'time': times,
        'weather_code': [0 if h != 15 else 61 for h in hours],
        'temperature_2m': [10.0 + h for h in hours],
        'apparent_temperature': [9.0 + h for h in hours],
        'relative_humidity_2m': [50] * len(times),
        'precipitation_probability': [40 if h == 15 else 0 for h in hours],
        'precipitation': [0.5 if h == 15 else 0.0 for h in hours],
        'wind_speed_10m': [2.0] * len(times),
        'wind_direction_10m': [270] * len(times),
        'wind_gusts_10m': [4.5] * len(times),
        'is_day': [1 if 7 <= h < 19 else 0 for h in hours],
    }


DATES = ['2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04']
DAILY = {'daily': {
    'time': DATES,
    'weather_code': [0, 2, 95, 61],
    'temperature_2m_max': [24.0, 25.0, 20.0, 15.0],
    'temperature_2m_min': [9.0, 10.0, 12.0, 8.0],
    'precipitation_probability_max': [0, 5, 70, 90],
    'precipitation_sum': [0.0, 0.0, 6.35, 0.1],
    'wind_speed_10m_max': [3.0, 4.0, 9.0, 5.0],
    'wind_gusts_10m_max': [6.0, 7.0, 15.0, 8.0],
    'wind_direction_10m_dominant': [180, 200, 270, 300],
    'uv_index_max': [6.5, 6.4, 3.0, 2.0],
    'sunrise': [f'{d}T07:0{i}' for i, d in enumerate(DATES)],
    'sunset': [f'{d}T18:4{i}' for i, d in enumerate(DATES)],
}, 'hourly': _hours(DATES)}


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
        self.assertEqual((params['wind_speed_unit'], params['forecast_days']), ('ms', 16))
        self.assertIn('temperature_2m', params['hourly'])
        days, hours = days
        self.assertEqual(days[2], {'date': '2026-10-03', 'code': 95, 'high_c': 20.0, 'low_c': 12.0, 'rain_pct': 70, 'rain_mm': 6.35,
                                   'wind_ms': 9.0, 'gust_ms': 15.0, 'wind_dir': 270, 'uv': 3.0,
                                   'sunrise': '07:02', 'sunset': '18:42'})
        self.assertEqual(len(hours), 96)
        self.assertEqual(hours[39], {'time': '2026-10-02T15:00', 'code': 61, 'temp_c': 25.0, 'feels_c': 24.0, 'humidity': 50,
                                     'rain_pct': 40, 'rain_mm': 0.5, 'wind_ms': 2.0, 'gust_ms': 4.5, 'wind_dir': 270, 'is_day': 1})

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
        for icon in forecast.NIGHT_ICONS.values():
            self.assertIn(f"'{icon}'", template)
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
        with mock.patch('weather.dashboard.timezone.now', return_value=NOW):     # the fixture's days are around NOW
            html = self.client.get(reverse('weather:station-live', args=[self.station.slug])).content.decode()
        self.assertIn('Weather data by Open-Meteo.com', html)
        self.assertIn('aria-label="Thunderstorms"', html)
        self.assertIn('70%', html)

    def test_forecast_from_before_hourly_data_refreshes_at_once(self):
        StationForecast.objects.create(station=self.station, fetched_at=NOW, days=[
            {'date': '2026-10-02', 'code': 0, 'high_c': 20.0, 'low_c': 5.0, 'rain_pct': 0, 'rain_mm': 0.0}])
        s = session()
        forecast.current(self.station, now=NOW + dt.timedelta(minutes=5), session=s)
        self.assertEqual(s.get.call_count, 1)
        self.assertEqual(len(StationForecast.objects.get().hours), 96)
        # An old-style day (no wind, UV or sun times) still displays.
        old = forecast.display([{'date': '2026-10-02', 'code': 0, 'high_c': 20.0, 'low_c': 5.0, 'rain_pct': 0, 'rain_mm': 0.0}],
                               IMPERIAL, dt.date(2026, 10, 2))
        self.assertIsNone(old[0]['sunrise'])

    def test_day_returns_that_dates_hours_only(self):
        found, hours = forecast.day(self.station, '2026-10-03', now=NOW, session=session())
        self.assertEqual(found['code'], 95)
        self.assertEqual((len(hours), hours[0]['time'], hours[-1]['time']), (24, '2026-10-03T00:00', '2026-10-03T23:00'))
        self.assertEqual(forecast.day(self.station, '2026-10-01', now=NOW, session=session()), (None, []))   # yesterday
        self.assertEqual(forecast.day(self.station, '2026-10-09', now=NOW, session=session()), (None, []))   # beyond it

    def test_display_hours(self):
        _, hours = forecast.day(self.station, '2026-10-02', now=NOW, session=session())
        rows = forecast.display_hours(hours, IMPERIAL, dt.datetime(2026, 10, 2, 12, 30))
        self.assertEqual((rows[0]['icon'], rows[9]['icon']), ('moon', 'sun'))          # clear at night → moon
        self.assertEqual((rows[15]['label'], rows[15]['rain_mm']), ('Light rain', 0.5))
        self.assertTrue(rows[11]['past'])                                            # 11:00–12:00 has ended
        self.assertFalse(rows[12]['past'])                                           # 12:00–13:00 hasn't
        chart = forecast.hours_chart(rows, IMPERIAL)
        self.assertEqual((chart['hours'][15], chart['temp'][0], chart['rain_pct'][15]), ('15:00', 50.0, 40))

    def test_temperature_bars_span_the_whole_forecast(self):
        days = forecast.temperature_bars([{'low_c': 0.0, 'high_c': 10.0}, {'low_c': 10.0, 'high_c': 20.0}, {'low_c': None, 'high_c': None}])
        self.assertEqual((days[0]['bar_left'], days[0]['bar_width']), (0.0, 50.0))
        self.assertEqual((days[1]['bar_left'], days[1]['bar_width']), (50.0, 50.0))
        self.assertNotIn('bar_left', days[2])


@override_settings(FORECAST_URL='https://forecast.test/v1/forecast')
class ForecastPageTests(TestCase):
    def setUp(self):
        self.station = make_station(latitude=Decimal('35.1'), longitude=Decimal('-106.6'), is_public=True, slug='mesa')
        patcher = mock.patch('weather.forecast.requests')
        req = patcher.start()
        self.addCleanup(patcher.stop)
        req.get.return_value = mock.Mock(json=mock.Mock(return_value=DAILY), raise_for_status=mock.Mock())
        req.RequestException = requests.RequestException
        clock = mock.patch('django.utils.timezone.now', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def test_page_lists_every_day_and_links_each_to_its_hours(self):
        html = self.client.get(reverse('weather:station-forecast', args=['mesa'])).content.decode()
        self.assertIn('3-day forecast', html)
        for date in ('2026-10-02', '2026-10-03', '2026-10-04'):
            self.assertIn(reverse('weather:station-forecast-day', args=['mesa', date]), html)
        self.assertNotIn(reverse('weather:station-forecast-day', args=['mesa', '2026-10-01']), html)
        self.assertIn('id="forecast-day"', html)                                  # the dialog

    def test_dashboard_card_links_to_page_and_days(self):
        html = self.client.get(reverse('weather:station', args=['mesa'])).content.decode()
        self.assertIn(reverse('weather:station-forecast', args=['mesa']), html)
        self.assertIn(reverse('weather:station-forecast-day', args=['mesa', '2026-10-03']), html)
        self.assertIn('id="forecast-day-body"', html)

    def test_day_popup(self):
        response = self.client.get(reverse('weather:station-forecast-day', args=['mesa', '2026-10-03']))
        html = response.content.decode()
        self.assertIn('Saturday, October 3', html)
        self.assertEqual(html.count('<th scope="row"'), 24)
        self.assertIn('forecast-hours-data', html)
        self.assertIn(reverse('weather:station-forecast-day', args=['mesa', '2026-10-02']), html)   # previous
        self.assertIn(reverse('weather:station-forecast-day', args=['mesa', '2026-10-04']), html)   # next
        self.assertIn('77°', html)                        # 25 °C at 15:00, in °F

    def test_day_popup_unknown_or_bad_date_is_404(self):
        for date in ('2026-10-01', '2026-10-20', 'nonsense'):
            with self.subTest(date=date):
                self.assertEqual(self.client.get(reverse('weather:station-forecast-day', args=['mesa', date])).status_code, 404)

    def test_private_station(self):
        self.station.is_public = False
        self.station.save()
        page = self.client.get(reverse('weather:station-forecast', args=['mesa']))
        self.assertEqual(page.status_code, 302)                                    # visitor → sign in
        day = self.client.get(reverse('weather:station-forecast-day', args=['mesa', '2026-10-03']))
        self.assertEqual(day.status_code, 404)

    def test_page_without_a_forecast(self):
        self.station.forecast_enabled = False
        self.station.save()
        html = self.client.get(reverse('weather:station-forecast', args=['mesa'])).content.decode()
        self.assertIn('No forecast is available right now.', html)
