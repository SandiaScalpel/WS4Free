"""Neighbouring Weather Underground stations (weather.neighbours)."""
import datetime as dt
from decimal import Decimal
from io import StringIO
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from weather import neighbours
from weather.models import LatestReading, Neighbour, NeighbourReading, Observation

from .helpers import make_station

UTC = dt.UTC
NOW = dt.datetime(2026, 7, 1, 18, 2, tzinfo=UTC)          # noon in Denver
KEY = 'secret-wu-key'


def wu_obs(wu_id='KNMTEST1', when=NOW, temp=25.0, humidity=30, qc=1, **extra):
    return {'observations': [{
        'stationID': wu_id, 'obsTimeUtc': when.strftime('%Y-%m-%dT%H:%M:%SZ'), 'neighborhood': f'{wu_id} street',
        'lat': 35.2, 'lon': -106.6, 'humidity': humidity, 'qcStatus': qc,
        'metric': {'temp': temp, 'dewpt': 5.0, 'elev': 1520.0}, **extra}]}


def response(status=200, payload=None):
    return mock.Mock(status_code=status, json=mock.Mock(return_value=payload))


def session(*responses):
    s = mock.Mock()
    s.get.side_effect = list(responses)
    return s


@override_settings(WU_API_KEY=KEY, WU_API_URL='https://wu.test/current', WU_POLL_MINUTES=10)
class ClientTests(TestCase):
    def setUp(self):
        self.station = make_station(latitude=Decimal('35.1'), longitude=Decimal('-106.6'), elevation_m=1500.0)

    def test_normalise_id(self):
        self.assertEqual(neighbours.normalise_id(' knmalbuq123 '), 'KNMALBUQ123')
        self.assertEqual(neighbours.normalise_id('https://www.wunderground.com/dashboard/pws/KXXEXAMPLE12?cm_ven=x'), 'KXXEXAMPLE12')
        self.assertIsNone(neighbours.normalise_id('not an id!'))
        self.assertIsNone(neighbours.normalise_id(''))

    def test_fetch_asks_for_metric_and_parses(self):
        s = session(response(payload=wu_obs(temp=24.5, humidity=31)))
        obs = neighbours.fetch_current('KNMTEST1', s)
        params = s.get.call_args.kwargs['params']
        self.assertEqual((params['stationId'], params['units'], params['apiKey']), ('KNMTEST1', 'm', KEY))
        self.assertEqual((obs['timestamp'], obs['temp_c'], obs['humidity'], obs['elevation_m']), (NOW, 24.5, 31.0, 1520.0))

    def test_fetch_errors_never_include_the_key(self):
        error = requests.ConnectionError(f'HTTPSConnectionPool: /current?apiKey={KEY}')
        s = mock.Mock()
        s.get.side_effect = error
        with self.assertRaises(neighbours.WUError) as raised:
            neighbours.fetch_current('KNMTEST1', s)
        self.assertNotIn(KEY, str(raised.exception))
        with self.assertRaises(neighbours.WUKeyError):
            neighbours.fetch_current('KNMTEST1', session(response(401)))
        self.assertIsNone(neighbours.fetch_current('KNMTEST1', session(response(204))))
        with self.assertRaises(neighbours.WUError):
            neighbours.fetch_current('KNMTEST1', session(response(200, {'nope': 1})))

    def test_implausible_values_are_dropped(self):
        obs = neighbours.parse(wu_obs(temp=-999.0, humidity=140)['observations'][0])
        self.assertIsNone(obs['temp_c'])
        self.assertIsNone(obs['humidity'])

    def test_add_checks_the_station_exists(self):
        n = neighbours.add(self.station, 'knmtest1', session(response(payload=wu_obs())), now=NOW)
        self.assertEqual((n.wu_id, n.label, float(n.latitude)), ('KNMTEST1', 'KNMTEST1 street', 35.2))
        self.assertEqual(n.readings.count(), 1)
        with self.assertRaisesMessage(ValueError, 'already on the list'):
            neighbours.add(self.station, 'KNMTEST1', session())
        with self.assertRaisesMessage(ValueError, 'no current reading'):
            neighbours.add(self.station, 'KNMGONE1', session(response(204)))

    def test_poll_fetches_due_neighbours_spaced_out(self):
        for wu_id in ('KNMA1', 'KNMB1'):
            Neighbour.objects.create(station=self.station, wu_id=wu_id)
        s = session(response(payload=wu_obs('KNMA1')), response(payload=wu_obs('KNMB1')))
        sleep = mock.Mock()
        self.assertEqual(neighbours.poll(now=NOW, session=s, sleep=sleep), 2)
        sleep.assert_called_once_with(neighbours.SPACING_S)
        # Not due again for WU_POLL_MINUTES; the same reading twice isn't stored twice.
        self.assertEqual(neighbours.poll(now=NOW + dt.timedelta(minutes=5), session=session(), sleep=sleep), 0)
        s = session(response(payload=wu_obs('KNMA1')), response(204))
        self.assertEqual(neighbours.poll(now=NOW + dt.timedelta(minutes=10), session=s, sleep=sleep), 0)
        self.assertIn('offline', Neighbour.objects.get(wu_id='KNMB1').last_error)
        self.assertEqual(NeighbourReading.objects.count(), 2)

    def test_poll_stops_on_a_rejected_key(self):
        for wu_id in ('KNMA1', 'KNMB1'):
            Neighbour.objects.create(station=self.station, wu_id=wu_id)
        s = session(response(401), response(payload=wu_obs('KNMB1')))
        neighbours.poll(now=NOW, session=s, sleep=mock.Mock())
        self.assertEqual(s.get.call_count, 1)
        self.assertIn('rejected', Neighbour.objects.get(wu_id='KNMA1').last_error)

    def test_command_without_key_does_nothing(self):
        Neighbour.objects.create(station=self.station, wu_id='KNMA1')
        with override_settings(WU_API_KEY=''), mock.patch('weather.neighbours.requests') as req:
            call_command('poll_neighbours', stdout=StringIO())
            req.get.assert_not_called()

    def test_calls_per_day(self):
        self.assertEqual(neighbours.calls_per_day(10), 1440)


class ComparisonTests(TestCase):
    def setUp(self):
        self.station = make_station(latitude=Decimal('35.1'), longitude=Decimal('-106.6'))
        self.ns = [Neighbour.objects.create(station=self.station, wu_id=f'KNM{i}') for i in range(5)]

    def reading(self, n, when, temp, humidity=30.0, qc=1):
        NeighbourReading.objects.create(neighbour=n, timestamp=when, temp_c=temp, humidity=humidity, qc_status=qc)

    def test_average_leaves_out_the_highest_and_lowest(self):
        average, used = neighbours.neighbour_average([20.0, 20.5, 21.0, 20.2, 35.0, None])
        self.assertAlmostEqual(average, (20.2 + 20.5 + 21.0) / 3)
        self.assertEqual(used, 3)
        self.assertEqual(neighbours.neighbour_average([20.0, 30.0]), (25.0, 2))     # too few to trim
        self.assertEqual(neighbours.neighbour_average([20.0, 30.0, 22.0]), (22.0, 1))
        self.assertEqual(neighbours.neighbour_average([None]), (None, 0))

    def test_compare_matches_the_same_interval(self):
        end = dt.datetime(2026, 7, 1, 18, 5, tzinfo=UTC)          # interval end, sun up in Denver
        Observation.objects.create(station=self.station, timestamp=end, source='api', temp_c=23.0, humidity=25.0, solar_wm2=800)
        night = dt.datetime(2026, 7, 2, 8, 0, tzinfo=UTC)          # 2 AM in Denver
        Observation.objects.create(station=self.station, timestamp=night, source='api', temp_c=14.0, humidity=50.0, solar_wm2=0)
        for n, t in zip(self.ns, (20.0, 20.5, 21.0, 20.2, 35.0)):
            self.reading(n, end - dt.timedelta(minutes=2), t)          # 18:03 → the 18:05 interval
            self.reading(n, night - dt.timedelta(minutes=1), t - 6, 55.0)
        self.reading(self.ns[0], end - dt.timedelta(minutes=1), 99.0, qc=-1)  # failed WU's check: ignored
        result = neighbours.compare(self.station, end - dt.timedelta(hours=1), night + dt.timedelta(hours=1))
        first = result['rows'][0]
        self.assertEqual((first['t'], first['sunny']), (end, True))
        day_avg = (20.2 + 20.5 + 21.0) / 3
        self.assertEqual(first['temp_c'][0], 23.0)
        self.assertAlmostEqual(first['temp_c'][1], day_avg)
        self.assertEqual(first['temp_c'][2], 3)
        summary = result['summary']['temp_c']
        self.assertAlmostEqual(summary['day']['mean'], 23.0 - day_avg)
        self.assertAlmostEqual(summary['night']['mean'], 14.0 - (day_avg - 6))
        self.assertEqual(summary['all']['count'], 2)
        self.assertEqual(result['summary']['humidity']['night']['mean'], -5.0)
        self.assertAlmostEqual(result['by_hour']['temp_c'][12], 23.0 - day_avg)        # noon local
        # The outlier stands out against the others.
        self.assertGreater(result['neighbours'][self.ns[4].pk]['temp_c'][0], 10)

    def test_left_out_neighbours_dont_count(self):
        end = dt.datetime(2026, 7, 1, 18, 5, tzinfo=UTC)
        Observation.objects.create(station=self.station, timestamp=end, source='api', temp_c=23.0)
        self.reading(self.ns[0], end, 20.0)
        self.reading(self.ns[1], end, 30.0)
        Neighbour.objects.filter(pk=self.ns[1].pk).update(include=False)
        row = neighbours.compare(self.station, end - dt.timedelta(hours=1), end + dt.timedelta(minutes=1))['rows'][0]
        self.assertEqual(row['temp_c'], (23.0, 20.0, 1))

    def test_live_uses_recent_readings_only(self):
        self.reading(self.ns[0], NOW - dt.timedelta(minutes=5), 20.0)
        self.reading(self.ns[1], NOW - dt.timedelta(minutes=10), 22.0)
        self.reading(self.ns[2], NOW - dt.timedelta(hours=2), 40.0)              # too old
        live = neighbours.live(self.station, NOW)
        self.assertEqual((live['temp_c'], live['n']), (21.0, 2))
        NeighbourReading.objects.all().delete()
        self.assertIsNone(neighbours.live(self.station, NOW))

    def test_distance(self):
        n = self.ns[0]
        n.latitude, n.longitude = Decimal('35.2'), Decimal('-106.6')
        self.assertAlmostEqual(neighbours.distance_km(self.station, n), 11.12, places=1)


@override_settings(WU_API_KEY=KEY, WU_API_URL='https://wu.test/current')
class PageTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('owner', password='x' * 16)
        self.other = User.objects.create_user('other', password='x' * 16)
        for user in (self.owner, self.other):         # a second factor, so no 2FA reminder page
            TOTPDevice.objects.create(user=user, name='phone', confirmed=True)
        self.station = make_station(owner=self.owner, slug='mesa', is_public=True,
                                    latitude=Decimal('35.1'), longitude=Decimal('-106.6'))
        self.url = reverse('weather:station-neighbours', args=['mesa'])

    def test_owner_only(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    @mock.patch('weather.neighbours.requests')
    def test_add_list_toggle_remove(self, req):
        req.RequestException = requests.RequestException
        req.get.return_value = response(payload=wu_obs('KNMTEST1', when=dt.datetime.now(UTC)))
        self.client.force_login(self.owner)
        self.client.post(self.url, {'wu_id': 'knmtest1'})
        n = Neighbour.objects.get()
        html = self.client.get(self.url).content.decode()
        self.assertIn('KNMTEST1', html)
        self.assertIn('Right now', html)
        self.assertNotIn(KEY, html)
        self.client.post(reverse('weather:station-neighbour-include', args=['mesa', n.pk]))
        self.assertFalse(Neighbour.objects.get().include)
        self.client.post(reverse('weather:station-neighbour-delete', args=['mesa', n.pk]))
        self.assertFalse(Neighbour.objects.exists())
        self.assertFalse(NeighbourReading.objects.exists())

    def test_bad_id_is_reported(self):
        self.client.force_login(self.owner)
        response_ = self.client.post(self.url, {'wu_id': '!!'}, follow=True)
        self.assertContains(response_, "doesn&#x27;t look like")

    def test_without_a_key_explains_how(self):
        self.client.force_login(self.owner)
        with override_settings(WU_API_KEY=''):
            html = self.client.get(self.url).content.decode()
        self.assertIn('WU_API_KEY', html)
        self.assertNotIn('name="wu_id"', html)

    def test_dashboard_line_for_the_owner_only(self):
        now = dt.datetime.now(UTC)
        LatestReading.objects.create(station=self.station, timestamp=now, source='api', data={'temp_c': 24.0, 'humidity': 30})
        n = Neighbour.objects.create(station=self.station, wu_id='KNMTEST1')
        NeighbourReading.objects.create(neighbour=n, timestamp=now - dt.timedelta(minutes=3), temp_c=22.0, humidity=35)
        live = reverse('weather:station-live', args=['mesa'])
        self.assertNotIn('Neighbours', self.client.get(live).content.decode())
        self.client.force_login(self.owner)
        html = self.client.get(live).content.decode()
        self.assertIn('Neighbours', html)
        self.assertIn('+3.6°F', html)                     # 2 °C warmer
