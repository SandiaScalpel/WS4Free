import datetime as dt
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from weather import dashboard, quality
from weather.ingest.store import record_push
from weather.models import DailyRollup, DataExclusion, ExcludedValue, LatestReading, Observation, Station
from weather.rollups import refresh_station
from weather.units import UnitPrefs

from .helpers import make_station, reading

UTC = dt.UTC
T0 = dt.datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def fill(station, hours=6, **values):
    for i in range(1, hours * 12 + 1):
        Observation.objects.create(station=station, timestamp=T0 + dt.timedelta(minutes=5 * i), source='api',
                                   temp_c=20.0 + (i % 3), humidity=40.0, dewpoint_c=6.0, wind_speed_ms=2.0,
                                   rain_daily_mm=0.254 * i, **values)


def exclusion(station, start, end, groups):
    e = DataExclusion.objects.create(station=station, start=start, end=end, groups=groups)
    quality.apply_exclusion(e)
    e.refresh_from_db()
    return e


class ApplyRemoveTests(TestCase):
    def setUp(self):
        self.station = make_station()
        fill(self.station)
        self.window = (T0 + dt.timedelta(hours=1), T0 + dt.timedelta(hours=2))

    def test_values_set_aside_not_deleted(self):
        before = dict(Observation.objects.values_list('pk', 'temp_c'))
        e = exclusion(self.station, *self.window, ['temp'])
        inside = Observation.objects.filter(timestamp__gt=self.window[0], timestamp__lte=self.window[1])
        self.assertEqual(e.readings, 12)
        self.assertEqual(e.status, 'applied')
        self.assertFalse(inside.exclude(temp_c__isnull=True).exists())
        self.assertFalse(inside.exclude(dewpoint_c__isnull=True).exists())   # dew point depends on temperature
        self.assertEqual(inside.exclude(humidity__isnull=True).count(), 12)   # other measurements untouched
        held = dict(ExcludedValue.objects.filter(field='temp_c').values_list('observation_id', 'value'))
        self.assertEqual(held, {pk: before[pk] for pk in held})

    def test_remove_restores_exactly(self):
        before = list(Observation.objects.order_by('pk').values_list('temp_c', 'dewpoint_c', 'humidity'))
        e = exclusion(self.station, *self.window, ['temp', 'humidity'])
        quality.remove_exclusion(e)
        after = list(Observation.objects.order_by('pk').values_list('temp_c', 'dewpoint_c', 'humidity'))
        self.assertEqual(before, after)
        self.assertFalse(ExcludedValue.objects.exists())
        self.assertFalse(DataExclusion.objects.exists())

    def test_overlapping_exclusions(self):
        a = exclusion(self.station, T0 + dt.timedelta(hours=1), T0 + dt.timedelta(hours=3), ['temp'])
        exclusion(self.station, T0 + dt.timedelta(hours=2), T0 + dt.timedelta(hours=4), ['temp'])
        quality.remove_exclusion(a)
        # Hour 1–2 comes back; hour 2–3 stays excluded because the second exclusion still covers it.
        restored = Observation.objects.filter(timestamp__gt=T0 + dt.timedelta(hours=1), timestamp__lte=T0 + dt.timedelta(hours=2))
        still = Observation.objects.filter(timestamp__gt=T0 + dt.timedelta(hours=2), timestamp__lte=T0 + dt.timedelta(hours=4))
        self.assertFalse(restored.filter(temp_c__isnull=True).exists())
        self.assertFalse(still.exclude(temp_c__isnull=True).exists())

    def test_rollups_follow_after_refresh(self):
        Observation.objects.filter(timestamp=T0 + dt.timedelta(hours=1, minutes=30)).update(temp_c=79.0)  # a glitch
        Station.objects.update(rollup_dirty_from=T0)
        refresh_station(self.station, now=T0 + dt.timedelta(days=1))
        self.assertEqual(DailyRollup.objects.get().temp_max_c, 79.0)
        e = exclusion(self.station, *self.window, ['temp'])
        refresh_station(self.station, now=T0 + dt.timedelta(days=1))
        self.assertEqual(DailyRollup.objects.get().temp_max_c, 22.0)
        quality.remove_exclusion(e)
        refresh_station(self.station, now=T0 + dt.timedelta(days=1))
        self.assertEqual(DailyRollup.objects.get().temp_max_c, 79.0)

    def test_rain_exclusion_removes_counters_so_rain_is_not_rebuilt(self):
        Station.objects.update(rollup_dirty_from=T0)
        refresh_station(self.station, now=T0 + dt.timedelta(days=1))
        total = DailyRollup.objects.get().rain_mm
        exclusion(self.station, *self.window, ['rain'])
        refresh_station(self.station, now=T0 + dt.timedelta(days=1))
        inside = Observation.objects.filter(timestamp__gt=self.window[0], timestamp__lte=self.window[1])
        self.assertFalse(inside.exclude(rain_mm__isnull=True).exists())
        self.assertLess(DailyRollup.objects.get().rain_mm, total)


class OngoingTests(TestCase):
    def test_new_pushes_are_excluded_and_hidden_live(self):
        station = make_station()
        e = DataExclusion.objects.create(station=station, start=T0, end=None, groups=['humidity'], status='applied')
        record_push(station, reading(7, temp_c=25.0, humidity=99.0), Observation.SOURCE_AMBIENT_PUSH)
        row = Observation.objects.get()
        self.assertIsNone(row.humidity)
        self.assertEqual(row.temp_c, 25.0)
        self.assertEqual(ExcludedValue.objects.get().value, 99.0)
        ctx = dashboard.build(station, UnitPrefs.for_system('metric'), now=T0 + dt.timedelta(minutes=8))
        self.assertNotIn('humidity', ctx['now'])
        self.assertEqual(ctx['now']['temp_c'], 25.0)
        self.assertIn('humidity', LatestReading.objects.get().data)    # stored as received; hidden on display
        e.delete()


class PageTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('owner', password='x' * 16)
        self.other = User.objects.create_user('other', password='x' * 16)
        for user in (self.owner, self.other):
            TOTPDevice.objects.create(user=user, name='phone', confirmed=True)
        self.station = make_station(owner=self.owner, is_public=True)
        self.url = reverse('weather:station-quality', args=[self.station.slug])

    def test_owner_only(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    @mock.patch('weather.views.quality.launch')
    def test_add_in_station_time_zone_and_launch(self, launch):
        self.client.force_login(self.owner)
        response = self.client.post(self.url, {'start': '2023-06-01T00:00', 'end': '2023-10-15T00:00',
                                               'groups': ['temp', 'humidity'], 'reason': 'Sensor failing'})
        self.assertRedirects(response, self.url)
        e = DataExclusion.objects.get()
        self.assertEqual(e.start, dt.datetime(2023, 6, 1, 6, 0, tzinfo=UTC))     # midnight MDT
        self.assertEqual(e.groups, ['temp', 'humidity'])
        launch.assert_called_once_with(e, 'apply')

    def test_validation(self):
        self.client.force_login(self.owner)
        future = (timezone.now() + dt.timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
        for data, message in (({'start': '2023-06-02T00:00', 'end': '2023-06-01T00:00', 'groups': ['temp']}, 'after the start'),
                              ({'start': future, 'groups': ['temp']}, 'in the future'),
                              ({'start': '2023-06-01T00:00'}, 'This field is required')):
            with self.subTest(message=message):
                self.assertContains(self.client.post(self.url, data), message)
        self.assertFalse(DataExclusion.objects.exists())

    def test_prefill_from_almanac_link(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url, {'start': '2023-07-20', 'group': 'temp', 'reason': 'Warmest night looks wrong'})
        self.assertContains(response, 'value="2023-07-20T00:00"')
        self.assertContains(response, 'value="2023-07-21T00:00"')
        self.assertContains(response, 'Warmest night looks wrong')

    @mock.patch('weather.views.quality.launch')
    def test_remove_marks_and_launches(self, launch):
        e = DataExclusion.objects.create(station=self.station, start=T0, end=T0 + dt.timedelta(hours=1), groups=['temp'], status='applied')
        self.client.force_login(self.owner)
        self.client.post(reverse('weather:station-quality-remove', args=[self.station.slug, e.pk]))
        self.assertEqual(DataExclusion.objects.get().status, 'removing')
        launch.assert_called_once()

    def test_command_runs_both_ways(self):
        fill(self.station, hours=1)
        e = DataExclusion.objects.create(station=self.station, start=T0, end=T0 + dt.timedelta(hours=1), groups=['wind'])
        call_command('run_exclusion', e.pk, 'apply', stdout=StringIO())
        self.assertFalse(Observation.objects.exclude(wind_speed_ms__isnull=True).exists())
        call_command('run_exclusion', e.pk, 'remove', stdout=StringIO())
        self.assertFalse(Observation.objects.filter(wind_speed_ms__isnull=True).exists())
