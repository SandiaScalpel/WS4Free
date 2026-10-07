"""Neighbouring Weather Underground stations (weather.neighbours)."""
import datetime as dt
import math
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


def _admin():
    """Neighbours use the site's Weather Underground key: administrators' stations only."""
    from django.contrib.auth import get_user_model
    return get_user_model().objects.get_or_create(username='admin', defaults={'is_staff': True})[0]

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
        self.station = make_station(latitude=Decimal('35.1'), longitude=Decimal('-106.6'), elevation_m=1500.0,
                                    owner=_admin())

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
        self.station = make_station(latitude=Decimal('35.1'), longitude=Decimal('-106.6'), owner=_admin())
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
        self.owner = User.objects.create_user('owner', password='x' * 16, is_staff=True)   # site's WU key
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


class SuggestedCalibrationTests(TestCase):
    """A sensor with a known error, against neighbours that read the truth."""
    DAYS = (dt.date(2026, 7, 1), dt.date(2026, 7, 6))

    def setUp(self):
        from weather.calibration import solar_elevation
        self.station = make_station(latitude=Decimal('35.1'), longitude=Decimal('-106.6'), owner=_admin())
        ns = [Neighbour.objects.create(station=self.station, wu_id=f'KXX{i}') for i in range(3)]
        phase = neighbours._phase_finder(self.station)
        tz = self.station.tzinfo
        t = dt.datetime.combine(self.DAYS[0], dt.time(), tzinfo=tz).astimezone(UTC)
        end = dt.datetime.combine(self.DAYS[1] + dt.timedelta(days=1), dt.time(), tzinfo=tz).astimezone(UTC)
        observations, readings = [], []
        while t <= end:
            elevation = solar_elevation(35.1, -106.6, t)
            solar = max(0.0, 1000 * math.sin(math.radians(elevation)))
            truth = 22 + 8 * math.sin(math.radians(elevation))
            p = phase(t, solar)
            error = {'night': -1.0, 'day': -1.0 + 0.5 + 2.0 * solar / 1000, 'evening': 3.0}[p]
            observations.append(Observation(station=self.station, timestamp=t, source='api',
                                            temp_c=truth + error, solar_wm2=solar))
            readings += [NeighbourReading(neighbour=n, timestamp=t - dt.timedelta(minutes=1), temp_c=truth + d)
                         for n, d in zip(ns, (-0.1, 0.0, 0.1))]
            t += dt.timedelta(minutes=5)
        Observation.objects.bulk_create(observations)
        NeighbourReading.objects.bulk_create(readings)

    def test_sun_times(self):
        from weather.calibration import sun_times
        sunrise, dusk = sun_times(35.1, -106.6, dt.date(2026, 7, 1), self.station.tzinfo)
        # Albuquerque, July 1: sunrise 05:53 MDT, astronomical dusk 22:13 MDT.
        self.assertLess(abs(sunrise - dt.datetime(2026, 7, 1, 11, 53, tzinfo=UTC)), dt.timedelta(minutes=4))
        self.assertLess(abs(dusk - dt.datetime(2026, 7, 2, 4, 13, tzinfo=UTC)), dt.timedelta(minutes=4))
        phase = neighbours._phase_finder(self.station)
        self.assertEqual(phase(dusk - dt.timedelta(minutes=10), 0.0), 'evening')
        self.assertEqual(phase(dusk + dt.timedelta(minutes=10), 0.0), 'night')
        self.assertEqual(phase(sunrise + dt.timedelta(minutes=10), 20.0), 'night')
        self.assertEqual(phase(sunrise + dt.timedelta(hours=2), 400.0), 'day')

    def test_recovers_the_error_and_ignores_the_evening(self):
        sg = neighbours.suggest_calibration(self.station, *self.DAYS)
        c = sg['coefficients']
        self.assertAlmostEqual(c['night'], -1.0, places=2)
        self.assertAlmostEqual(c['day'], 0.5, places=2)
        self.assertAlmostEqual(c['solar'], 2.0, places=2)
        self.assertAlmostEqual(sg['means']['evening'], 3.0, places=2)
        self.assertEqual(sg['verdict'], 'recommended')
        self.assertLess(sg['validation']['after'], 0.05)
        self.assertEqual(sg['neighbours'], 3)

    def test_uses_the_sensors_own_readings(self):
        from weather import calibration
        from weather.models import TempCalibration
        cal = TempCalibration.objects.create(
            station=self.station, mode='manual', start=dt.datetime(2026, 7, 3, tzinfo=UTC), status='pending',
            coefficients={str(m): {'night': -1.0, 'day': 0.0, 'solar': 0.0} for m in range(1, 13)})
        calibration.apply_calibration(cal)
        self.assertAlmostEqual(neighbours.suggest_calibration(self.station, *self.DAYS)['coefficients']['night'],
                               -1.0, places=2)

    def test_too_few_readings(self):
        sg = neighbours.suggest_calibration(self.station, dt.date(2026, 6, 1), dt.date(2026, 6, 2))
        self.assertIsNone(sg['coefficients'])

    def test_range_starts_at_the_first_reading(self):
        self.assertEqual(neighbours.calibration_range(self.station, today=dt.date(2026, 7, 9)),
                         (dt.date(2026, 6, 30), dt.date(2026, 7, 9)))

    def test_pages(self):
        from weather.models import TempCalibration
        owner = self.station.owner
        TOTPDevice.objects.create(user=owner, name='phone', confirmed=True)
        self.client.force_login(owner)
        url = reverse('weather:station-neighbours', args=[self.station.slug])
        with override_settings(WU_API_KEY=KEY):
            html = self.client.get(url).content.decode()
            self.assertIn('For calibration', html)
            self.assertNotIn('Suggested correction', html)
            html = self.client.get(url, {'cal_start': '2026-07-01', 'cal_end': '2026-07-06'}).content.decode()
        self.assertIn('Suggested correction', html)
        self.assertIn('Recommended', html)
        self.assertIn('cal_mode=neighbours&amp;compare_start=2026-07-01&amp;compare_end=2026-07-06', html)

        quality = reverse('weather:station-quality', args=[self.station.slug])
        html = self.client.get(quality, {'cal_mode': 'neighbours', 'compare_start': '2026-07-01',
                                         'compare_end': '2026-07-06'}).content.decode()
        self.assertIn('Fitted to neighbours', html)
        self.assertIn('value="2026-07-01"', html)
        response_ = self.client.post(quality, {'form': 'calibration', 'cal-mode': 'neighbours', 'cal-start': '2026-07-01T00:00',
                                               'cal-compare_start': '2026-07-01', 'cal-compare_end': '2026-07-06'}, follow=True)
        c = TempCalibration.objects.get()
        self.assertEqual((c.mode, c.status, c.baseline_start), ('neighbours', 'fitted', dt.date(2026, 7, 1)))
        self.assertAlmostEqual(c.coefficients['12']['night'], -1.0, places=2)
        self.assertContains(response_, 'Review it below')
        # Outside the readings: refused.
        c.delete()
        self.assertContains(self.client.post(quality, {'form': 'calibration', 'cal-mode': 'neighbours',
                                                       'cal-start': '2026-07-01T00:00', 'cal-compare_start': '2026-05-01',
                                                       'cal-compare_end': '2026-07-06'}), 'Neighbour readings cover')
        self.assertFalse(TempCalibration.objects.exists())

    def test_no_neighbours_option_without_readings(self):
        from weather.forms import CalibrationForm
        from weather.units import UnitPrefs
        NeighbourReading.objects.all().delete()
        form = CalibrationForm(station=self.station, prefs=UnitPrefs())
        self.assertNotIn('neighbours', [k for k, _ in form.fields['mode'].choices])
