"""Sensor gateways: a second device that adds only extra sensors to a station."""
import datetime as dt
import hashlib
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from weather import dashboard
from weather.ingest.parsers import Reading
from weather.ingest.store import record_archive
from weather.models import DailyRollup, LatestReading, Observation, SensorGateway, Station
from weather.rollups import refresh_station
from weather.units import UnitPrefs

from .fixtures import AMBIENT_QUERY, PUSH_NOW
from .helpers import make_station

NOW = mock.patch('weather.ingest.views.timezone.now', return_value=PUSH_NOW)
GATEWAY_MAC = '48:3F:DA:AA:BB:CC'
GATEWAY_PASSKEY = hashlib.md5(GATEWAY_MAC.encode()).hexdigest().upper()
BUCKET = dt.datetime(2026, 9, 30, 12, 5, tzinfo=dt.UTC)

# What a GW1100 sends: its own indoor readings, an outdoor array it hears, and a soil probe.
GATEWAY_BODY = (
    f'PASSKEY={GATEWAY_PASSKEY}&stationtype=GW1100B_V2.4.4&runtime=99&heap=20000&dateutc=2026-09-30+12:03:20'
    '&tempinf=71.1&humidityin=49&baromrelin=25.26&baromabsin=25.26&tempf=77.9&humidity=29&winddir=204'
    '&windspeedmph=2.01&windgustmph=4.47&maxdailygust=58.16&solarradiation=645.07&uv=5&rainratein=0.000'
    '&eventrainin=0.000&hourlyrainin=0.000&dailyrainin=0.000&weeklyrainin=0.000&monthlyrainin=0.000'
    '&yearlyrainin=11.130&totalrainin=11.130&soilmoisture1=34&soilad1=180&soilbatt1=1.5&batt1=0'
    '&wh65batt=0&freq=915M&model=GW1100B&interval=60'
)


class GatewayIngestTests(TestCase):
    def setUp(self):
        NOW.start()
        self.addCleanup(NOW.stop)
        self.station = make_station()                      # an Ambient console station
        self.gateway = SensorGateway.objects.create(station=self.station, name='Soil gateway')
        self.gw_url = f'/ingest/ecowitt/{self.gateway.push_token}/'
        self.console_url = f'/ingest/ambient/{self.station.push_token}/'

    def post_gateway(self, body=GATEWAY_BODY):
        return self.client.post(self.gw_url, body, content_type='application/x-www-form-urlencoded')

    def test_only_extra_sensors_are_kept(self):
        self.assertEqual(self.post_gateway().status_code, 200)
        row = Observation.objects.get()
        self.assertEqual(row.timestamp, BUCKET)
        self.assertIsNone(row.temp_c)                      # its outdoor/indoor/pressure readings are dropped
        self.assertIsNone(row.pressure_rel_hpa)
        self.assertIsNone(row.rain_counter_mm)
        self.assertEqual(row.sample_count, 0)              # no main readings: not coverage
        self.assertEqual(set(row.extra), {'soilmoisture1', 'soilbatt1', 'batt1'})
        self.assertEqual(row.extra['batt1'], 1)            # Ecowitt 0 = OK → Ambient 1 = OK
        gateway = SensorGateway.objects.get()
        self.assertEqual(gateway.push_passkey, GATEWAY_PASSKEY)
        self.assertEqual(gateway.latest_extra['soilmoisture1'], 34)
        self.assertIn('soilmoisture1', Station.objects.get().sensors)
        self.assertEqual(Station.objects.get().push_passkey, '')     # the console's PASSKEY is untouched

    def test_console_push_fills_the_gateway_row(self):
        self.post_gateway()
        self.client.get(f'{self.console_url}?{AMBIENT_QUERY}')
        row = Observation.objects.get()
        self.assertAlmostEqual(row.temp_c, (58.8 - 32) * 5 / 9)
        self.assertEqual(row.extra['soilmoisture1'], 34)
        self.assertEqual(row.sample_count, 1)
        self.assertEqual(row.source, Observation.SOURCE_AMBIENT_PUSH)

    def test_gateway_never_overwrites_the_console(self):
        self.client.get(f'{self.console_url}?{AMBIENT_QUERY}')
        self.post_gateway()
        row = Observation.objects.get()
        self.assertAlmostEqual(row.temp_c, (58.8 - 32) * 5 / 9)       # not the gateway's 77.9 °F
        self.assertAlmostEqual(row.humidity, 80)
        self.assertEqual(row.extra['soilmoisture1'], 34)
        latest = LatestReading.objects.get()
        self.assertAlmostEqual(latest.data['temp_c'], (58.8 - 32) * 5 / 9)

    def test_another_device_on_the_gateway_path_is_rejected(self):
        self.post_gateway()
        other = GATEWAY_BODY.replace(GATEWAY_PASSKEY, 'F' * 32)
        self.assertEqual(self.post_gateway(other).status_code, 403)

    def test_upload_without_extra_sensors_is_noted_not_stored(self):
        body = GATEWAY_BODY.replace('&soilmoisture1=34&soilad1=180&soilbatt1=1.5&batt1=0', '')
        self.assertEqual(self.post_gateway(body).status_code, 200)
        self.assertFalse(Observation.objects.exists())
        self.assertIsNotNone(SensorGateway.objects.get().last_upload_at)

    def test_api_gap_fill_completes_a_gateway_only_row(self):
        self.post_gateway()
        api = Reading(BUCKET - dt.timedelta(minutes=1), {'temp_c': 15.0, 'humidity': 60.0}, extra={'battout': '1'})
        self.assertEqual(record_archive(self.station, [api], Observation.SOURCE_API), 1)
        row = Observation.objects.get()
        self.assertEqual((row.temp_c, row.sample_count, row.source), (15.0, 1, Observation.SOURCE_API))
        self.assertEqual(row.extra, {'battout': '1', 'soilmoisture1': 34, 'soilbatt1': 1.5, 'batt1': 1})

    def test_gateway_rows_dont_count_as_coverage(self):
        self.post_gateway()
        refresh_station(self.station, now=PUSH_NOW + dt.timedelta(hours=1))
        day = DailyRollup.objects.get(station=self.station)
        self.assertEqual(day.coverage, 0)
        self.assertEqual(day.sample_count, 0)
        self.assertEqual(day.extra['soilmoisture1'][0], 34.0)        # but the soil moisture is summarised

    def test_dashboard_shows_the_gateways_sensors(self):
        self.client.get(f'{self.console_url}?{AMBIENT_QUERY}')
        self.post_gateway(GATEWAY_BODY.replace('soilbatt1=1.5', 'soilbatt1=1.1'))   # a low soil-probe battery
        Station.objects.filter(pk=self.station.pk).update(sensors={'soilmoisture1': {'name': 'Vineyard', 'public': True}})
        self.station.refresh_from_db()
        owner = self.station.owner
        ctx = dashboard.build(self.station, UnitPrefs.for_system('imperial'), viewer=owner, now=PUSH_NOW)
        items = [i for g in ctx['sensor_groups'] for i in g['items']]
        self.assertEqual([(i['name'], i['value']) for i in items], [('Vineyard', '34%')])
        self.assertEqual(items[0]['battery'], {'volts': 1.1, 'low': True})
        self.assertEqual(ctx['low_batteries'], ['Soil moisture sensor 1'])
        visitor = dashboard.build(self.station, UnitPrefs.for_system('imperial'), viewer=None, now=PUSH_NOW)
        self.assertIsNone(visitor['sensor_groups'][0]['items'][0]['battery'])   # the voltage is for the owner only
        # A gateway that has gone quiet stops showing.
        later = dashboard.build(self.station, UnitPrefs.for_system('imperial'), viewer=owner, now=PUSH_NOW + dt.timedelta(hours=1))
        self.assertEqual(later['sensor_groups'], [])

    def test_battery_voltage_on_the_dashboard(self):
        self.client.get(f'{self.console_url}?{AMBIENT_QUERY}')
        self.post_gateway()                                 # soilbatt1=1.5
        Station.objects.filter(pk=self.station.pk).update(sensors={'soilmoisture1': {'name': '', 'public': True}})
        TOTPDevice.objects.create(user=self.station.owner, name='phone', confirmed=True)
        self.client.force_login(self.station.owner)
        with mock.patch('weather.dashboard.timezone.now', return_value=PUSH_NOW):
            html = self.client.get(reverse('weather:station-live', args=[self.station.slug])).content.decode()
        self.assertIn('text-good" title="Only you see this. Below 1.2 V the battery needs replacing">Battery 1.50 V</p>', html)

    def test_unknown_token_is_still_404(self):
        self.assertEqual(self.client.post('/ingest/ecowitt/nope/', GATEWAY_BODY,
                                          content_type='application/x-www-form-urlencoded').status_code, 404)


class GatewaySetupTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('owner', password='x' * 16)
        TOTPDevice.objects.create(user=self.owner, name='phone', confirmed=True)
        self.station = make_station(owner=self.owner, slug='mesa')
        self.client.force_login(self.owner)

    def test_add_rotate_remove(self):
        self.client.post(reverse('weather:station-gateway-add', args=['mesa']), {'name': 'Soil gateway'})
        gateway = SensorGateway.objects.get()
        html = self.client.get(reverse('weather:station-setup', args=['mesa'])).content.decode()
        self.assertIn(f'/ingest/ecowitt/{gateway.push_token}/', html)
        self.assertIn('Soil gateway', html)
        old = gateway.push_token
        self.client.post(reverse('weather:station-gateway-rotate', args=['mesa', gateway.pk]))
        self.assertNotEqual(SensorGateway.objects.get().push_token, old)
        self.client.post(reverse('weather:station-gateway-delete', args=['mesa', gateway.pk]))
        self.assertFalse(SensorGateway.objects.exists())

    def test_other_users_cant(self):
        other = get_user_model().objects.create_user('other', password='x' * 16)
        TOTPDevice.objects.create(user=other, name='phone', confirmed=True)
        self.client.force_login(other)
        response = self.client.post(reverse('weather:station-gateway-add', args=['mesa']), {'name': 'x'})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(SensorGateway.objects.exists())
