import datetime as dt

from django.test import SimpleTestCase

from weather import units
from weather.ingest.parsers import ParseError, parse_ambient_push, parse_api_record, parse_dateutc, parse_ecowitt_push

from .fixtures import AMBIENT_QUERY, API_RECORD, ECOWITT_BODY, ECOWITT_PASSKEY, MAC, PUSH_NOW, WU_QUERY, as_dict


class UnitTests(SimpleTestCase):
    def test_conversions(self):
        self.assertAlmostEqual(units.f_to_c(212), 100)
        self.assertAlmostEqual(units.inhg_to_hpa(29.92), 1013.21, places=2)
        self.assertAlmostEqual(units.mph_to_ms(10), 4.4704)
        self.assertAlmostEqual(units.in_to_mm(1), 25.4)

    def test_dewpoint_matches_reference(self):
        # 20 °C at 50 % RH → 9.3 °C (standard psychrometric tables).
        self.assertAlmostEqual(units.dewpoint_c(20, 50), 9.3, delta=0.1)


class AmbientApiRecordTests(SimpleTestCase):
    def setUp(self):
        self.r = parse_api_record(API_RECORD)

    def test_timestamp_is_epoch_ms_utc(self):
        self.assertEqual(self.r.timestamp, dt.datetime(2026, 10, 1, 4, 40, tzinfo=dt.UTC))

    def test_core_values_in_si(self):
        v = self.r.values
        self.assertAlmostEqual(v['temp_c'], 14.889, places=3)
        self.assertAlmostEqual(v['pressure_rel_hpa'], 1001.32, places=2)
        self.assertAlmostEqual(v['wind_gust_ms'], 2.0117, places=4)
        self.assertAlmostEqual(v['rain_counter_mm'], 1208.30, places=2)
        self.assertAlmostEqual(v['dewpoint_c'], 11.467, places=3)   # console's own, not computed
        self.assertEqual(v['rain_rate_mmh'], 0)                    # hourlyrainin is a RATE here

    def test_extras_keep_sensors_drop_noise(self):
        self.assertEqual(self.r.extra, {'battout': 1, 'maxdailygust': 18.3})


class AmbientPushTests(SimpleTestCase):
    def test_parses_console_upload(self):
        r = parse_ambient_push(as_dict(AMBIENT_QUERY), PUSH_NOW)
        self.assertEqual(r.timestamp, dt.datetime(2026, 9, 30, 12, 3, 17, tzinfo=dt.UTC))
        self.assertEqual(r.passkey, MAC)
        self.assertAlmostEqual(r.values['rain_rate_mmh'], 3.048)
        self.assertAlmostEqual(r.values['humidity_in'], 43)
        self.assertNotIn('stationtype', r.extra)

    def test_computes_dewpoint_when_absent(self):
        r = parse_ambient_push(as_dict(AMBIENT_QUERY), PUSH_NOW)
        self.assertAlmostEqual(r.values['dewpoint_c'], units.dewpoint_c(r.values['temp_c'], 80))

    def test_wunderground_synonyms(self):
        r = parse_ambient_push(as_dict(WU_QUERY), PUSH_NOW)
        self.assertEqual(r.timestamp, PUSH_NOW)                         # dateutc=now
        self.assertAlmostEqual(r.values['pressure_rel_hpa'], 1001.32, places=2)
        self.assertAlmostEqual(r.values['temp_in_c'], units.f_to_c(73.2))
        self.assertAlmostEqual(r.values['dewpoint_c'], units.f_to_c(52.6))
        self.assertNotIn('rain_rate_mmh', r.values)                     # WU 'rainin' is last-hour total, not a rate
        self.assertEqual(r.passkey, 'KCOTEST99')
        self.assertNotIn('PASSWORD', r.extra)

    def test_rejects_payload_without_weather(self):
        with self.assertRaises(ParseError):
            parse_ambient_push({'PASSKEY': MAC, 'stationtype': 'x'}, PUSH_NOW)

    def test_out_of_range_values_dropped(self):
        r = parse_ambient_push({'tempf': '-9999', 'humidity': '140', 'winddir': '360', 'windspeedmph': 'nan'}, PUSH_NOW)
        self.assertNotIn('temp_c', r.values)
        self.assertNotIn('humidity', r.values)
        self.assertNotIn('wind_speed_ms', r.values)
        self.assertEqual(r.values['wind_dir_deg'], 0.0)   # 360° is north


class EcowittPushTests(SimpleTestCase):
    def setUp(self):
        self.r = parse_ecowitt_push(as_dict(ECOWITT_BODY), PUSH_NOW)

    def test_rate_comes_from_rainratein_not_hourlyrainin(self):
        self.assertAlmostEqual(self.r.values['rain_rate_mmh'], 3.048)

    def test_extra_sensors_kept(self):
        self.assertEqual(self.r.extra['temp1f'], 65.3)
        self.assertEqual(self.r.extra['soilmoisture1'], 34)
        self.assertEqual(self.r.extra['wh65batt'], 0)
        self.assertNotIn('hourlyrainin', self.r.extra)
        self.assertNotIn('model', self.r.extra)

    def test_passkey(self):
        self.assertEqual(self.r.passkey, ECOWITT_PASSKEY)


class DateutcTests(SimpleTestCase):
    def test_formats(self):
        expected = dt.datetime(2026, 9, 30, 12, 0, 0, tzinfo=dt.UTC)
        now = expected + dt.timedelta(seconds=30)
        for raw in ('2026-09-30 12:00:00', '2026-09-30+12:00:00', '2026-09-30T12:00:00',
                    str(int(expected.timestamp())), str(int(expected.timestamp() * 1000))):
            with self.subTest(raw=raw):
                self.assertEqual(parse_dateutc(raw, now, 900), (expected, False))

    def test_implausible_clock_falls_back_to_server_time(self):
        self.assertEqual(parse_dateutc('2000-01-01 00:00:00', PUSH_NOW, 900), (PUSH_NOW, True))
        self.assertEqual(parse_dateutc('garbage', PUSH_NOW, 900), (PUSH_NOW, True))
