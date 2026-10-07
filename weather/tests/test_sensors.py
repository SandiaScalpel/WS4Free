"""Extra sensors (weather.sensors), piezo rain, metric Ecowitt uploads and WeeWX."""
import datetime as dt
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from weather import charts, dashboard, sensors
from weather.ingest.parsers import parse_ambient_push, parse_ecowitt_push
from weather.ingest.store import record_archive, record_push
from weather.models import DailyRollup, HourlyRollup, LatestReading, Observation, Station
from weather.rollups import refresh_station
from weather.units import UnitPrefs

from .fixtures import ECOWITT_PASSKEY, as_dict
from .helpers import make_station

UTC = dt.UTC
NOW = dt.datetime(2026, 7, 1, 18, 3, 30, tzinfo=UTC)
IMPERIAL, METRIC = UnitPrefs.for_system('imperial'), UnitPrefs.for_system('metric')

# A GW2000 with a WS90 (piezo rain) only — field names as Ecowitt documents them.
WS90_BODY = (
    f'PASSKEY={ECOWITT_PASSKEY}&stationtype=GW2000A_V3.1.4&dateutc=2026-07-01+18:03:17&tempinf=74.1'
    '&humidityin=40&baromrelin=29.90&baromabsin=24.90&tempf=81.3&humidity=22&winddir=200&windspeedmph=5.6'
    '&windgustmph=8.1&maxdailygust=15.0&solarradiation=850.2&uv=8&rrain_piezo=0.120&erain_piezo=0.300'
    '&hrain_piezo=0.050&drain_piezo=0.250&wrain_piezo=0.300&mrain_piezo=1.100&yrain_piezo=6.400'
    '&srain_piezo=1&ws90cap_volt=5.4&ws90_ver=133&wh90batt=3.10&freq=915M&model=GW2000A&interval=16'
)
TIPPING = '&rainratein=0.040&eventrainin=0.200&dailyrainin=0.180&totalrainin=40.000&yearlyrainin=6.000'

# Gateway set to upload in metric.
METRIC_BODY = (
    f'PASSKEY={ECOWITT_PASSKEY}&stationtype=GW1100A&dateutc=2026-07-01+18:03:17&tempc=27.4&humidity=22'
    '&baromrelhpa=1012.5&baromabshpa=843.2&windspeedkmh=9.0&windgustkmh=14.4&winddir=200'
    '&rainratemm=1.2&eventrainmm=5.1&dailyrainmm=4.6&totalrainmm=1016.0&tempinc=23.0&temp1c=19.5'
)

# Exactly what WeeWX's [[Wunderground]] uploader builds (weewx/restx.py AmbientThread.format_url).
WEEWX_QUERY = (
    'action=updateraw&ID=KXXWEEWX1&PASSWORD=secret&softwaretype=weewx-5.1.0'
    '&baromin=29.902&dateutc=2026-07-01%2018%3A00%3A00&dailyrainin=0.12&dewptf=38.4&rainin=0.02'
    '&humidity=022&tempf=81.3&radiation=850&solarradiation=850.00&soilmoisture=031&soiltempf=72.5'
    '&leafwetness=007&AqPM2.5=12.4&UV=8.00&winddir=200&windgustmph=8.1&windspeedmph=5.6'
)


class ParserTests(SimpleTestCase):
    def test_piezo_only_station_records_its_rain(self):
        r = parse_ecowitt_push(as_dict(WS90_BODY), NOW)
        self.assertAlmostEqual(r.values['rain_daily_mm'], 0.25 * 25.4)
        self.assertAlmostEqual(r.values['rain_event_mm'], 0.30 * 25.4)
        self.assertAlmostEqual(r.values['rain_counter_mm'], 6.4 * 25.4)      # yearly counter stands in
        self.assertAlmostEqual(r.values['rain_rate_mmh'], 0.12 * 25.4)
        self.assertFalse([k for k in r.extra if 'rain' in k])

    def test_both_gauges_automatic_prefers_tipping_bucket(self):
        r = parse_ecowitt_push(as_dict(WS90_BODY + TIPPING), NOW)
        self.assertAlmostEqual(r.values['rain_daily_mm'], 0.18 * 25.4)
        self.assertAlmostEqual(r.values['rain_counter_mm'], 40.0 * 25.4)    # lifetime counter beats yearly
        self.assertAlmostEqual(r.values['rain_rate_mmh'], 0.04 * 25.4)
        self.assertFalse([k for k in r.extra if 'rain' in k])               # the piezo's aren't kept either

    def test_both_gauges_station_chooses_piezo(self):
        r = parse_ecowitt_push(as_dict(WS90_BODY + TIPPING), NOW, rain_gauge='piezo')
        self.assertAlmostEqual(r.values['rain_daily_mm'], 0.25 * 25.4)
        self.assertAlmostEqual(r.values['rain_rate_mmh'], 0.12 * 25.4)

    def test_tipping_station_choosing_piezo_without_one_keeps_tipping(self):
        r = parse_ecowitt_push(as_dict('PASSKEY=x&tempf=70' + TIPPING), NOW, rain_gauge='piezo')
        self.assertAlmostEqual(r.values['rain_daily_mm'], 0.18 * 25.4)

    def test_metric_ecowitt_upload(self):
        r = parse_ecowitt_push(as_dict(METRIC_BODY), NOW)
        v = r.values
        self.assertEqual((v['temp_c'], v['pressure_rel_hpa'], v['temp_in_c']), (27.4, 1012.5, 23.0))
        self.assertAlmostEqual(v['wind_speed_ms'], 2.5)
        self.assertAlmostEqual(v['wind_gust_ms'], 4.0)
        self.assertEqual((v['rain_daily_mm'], v['rain_event_mm'], v['rain_counter_mm'], v['rain_rate_mmh']),
                         (4.6, 5.1, 1016.0, 1.2))
        self.assertEqual(r.extra['temp1c'], 19.5)

    def test_weewx_wunderground_upload(self):
        r = parse_ambient_push(as_dict(WEEWX_QUERY), dt.datetime(2026, 7, 1, 18, 0, 20, tzinfo=UTC))
        self.assertEqual(r.timestamp, dt.datetime(2026, 7, 1, 18, 0, tzinfo=UTC))
        self.assertFalse(r.clock_adjusted)
        self.assertEqual(r.passkey, 'KXXWEEWX1')
        self.assertAlmostEqual(r.values['temp_c'], (81.3 - 32) * 5 / 9)
        self.assertAlmostEqual(r.values['rain_daily_mm'], 0.12 * 25.4)
        self.assertNotIn('rain_rate_mmh', r.values)        # WU 'rainin' is last-hour rain, not a rate
        self.assertEqual({k: r.extra[k] for k in ('soilmoisture', 'soiltempf', 'leafwetness', 'AqPM2.5')},
                         {'soilmoisture': 31, 'soiltempf': 72.5, 'leafwetness': 7, 'AqPM2.5': 12.4})
        self.assertNotIn('PASSWORD', r.extra)


class CatalogTests(SimpleTestCase):
    def test_describe_across_brands(self):
        cases = {
            'temp3f': ('temperature', 3, 'Temperature 3'), 'temp2c': ('temperature', 2, 'Temperature 2'),
            'soilhum2': ('soil_moisture', 2, 'Soil moisture 2'), 'soilmoisture4': ('soil_moisture', 4, 'Soil moisture 4'),
            'soilmoisture': ('soil_moisture', 1, 'Soil moisture 1'), 'tf_ch1': ('soil_temp', 1, 'Probe temperature 1'),
            'pm25_ch2': ('pm25', 2, 'PM2.5 2'), 'AqPM2.5': ('pm25', 1, 'Outdoor PM2.5'),
            'lightning_day': ('lightning', 1, 'Lightning strikes'), 'lightning_num': ('lightning', 1, 'Lightning strikes'),
            'leak_ch3': ('leak', 3, 'Leak sensor 3'),
        }
        for key, (kind, channel, label) in cases.items():
            with self.subTest(key=key):
                s = sensors.describe(key)
                self.assertEqual((s.kind, s.channel, s.default_label), (kind, channel, label))
        for key in ('tempf', 'humidity', 'humidityin', 'battout', 'maxdailygust', 'pm25_24h', 'lightning_time'):
            with self.subTest(key=key):
                self.assertIsNone(sensors.describe(key))

    def test_conversion_and_privacy_defaults(self):
        self.assertAlmostEqual(sensors.describe('temp1f').si('212'), 100.0)
        self.assertIsNone(sensors.describe('temp1f').si('n/a'))
        self.assertTrue(sensors.describe('temp1f').private_default)        # often a room indoors
        self.assertTrue(sensors.describe('pm25_in').private_default)
        self.assertFalse(sensors.describe('soilhum1').private_default)
        self.assertFalse(sensors.describe('pm25').private_default)

    def test_low_batteries_follow_each_brands_convention(self):
        self.assertEqual(sensors.low_batteries({'battout': 0, 'batt1': 0, 'batt2': 1, 'batt_lightning': 1}, 'api'),
                         ['Lightning detector', 'Outdoor sensor', 'Sensor 1'])
        self.assertEqual(sensors.low_batteries({'wh65batt': 1, 'batt1': 1, 'batt2': 0, 'soilbatt1': 1.1,
                                                'soilbatt2': 1.5, 'pm25batt1': 1, 'wh90batt': 3.1}, 'ecowitt_push'),
                         ['Outdoor sensor', 'PM2.5 sensor 1', 'Sensor 1', 'Soil moisture sensor 1'])

    def test_lightning_last_strike(self):
        tz = dt.timezone.utc
        km, when = sensors.lightning_last({'lightning_distance': 5, 'lightning_time': 1782928800000}, tz)
        self.assertAlmostEqual(km, 8.04672)
        self.assertEqual(when, dt.datetime(2026, 7, 1, 18, 0, tzinfo=UTC))
        km, when = sensors.lightning_last({'lightning': 12, 'lightning_time': '1782928800'}, tz)
        self.assertEqual((km, when), (12.0, dt.datetime(2026, 7, 1, 18, 0, tzinfo=UTC)))
        self.assertEqual(sensors.distance_text(8.04672, IMPERIAL), '5 mi')
        self.assertEqual(sensors.distance_text(12, METRIC), '12 km')

    def test_pm25_categories(self):
        self.assertEqual(sensors.pm25_category(9.0)['label'], 'Good')
        self.assertEqual(sensors.pm25_category(9.1)['label'], 'Moderate')
        self.assertEqual(sensors.pm25_category(60)['label'], 'Unhealthy')


def ambient_push(station, minute, **extra):
    """An Ambient-protocol push at 18:00 UTC + `minute` with the given extra sensors."""
    from weather.ingest.parsers import Reading
    reading = Reading(dt.datetime(2026, 7, 1, 18, 0, tzinfo=UTC) + dt.timedelta(minutes=minute),
                      {'temp_c': 25.0}, extra)
    return record_push(station, reading, Observation.SOURCE_AMBIENT_PUSH)


class DetectionAndRollupTests(TestCase):
    def setUp(self):
        self.station = make_station()

    def test_new_sensors_are_registered_and_history_resummarised(self):
        Observation.objects.create(station=self.station, timestamp=dt.datetime(2024, 1, 1, tzinfo=UTC), source='api')
        Station.objects.update(rollup_dirty_from=None)
        ambient_push(self.station, 5, temp1f=68.0, soilhum1=30, battout=1, pm25_in=4.0)
        st = Station.objects.get()
        self.assertEqual(st.sensors, {'temp1f': {'name': '', 'public': False},
                                      'soilhum1': {'name': '', 'public': True},
                                      'pm25_in': {'name': '', 'public': False}})
        self.assertEqual(st.rollup_dirty_from, dt.datetime(2024, 1, 1, tzinfo=UTC))
        # Seen already: no more marking dirty from the start of history.
        Station.objects.update(rollup_dirty_from=None)
        ambient_push(self.station, 10, temp1f=69.0, soilhum1=31)
        self.assertEqual(Station.objects.get().rollup_dirty_from, dt.datetime(2026, 7, 1, 18, 10, tzinfo=UTC))

    def test_archive_records_register_sensors_too(self):
        from weather.ingest.parsers import Reading
        record_archive(self.station, [Reading(dt.datetime(2026, 7, 1, 18, 5, tzinfo=UTC), {'temp_c': 20.0},
                                              {'soiltemp1f': 60.0})], Observation.SOURCE_API)
        self.assertIn('soiltemp1f', Station.objects.get().sensors)

    def test_rollups_summarise_sensors_weighted_by_interval(self):
        ambient_push(self.station, 5, temp1f=50.0, lightning_day=2)       # 18:05
        ambient_push(self.station, 10, temp1f=68.0, lightning_day=5)      # 18:10
        Observation.objects.filter(timestamp=dt.datetime(2026, 7, 1, 18, 10, tzinfo=UTC)).update(interval_s=900)
        refresh_station(self.station, now=dt.datetime(2026, 7, 1, 19, tzinfo=UTC))
        hour = HourlyRollup.objects.get(period_start=dt.datetime(2026, 7, 1, 18, tzinfo=UTC))
        mean, lo, hi = hour.extra['temp1f']
        self.assertAlmostEqual(mean, (10 * 300 + 20 * 900) / 1200, places=2)      # °C, weighted 1:3
        self.assertEqual((lo, hi), (10.0, 20.0))
        self.assertEqual(hour.extra['lightning_day'][2], 5)
        day = DailyRollup.objects.get(date=dt.date(2026, 7, 1))
        self.assertEqual(day.extra['temp1f'][1:], [10.0, 20.0])

    def test_station_without_sensors_has_empty_summaries(self):
        ambient_push(self.station, 5, battout=1)
        refresh_station(self.station, now=dt.datetime(2026, 7, 1, 19, tzinfo=UTC))
        self.assertEqual(HourlyRollup.objects.get().extra, {})


class ChartTests(TestCase):
    def setUp(self):
        self.station = make_station(sensors={'temp1f': {'name': 'Greenhouse', 'public': True},
                                             'temp2f': {'name': 'Bedroom', 'public': False},
                                             'lightning_day': {'name': '', 'public': True}})
        tz = self.station.tzinfo
        # Local 23:50, 23:55, 00:00 (end of the day), 00:05 next day.
        base = dt.datetime(2026, 7, 1, 23, 50, tzinfo=tz)
        for i, (t1, strikes) in enumerate(((60.0, 3), (62.0, 7), (64.0, 7), (66.0, 9))):
            Observation.objects.create(station=self.station, timestamp=base + dt.timedelta(minutes=5 * i), source='api',
                                       temp_c=20.0, extra={'temp1f': t1, 'temp2f': 70.0, 'lightning_day': strikes})
        self.start, self.end = base - dt.timedelta(hours=1), base + dt.timedelta(hours=1)

    def test_raw_history_columns_privacy_and_strikes(self):
        public = charts.history(self.station, self.start, self.end, IMPERIAL)
        self.assertNotIn('x:temp2f', public['series'])
        self.assertEqual([c['title'] for c in public['sensor_charts']], ['Temperature: Greenhouse (°F)', 'Lightning strikes'])
        self.assertEqual(public['sensor_charts'][0]['lines'], [{'col': 'x:temp1f', 'name': 'Greenhouse'}])
        self.assertEqual(public['series']['x:temp1f'], [60.0, 62.0, 64.0, 66.0])
        # 3, +4, +0, then a new local day starts from its own count (9, not 9 − 7).
        self.assertEqual(public['series']['x:lightning_day'], [3, 4, 0, 9])
        owner = charts.history(self.station, self.start, self.end, METRIC, include_private=True)
        self.assertEqual(owner['series']['x:temp2f'], [21.1] * 4)

    def test_rollup_history_and_csv(self):
        Station.objects.update(rollup_dirty_from=self.start)
        refresh_station(self.station, now=self.end)
        long = charts.history(self.station, self.start - dt.timedelta(days=10), self.end, IMPERIAL)
        self.assertEqual(long['resolution'], 'hourly')
        values = [v for v in long['series']['x:temp1f'] if v is not None]
        # Hourly means. Rows are stamped at the END of their interval, so 00:00 belongs to the 23:00 hour.
        self.assertEqual(values, [62.0, 66.0])
        strikes = [v for v in long['series']['x:lightning_day'] if v is not None]
        self.assertEqual(strikes, [7, 9])                         # new local day: its own count
        csv_text = charts.history_csv(self.station, self.start, self.end, IMPERIAL)
        self.assertIn('Greenhouse (°F)', csv_text.splitlines()[0])
        self.assertNotIn('Bedroom', csv_text)

    def test_chart_endpoint_shows_private_sensors_to_owner_only(self):
        Station.objects.update(is_public=True)
        url = reverse('weather:station-chart-data', args=[self.station.slug])
        query = {'kind': 'history', 'start': '2026-07-01', 'end': '2026-07-02'}
        self.assertNotIn('x:temp2f', self.client.get(url, query).json()['series'])
        TOTPDevice.objects.create(user=self.station.owner, name='phone', confirmed=True)
        self.client.force_login(self.station.owner)
        self.assertIn('x:temp2f', self.client.get(url, query).json()['series'])


class DashboardTests(TestCase):
    def setUp(self):
        self.station = make_station(sensors={
            'temp1f': {'name': 'Greenhouse', 'public': True}, 'pm25': {'name': '', 'public': True},
            'leak1': {'name': 'Basement', 'public': False}, 'lightning_day': {'name': '', 'public': True}})
        LatestReading.objects.create(station=self.station, timestamp=NOW, source='api', data={'temp_c': 25.0}, extra={
            'temp1f': 77.0, 'pm25': 40.0, 'pm25_24h': 8.0, 'leak1': 1, 'lightning_day': 4,
            'lightning_distance': 5, 'lightning_time': int((NOW - dt.timedelta(minutes=20)).timestamp() * 1000),
            'battout': 0})

    def test_groups_values_and_privacy(self):
        public = dashboard.build(self.station, IMPERIAL, now=NOW)
        labels = [g['label'] for g in public['sensor_groups']]
        self.assertEqual(labels, ['Temperature', 'PM2.5', 'Lightning'])
        self.assertEqual(public['low_batteries'], [])
        items = {i['name']: i for g in public['sensor_groups'] for i in g['items']}
        self.assertEqual(items['Greenhouse']['value'], '77.0°F')
        self.assertEqual(items['Outdoor PM2.5']['detail'], 'Good · 24-h average')      # category on the 24-h mean
        self.assertEqual(items['Lightning strikes']['value'], '4')
        self.assertEqual(items['Lightning strikes']['detail'], 'strikes today · last 11:43 AM, 5 mi away')

        owner = dashboard.build(self.station, IMPERIAL, viewer=self.station.owner, now=NOW)
        leak = [i for g in owner['sensor_groups'] for i in g['items'] if i['name'] == 'Basement'][0]
        self.assertEqual((leak['value'], leak['tone'], leak['private']), ('Leak!', 'bad', True))
        self.assertEqual(owner['low_batteries'], ['Outdoor sensor'])

    def test_last_strike_on_an_earlier_day_says_so(self):
        LatestReading.objects.filter(station=self.station).update(extra={
            'lightning_day': 0, 'lightning_time': int((NOW - dt.timedelta(hours=20)).timestamp() * 1000)})
        items = [i for g in dashboard.build(self.station, IMPERIAL, now=NOW)['sensor_groups'] for i in g['items']]
        self.assertEqual(items[0]['detail'], 'strikes today · last yesterday 4:03 PM')

    def test_live_partial_renders(self):
        Station.objects.update(is_public=True)
        response = self.client.get(reverse('weather:station-live', args=[self.station.slug]))
        self.assertContains(response, 'More sensors')
        self.assertContains(response, 'Greenhouse')
        self.assertNotContains(response, 'Basement')


class SettingsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('owner', password='x' * 16)
        TOTPDevice.objects.create(user=self.owner, name='phone', confirmed=True)
        self.station = make_station(owner=self.owner, sensors={'soilhum1': {'name': '', 'public': True},
                                                               'temp1f': {'name': '', 'public': False}})
        self.client.force_login(self.owner)
        self.url = reverse('weather:station-settings', args=[self.station.slug])

    def test_rename_and_publish_sensors_and_choose_gauge(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'soilhum1')
        self.assertContains(page, 'placeholder="Temperature 1"')
        data = {'name': 'Test Station', 'timezone': 'America/Denver', 'anemometer_height': '6.6',
                'rain_gauge': 'piezo', 'sensor_0_name': ' Greenhouse ', 'sensor_0_public': 'on',
                'sensor_1_name': 'Orchard', 'sensor_1_public': ''}
        self.assertRedirects(self.client.post(self.url, data), self.url)
        st = Station.objects.get()
        self.assertEqual(st.rain_gauge, 'piezo')
        self.assertEqual(st.sensors, {'soilhum1': {'name': 'Orchard', 'public': False},
                                      'temp1f': {'name': 'Greenhouse', 'public': True}})


class IngestViewTests(TestCase):
    def test_upload_uses_the_stations_rain_gauge(self):
        station = make_station(rain_gauge='piezo')
        with mock.patch('weather.ingest.views.timezone.now', return_value=NOW):
            response = self.client.post(f'/ingest/ecowitt/{station.push_token}/', WS90_BODY + TIPPING,
                                        content_type='application/x-www-form-urlencoded')
        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(Observation.objects.get().rain_daily_mm, 0.25 * 25.4)


class EverywhereTests(TestCase):
    """Extra sensors in reports and almanac records (TODO F5)."""

    def setUp(self):
        self.owner = get_user_model().objects.create_user('owner5', password='x' * 16)
        TOTPDevice.objects.create(user=self.owner, name='phone', confirmed=True)
        self.station = make_station(owner=self.owner, is_public=True, sensors={
            'temp1f': {'name': 'Greenhouse', 'public': True}, 'temp2f': {'name': 'Cellar', 'public': False},
            'pm25': {'name': '', 'public': True}, 'lightning_day': {'name': '', 'public': True}})
        tz = self.station.tzinfo
        readings = {  # local day → [(hour, temp1f °F, pm25, strikes so far)]
            dt.date(2025, 7, 1): [(6, 50.0, 5.0, 0), (15, 104.0, 8.0, 3)],
            dt.date(2025, 7, 2): [(6, 41.0, 30.0, 2), (15, 95.0, 12.0, 9)],
        }
        for day, rows in readings.items():
            for hour, t1, pm, strikes in rows:
                Observation.objects.create(station=self.station, timestamp=dt.datetime.combine(day, dt.time(hour), tzinfo=tz),
                                           source='api', temp_c=20.0, extra={'temp1f': t1, 'temp2f': 55.0, 'pm25': pm,
                                                                             'lightning_day': strikes})
        Station.objects.filter(pk=self.station.pk).update(rollup_dirty_from=dt.datetime(2025, 7, 1, tzinfo=UTC))
        refresh_station(self.station, now=dt.datetime(2025, 7, 3, tzinfo=UTC))

    def test_report_columns(self):
        from weather import reports
        keys = [c.key for c in reports.sensor_columns(self.station)]
        self.assertEqual(keys, ['x:temp1f:high', 'x:temp1f:low', 'x:temp1f:mean', 'x:pm25:mean', 'x:pm25:max',
                                'x:lightning_day:strikes'])
        self.assertIn('x:temp2f:high', [c.key for c in reports.sensor_columns(self.station, include_private=True)])
        report = reports.build(self.station, dt.date(2025, 7, 1), dt.date(2025, 7, 2), IMPERIAL, 'day',
                               ['x:temp1f:high', 'x:temp1f:low', 'x:pm25:max', 'x:lightning_day:strikes', 'x:temp2f:high'])
        self.assertEqual([c.key for c in report.columns], ['x:temp1f:high', 'x:temp1f:low', 'x:pm25:max', 'x:lightning_day:strikes'])
        self.assertEqual([[round(v, 1) for v in row.values] for row in report.rows], [[104.0, 50.0, 8.0, 3], [95.0, 41.0, 30.0, 9]])
        self.assertEqual([round(v, 1) for v in report.summary], [104.0, 41.0, 30.0, 12])     # max, min, max, total strikes
        page = self.client.get(reverse('weather:station-reports', args=[self.station.slug]),
                               {'col': ['x:temp1f:high'], 'period': 'custom', 'start': '2025-07-01', 'end': '2025-07-02'})
        self.assertContains(page, 'Greenhouse high')
        self.assertNotContains(page, 'Cellar')

    def test_almanac_records(self):
        from weather import almanac
        recs = {r.key: r for r in almanac.sensor_records(self.station)}
        self.assertEqual(set(recs), {'x:temp1f:high', 'x:temp1f:low', 'x:pm25:high', 'x:lightning_day:day'})
        high, low = recs['x:temp1f:high'], recs['x:temp1f:low']
        self.assertEqual((round(high.value * 9 / 5 + 32, 1), high.date, high.time.astimezone(self.station.tzinfo).hour),
                         (104.0, dt.date(2025, 7, 1), 15))
        self.assertEqual((round(low.value * 9 / 5 + 32, 1), low.date), (41.0, dt.date(2025, 7, 2)))
        self.assertEqual((recs['x:lightning_day:day'].value, recs['x:lightning_day:day'].date), (9, dt.date(2025, 7, 2)))
        self.assertIn('x:temp2f:high', {r.key for r in almanac.sensor_records(self.station, include_private=True)})
        self.assertEqual(almanac.sensor_records(self.station, year=2024), [])
        page = self.client.get(reverse('weather:station-almanac', args=[self.station.slug]))
        self.assertContains(page, 'Greenhouse: highest')
        self.assertContains(page, '9 strikes')
        self.assertNotContains(page, 'Cellar')
        self.client.force_login(self.owner)
        page = self.client.get(reverse('weather:station-almanac', args=[self.station.slug]))
        self.assertContains(page, 'group=x:temp1f')                                     # "exclude these readings" link


class LowBatteryPeriodTests(TestCase):
    """Low-battery periods on the charts (TODO F6)."""

    def test_merging(self):
        step = 300_000
        day = 86_400_000
        buckets = [(0, []), (step, ['Outdoor sensor']), (2 * step, ['Outdoor sensor']), (4 * step, ['Rain gauge']),
                   (day, ['Outdoor sensor']),                                  # back within a day: same period
                   (3 * day, ['Outdoor sensor'])]                              # two days later: a new one
        self.assertEqual(charts.battery_periods(buckets, step), [
            {'start': step, 'end': day + step, 'names': ['Outdoor sensor', 'Rain gauge'], 'intermittent': True},
            {'start': 3 * day, 'end': 3 * day + step, 'names': ['Outdoor sensor'], 'intermittent': False},
        ])
        self.assertFalse(charts.battery_periods([(0, ['A']), (step, ['A'])], step)[0]['intermittent'])

    def test_rollups_and_history(self):
        station = make_station(is_public=True)
        tz = station.tzinfo
        rows = [(dt.datetime(2025, 7, 1, 10, 5, tzinfo=tz), 'api', {'battout': 1}),
                (dt.datetime(2025, 7, 1, 10, 10, tzinfo=tz), 'api', {'battout': 0}),
                (dt.datetime(2025, 7, 1, 10, 15, tzinfo=tz), 'ecowitt_push', {'wh65batt': 1, 'batt1': 0}),
                (dt.datetime(2025, 7, 1, 10, 20, tzinfo=tz), 'api', {'battout': 1})]
        for ts, src, extra in rows:
            Observation.objects.create(station=station, timestamp=ts, source=src, temp_c=20.0, extra=extra)
        Station.objects.filter(pk=station.pk).update(rollup_dirty_from=dt.datetime(2025, 7, 1, tzinfo=UTC))
        refresh_station(station, now=dt.datetime(2025, 7, 2, tzinfo=UTC))
        self.assertEqual(DailyRollup.objects.get().low_batteries, ['Outdoor sensor'])
        self.assertEqual(HourlyRollup.objects.get(period_start=dt.datetime(2025, 7, 1, 10, tzinfo=tz)).low_batteries,
                         ['Outdoor sensor'])
        start = dt.datetime(2025, 7, 1, 9, tzinfo=tz)
        self.assertEqual(charts.history(station, start, start + dt.timedelta(hours=3), IMPERIAL)['battery'], [])   # visitors
        viewer = charts.history(station, start, start + dt.timedelta(hours=3), IMPERIAL, include_private=True)
        self.assertEqual(viewer['battery'], [])                       # viewers see private sensors, not batteries
        raw = charts.history(station, start, start + dt.timedelta(hours=3), IMPERIAL, include_private=True, batteries=True)
        ms = lambda h, m: int(dt.datetime(2025, 7, 1, h, m, tzinfo=tz).timestamp() * 1000)   # noqa: E731
        self.assertEqual(raw['battery'], [{'start': ms(10, 5), 'end': ms(10, 15), 'names': ['Outdoor sensor'], 'intermittent': False}])
        daily = charts.history(station, start - dt.timedelta(days=200), start + dt.timedelta(days=1), IMPERIAL,
                               include_private=True, batteries=True)
        self.assertEqual([b['names'] for b in daily['battery']], [['Outdoor sensor']])


class LowBatteryVisibilityTests(TestCase):
    def test_bands_only_for_the_owner(self):
        owner = get_user_model().objects.create_user('owner6', password='x' * 16)
        TOTPDevice.objects.create(user=owner, name='phone', confirmed=True)
        station = make_station(owner=owner, is_public=True)
        Observation.objects.create(station=station, timestamp=dt.datetime(2025, 7, 1, 18, tzinfo=UTC), source='api',
                                   temp_c=20.0, extra={'battout': 0})
        url = reverse('weather:station-chart-data', args=[station.slug])
        query = {'kind': 'history', 'start': '2025-07-01', 'end': '2025-07-01'}
        self.assertEqual(self.client.get(url, query).json()['battery'], [])
        self.client.force_login(owner)
        self.assertEqual(self.client.get(url, query).json()['battery'][0]['names'], ['Outdoor sensor'])
