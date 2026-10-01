import datetime as dt
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from weather import calibration as cal, dashboard, quality
from weather.ingest.parsers import Reading
from weather.ingest.store import record_archive, record_push
from weather.models import (
    CalibratedValue, DataExclusion, ExcludedValue, LatestReading, Observation, TempCalibration,
)
from weather.units import UnitPrefs, dewpoint_c

from .helpers import make_station

UTC = dt.UTC
DENVER = ZoneInfo('America/Denver')
NOON = dt.datetime(2025, 7, 1, 18, 0, tzinfo=UTC)        # 12:00 MDT
MIDNIGHT = dt.datetime(2025, 7, 1, 6, 0, tzinfo=UTC)     # 00:00 MDT


def manual(station, night=0.0, day=0.0, solar=0.0, start=dt.datetime(2025, 1, 1, tzinfo=UTC), end=None, status='applied'):
    coef = {str(m): {'night': night, 'day': day, 'solar': solar} for m in range(1, 13)}
    return TempCalibration.objects.create(station=station, mode='manual', start=start, end=end, coefficients=coef, status=status)


class SunAndOffsetTests(SimpleTestCase):
    def test_solar_elevation(self):
        # Boulder-ish latitude at local solar noon in July: ~73°; at midnight well below the horizon.
        self.assertAlmostEqual(cal.solar_elevation(40.0, -105.0, dt.datetime(2025, 7, 1, 19, 5, tzinfo=UTC)), 73, delta=1.5)
        self.assertLess(cal.solar_elevation(40.0, -105.0, dt.datetime(2025, 7, 1, 7, 0, tzinfo=UTC)), -20)

    def test_offset_by_month_sun_and_solar(self):
        st = mock.Mock(tzinfo=DENVER, latitude=40.0, longitude=-105.0)
        c = TempCalibration(coefficients={'7': {'night': -1.0, 'day': 2.0, 'solar': 3.0},
                                          '1': {'night': -2.0, 'day': 1.0, 'solar': 0.0}})
        self.assertAlmostEqual(cal.offset_c(c, st, NOON, 800.0), -1.0 + 2.0 + 2.4)
        self.assertAlmostEqual(cal.offset_c(c, st, MIDNIGHT, 0.0), -1.0)
        self.assertAlmostEqual(cal.offset_c(c, st, NOON, None), -1.0 + 2.0)      # no reading: sun position decides
        self.assertAlmostEqual(cal.offset_c(c, st, dt.datetime(2025, 1, 15, 7, tzinfo=UTC), 0.0), -2.0)
        self.assertEqual(cal.offset_c(c, st, dt.datetime(2025, 4, 1, 18, tzinfo=UTC), 900.0), 0.0)   # month not set


class ApplyRemoveTests(TestCase):
    def setUp(self):
        self.station = make_station(latitude=40.0, longitude=-105.0)
        Observation.objects.create(station=self.station, timestamp=NOON, source='api', temp_c=35.0, humidity=20.0,
                                   dewpoint_c=dewpoint_c(35.0, 20.0), solar_wm2=900.0)
        Observation.objects.create(station=self.station, timestamp=MIDNIGHT, source='api', temp_c=20.0, humidity=50.0,
                                   dewpoint_c=dewpoint_c(20.0, 50.0), solar_wm2=0.0)

    def test_apply_and_remove_exactly(self):
        before = list(Observation.objects.order_by('pk').values_list('temp_c', 'dewpoint_c'))
        c = manual(self.station, night=-1.0, day=2.0, solar=3.0, status='pending')
        self.assertEqual(cal.apply_calibration(c), 2)
        noon = Observation.objects.get(timestamp=NOON)
        night = Observation.objects.get(timestamp=MIDNIGHT)
        self.assertAlmostEqual(noon.temp_c, 35.0 - (-1.0 + 2.0 + 2.7))
        self.assertAlmostEqual(night.temp_c, 21.0)                         # cold at night → corrected upward
        self.assertAlmostEqual(noon.dewpoint_c, dewpoint_c(noon.temp_c, 20.0))
        cal.remove_calibration(c)
        self.assertEqual(list(Observation.objects.order_by('pk').values_list('temp_c', 'dewpoint_c')), before)
        self.assertFalse(CalibratedValue.objects.exists())

    def test_reapplying_never_double_corrects(self):
        c = manual(self.station, night=1.0, status='pending')
        cal.apply_calibration(c)
        cal.apply_calibration(c)
        self.assertAlmostEqual(Observation.objects.get(timestamp=MIDNIGHT).temp_c, 19.0)


class IngestTests(TestCase):
    def setUp(self):
        self.station = make_station(latitude=40.0, longitude=-105.0)
        manual(self.station, night=1.0, day=2.0, start=dt.datetime(2025, 1, 1, tzinfo=UTC))

    def test_pushes_merging_into_one_row_are_corrected_once(self):
        for seconds in (10, 70, 130):
            record_push(self.station, Reading(NOON - dt.timedelta(minutes=4, seconds=-seconds),
                                              {'temp_c': 30.0, 'humidity': 30.0, 'solar_wm2': 0.0}),
                        Observation.SOURCE_AMBIENT_PUSH)
        row = Observation.objects.get()
        self.assertAlmostEqual(row.temp_c, 29.0)                           # solar 0 → night offset only
        self.assertEqual(CalibratedValue.objects.get(field='temp_c').value, 30.0)

    def test_archive_rows_corrected_and_live_reading_too(self):
        record_archive(self.station, [Reading(NOON, {'temp_c': 30.0, 'solar_wm2': 600.0})], Observation.SOURCE_API)
        self.assertAlmostEqual(Observation.objects.get().temp_c, 27.0)
        ctx = dashboard.build(self.station, UnitPrefs.for_system('metric'), now=NOON)
        self.assertAlmostEqual(ctx['now']['temp_c'], 27.0)
        self.assertTrue(ctx['calibrated'])
        self.assertEqual(LatestReading.objects.get().data['temp_c'], 30.0)   # stored as received


class ExclusionInterplayTests(TestCase):
    def setUp(self):
        self.station = make_station(latitude=40.0, longitude=-105.0)
        Observation.objects.create(station=self.station, timestamp=MIDNIGHT, source='api', temp_c=20.0, solar_wm2=0.0)
        self.window = (MIDNIGHT - dt.timedelta(hours=1), MIDNIGHT + dt.timedelta(hours=1))

    def test_calibration_then_exclusion_then_remove_calibration(self):
        c = manual(self.station, night=2.0, status='pending')
        cal.apply_calibration(c)
        e = DataExclusion.objects.create(station=self.station, start=self.window[0], end=self.window[1], groups=['temp'])
        quality.apply_exclusion(e)
        cal.remove_calibration(c)
        self.assertIsNone(Observation.objects.get().temp_c)                # still excluded
        self.assertEqual(ExcludedValue.objects.get(field='temp_c').value, 20.0)   # but holds the original now
        quality.remove_exclusion(e)
        self.assertEqual(Observation.objects.get().temp_c, 20.0)

    def test_exclusion_then_calibration_then_remove_exclusion(self):
        e = DataExclusion.objects.create(station=self.station, start=self.window[0], end=self.window[1], groups=['temp'])
        quality.apply_exclusion(e)
        c = manual(self.station, night=2.0, status='pending')
        cal.apply_calibration(c)                                            # nothing to correct yet
        quality.remove_exclusion(e)
        self.assertAlmostEqual(Observation.objects.get().temp_c, 18.0)     # restored, then corrected
        cal.remove_calibration(c)
        self.assertEqual(Observation.objects.get().temp_c, 20.0)


class FitTests(TestCase):
    """Synthetic station: the site differs from the reference by −1 °C at night and
    +0.5 °C by day (its own microclimate); from 2025 the sensor adds a planted error
    of −1.5 °C at night, +2 °C by day and +3 °C per 1000 W/m². The fit must recover
    the planted error, not the microclimate."""

    def setUp(self):
        self.station = make_station(latitude=40.0, longitude=-105.0)
        self.reference = {}
        rows = []
        for start, end, faulty in ((dt.date(2024, 1, 1), dt.date(2024, 12, 31), False),
                                   (dt.date(2025, 1, 1), dt.date(2025, 9, 30), True)):
            d = start
            while d <= end:
                for hour in range(24):
                    when = dt.datetime(d.year, d.month, d.day, hour, 52, tzinfo=UTC)
                    local = when.astimezone(DENVER)
                    sunny = 7 <= local.hour <= 18
                    solar = (900 * (1 - abs(local.hour - 12.5) / 6.5) if sunny else 0.0) * (0.6 + 0.4 * ((d.day + hour) % 3 == 0))
                    ref = 15 + 10 * (local.hour / 24) + (d.toordinal() % 7) * 0.3
                    site = ref + (0.5 if sunny else -1.0)
                    if faulty:
                        site += -1.5 + (2.0 if solar > 5 else 0.0) + 3.0 * solar / 1000
                    self.reference[when] = ref
                    rows.append(Observation(station=self.station, timestamp=when + dt.timedelta(minutes=3),
                                            source='api', temp_c=site, solar_wm2=solar))
                d += dt.timedelta(days=1)
        Observation.objects.bulk_create(rows, batch_size=2000)

    def test_recovers_planted_error_and_validates(self):
        c = TempCalibration.objects.create(station=self.station, mode='reference', status='fitting',
                                           start=dt.datetime(2025, 1, 1, 7, tzinfo=UTC), end=dt.datetime(2025, 10, 1, tzinfo=UTC),
                                           reference_station='TST', baseline_start=dt.date(2024, 1, 1),
                                           baseline_end=dt.date(2024, 12, 31))
        cal.fit_calibration(c, fetch=lambda code, start, end: {k: v for k, v in self.reference.items()
                                                                if start <= k.astimezone(DENVER).date() <= end})
        c.refresh_from_db()
        self.assertEqual(c.status, 'fitted')
        july = c.coefficients['7']
        self.assertAlmostEqual(july['night'], -1.5, delta=0.15)
        self.assertAlmostEqual(july['day'], 2.0, delta=0.3)
        self.assertAlmostEqual(july['solar'], 3.0, delta=0.5)
        self.assertTrue(c.coefficients['11']['pooled'])                   # no affected data in November
        v = c.validation
        self.assertGreater(v['improvement'], 0.8)
        self.assertLess(v['after'], 0.2)


class FetchTests(SimpleTestCase):
    def test_retries_and_parses(self):
        ok = mock.Mock(status_code=200, text='station,valid,tmpf\nDEN,2025-07-01 00:52,86.00\nDEN,2025-07-01 01:52,\n')
        session = mock.Mock()
        session.get.side_effect = [mock.Mock(status_code=503, text=''), ok]
        sleeps = []
        out = cal.fetch_reference('DEN', dt.date(2025, 7, 1), dt.date(2025, 7, 31), session=session, sleep=sleeps.append)
        self.assertEqual(out, {dt.datetime(2025, 7, 1, 0, 52, tzinfo=UTC): 30.0})
        self.assertIn(5, sleeps)
        params = session.get.call_args.kwargs['params']
        self.assertEqual((params['station'], params['month2'], params['day2']), ('DEN', 8, 1))   # end is exclusive

    def test_gives_up_with_a_clear_message(self):
        session = mock.Mock()
        session.get.return_value = mock.Mock(status_code=503, text='')
        with self.assertRaises(cal.FitError):
            cal.fetch_reference('DEN', dt.date(2025, 7, 1), dt.date(2025, 7, 2), session=session, sleep=lambda s: None)


class PageTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('owner', password='x' * 16)
        TOTPDevice.objects.create(user=self.owner, name='phone', confirmed=True)
        self.station = make_station(owner=self.owner)
        self.url = reverse('weather:station-quality', args=[self.station.slug])
        self.client.force_login(self.owner)

    def post(self, **data):
        base = {'form': 'calibration', 'cal-mode': 'manual', 'cal-start': '2024-06-17T00:00'}
        return self.client.post(self.url, {**base, **{f'cal-{k}': v for k, v in data.items()}})

    @mock.patch('weather.views.calibration.launch')
    def test_manual_in_fahrenheit_stored_in_celsius(self, launch):
        self.post(night='-4.5', day='9', solar='')
        c = TempCalibration.objects.get()
        self.assertEqual((c.mode, c.status), ('manual', 'pending'))
        self.assertAlmostEqual(c.coefficients['3']['night'], -2.5)
        self.assertAlmostEqual(c.coefficients['3']['day'], 5.0)
        self.assertEqual(c.start, dt.datetime(2024, 6, 17, 6, tzinfo=UTC))
        launch.assert_called_once_with(c, 'apply')

    @mock.patch('weather.views.calibration.launch')
    def test_reference_mode_normalises_code_and_launches_fit(self, launch):
        self.post(mode='reference', reference_station='kden', baseline_start='2022-01-01', baseline_end='2022-12-31')
        c = TempCalibration.objects.get()
        self.assertEqual((c.reference_station, c.status), ('DEN', 'fitting'))
        launch.assert_called_once_with(c, 'fit')

    @mock.patch('weather.views.calibration.launch')
    def test_validation(self, launch):
        manual(self.station, night=1.0, start=dt.datetime(2024, 1, 1, tzinfo=UTC))
        for data, message in (({'night': '1'}, 'overlaps an existing calibration'),
                              ({'mode': 'reference', 'reference_station': 'DEN', 'baseline_start': '2024-01-01',
                                'baseline_end': '2024-12-31', 'start': '2030-01-01T00:00'}, 'in the future')):
            with self.subTest(message=message):
                self.assertContains(self.post(**data), message)
        TempCalibration.objects.all().delete()
        self.assertContains(self.post(mode='reference', reference_station='DEN', baseline_start='2024-01-01',
                                      baseline_end='2024-12-31'), 'must end before the affected period')
        self.assertContains(self.post(night='', day='', solar=''), 'at least one non-zero')
        launch.assert_not_called()

    @mock.patch('weather.views.calibration.launch')
    def test_review_apply_and_discard(self, launch):
        c = manual(self.station, night=1.0, status='fitted')
        self.client.post(reverse('weather:station-calibration-apply', args=[self.station.slug, c.pk]))
        self.assertEqual(TempCalibration.objects.get().status, 'pending')
        launch.assert_called_once_with(mock.ANY, 'apply')
        TempCalibration.objects.update(status='fitted')
        self.client.post(reverse('weather:station-calibration-remove', args=[self.station.slug, c.pk]))
        self.assertFalse(TempCalibration.objects.exists())                 # never applied: just discarded

    def test_corrected_label_shown(self):
        from weather.models import Station
        Station.objects.update(is_public=True)
        manual(self.station, night=1.0, start=dt.datetime(2024, 6, 17, tzinfo=UTC))
        for name in ('station-charts', 'station-almanac', 'station-reports', 'station-growing'):
            with self.subTest(page=name):
                self.assertContains(self.client.get(reverse(f'weather:{name}', args=[self.station.slug])), 'are corrected')
