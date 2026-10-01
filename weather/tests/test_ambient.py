import datetime as dt
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from weather.ambient import AmbientAPIError, AmbientClient
from weather.models import LatestReading, Observation, Station

from .fixtures import API_RECORD, MAC
from .helpers import make_station


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code, self._payload, self.text = status, payload, ''

    def json(self):
        return self._payload


def client_with(responses):
    session = mock.Mock()
    session.get.side_effect = responses
    sleeps = []
    clock = iter(range(0, 10_000))
    client = AmbientClient('k' * 64, 'a' * 64, session=session, sleep=sleeps.append, monotonic=lambda: next(clock) * 0.0)
    return client, session, sleeps


def records(start_ms, n, step_ms=300_000):
    """n records newest-first ending at start_ms."""
    return [{**API_RECORD, 'dateutc': start_ms - i * step_ms} for i in range(n)]


class ClientTests(SimpleTestCase):
    def test_retries_429_with_backoff(self):
        client, session, sleeps = client_with([FakeResponse(429), FakeResponse(429), FakeResponse(200, [1])])
        self.assertEqual(client.devices(), [1])
        self.assertEqual(session.get.call_count, 3)
        self.assertIn(1, sleeps)
        self.assertIn(2, sleeps)

    def test_gives_up_eventually(self):
        client, _, _ = client_with([FakeResponse(429)] * 10)
        client.max_retries = 2
        with self.assertRaises(AmbientAPIError):
            client.devices()

    def test_error_message_never_contains_keys(self):
        client, _, _ = client_with([FakeResponse(401)])
        with self.assertRaises(AmbientAPIError) as ctx:
            client.devices()
        self.assertNotIn('k' * 64, str(ctx.exception))

    def test_throttles_to_one_request_per_second(self):
        session = mock.Mock()
        session.get.return_value = FakeResponse(200, [])
        sleeps = []
        client = AmbientClient('k', 'a', session=session, sleep=sleeps.append, monotonic=lambda: 100.0)
        client.devices()
        client.devices()
        self.assertEqual(sleeps, [client.min_interval])

    def test_history_pages_backwards_until_start(self):
        newest = 1_790_000_000_000
        day = 288 * 300_000
        pages = [records(newest, 288), records(newest - day, 288), records(newest - 2 * day, 288)]
        client, session, _ = client_with([FakeResponse(200, p) for p in pages])
        start = dt.datetime.fromtimestamp((newest - day - 100 * 300_000) / 1000, dt.UTC)
        got = list(client.iter_history(MAC, start=start))
        self.assertEqual([len(p) for p in got], [288, 101])
        second_call = session.get.call_args_list[1].kwargs['params']
        self.assertEqual(second_call['endDate'], newest - 287 * 300_000 - 1)
        self.assertEqual(session.get.call_count, 2)

    def test_offline_days_are_stepped_over_not_treated_as_the_start(self):
        # Real API behaviour: endDate only covers the previous 24 h, so a day the
        # station was down returns []. History continues before it.
        newest = 1_790_000_000_000
        older = newest - 3 * 86_400_000
        client, session, _ = client_with([
            FakeResponse(200, records(newest, 288)), FakeResponse(200, []), FakeResponse(200, []),
            FakeResponse(200, records(older, 288)), FakeResponse(200, []), FakeResponse(200, []),
            FakeResponse(200, []),
        ])
        pages = list(client.iter_history(MAC, max_empty_days=3))   # a 2-day outage is crossed; 3 empty days end it
        self.assertEqual([p[0]['dateutc'] for p in pages], [newest, older])
        third = session.get.call_args_list[2].kwargs['params']['endDate']
        second = session.get.call_args_list[1].kwargs['params']['endDate']
        self.assertEqual(second - third, 86_400_000)   # empty day → step back one window

    def test_since_bounds_a_walk_through_empty_days(self):
        newest = 1_790_000_000_000
        start = dt.datetime.fromtimestamp((newest - 3 * 86_400_000) / 1000, dt.UTC)
        client, session, _ = client_with([FakeResponse(200, records(newest, 288))] + [FakeResponse(200, [])] * 10)
        list(client.iter_history(MAC, start=start))
        self.assertLessEqual(session.get.call_count, 4)


class CommandTests(TestCase):
    def setUp(self):
        self.station = make_station()

    def _patch_client(self, **methods):
        fake = mock.Mock(**{f'{name}.return_value': value for name, value in methods.items() if name != 'iter_history'})
        if 'iter_history' in methods:
            fake.iter_history.return_value = iter(methods['iter_history'])
        return mock.patch('weather.management.commands.poll_ambient.AmbientClient', return_value=fake), fake

    def test_poll_fills_gaps_and_updates_latest(self):
        latest = {**API_RECORD, 'dateutc': API_RECORD['dateutc'] + 180_000}
        patcher, _ = self._patch_client(devices=[{'macAddress': MAC, 'lastData': latest}],
                                        device_data=records(API_RECORD['dateutc'], 12))
        with patcher:
            call_command('poll_ambient', stdout=StringIO())
        self.assertEqual(Observation.objects.filter(source=Observation.SOURCE_API).count(), 12)
        self.assertEqual(LatestReading.objects.get().timestamp.timestamp() * 1000, latest['dateutc'])

    def test_backfill_is_idempotent_and_computes_rain(self):
        newest = API_RECORD['dateutc']
        page = [{**r, 'totalrainin': 10 + i * 0.01, 'dailyrainin': 0.1 + i * 0.01, 'eventrainin': 0.5 + i * 0.01}
                for i, r in enumerate(reversed(records(newest, 5)))][::-1]
        for _ in range(2):
            with mock.patch('weather.management.commands.backfill_ambient.AmbientClient') as cls:
                cls.return_value.iter_history.return_value = iter([page])
                call_command('backfill_ambient', self.station.slug, stdout=StringIO())
        self.assertEqual(Observation.objects.count(), 5)
        rain = list(Observation.objects.order_by('timestamp').values_list('rain_mm', flat=True))
        self.assertEqual(rain[0], None)
        self.assertEqual(rain[1:], [0.254] * 4)

    def test_import_creates_station_from_account(self):
        Station.objects.all().delete()
        device = {'macAddress': MAC.lower(), 'info': {'name': 'Test Station', 'coords': {
            'coords': {'lat': 40.015, 'lon': -105.2705}, 'elevation': 1655.0}}, 'lastData': API_RECORD}
        with mock.patch('weather.management.commands.import_ambient_stations.AmbientClient') as cls:
            cls.return_value.devices.return_value = [device]
            call_command('import_ambient_stations', owner='owner', stdout=StringIO())
        st = Station.objects.get()
        self.assertEqual((st.mac_address, st.timezone, st.slug), (MAC, 'America/Denver', 'test-station'))
        self.assertAlmostEqual(float(st.latitude), 40.015)
