import datetime as dt

from django.test import TestCase

from weather.ingest.store import interval_end, mark_dirty, record_archive, record_push
from weather.models import LatestReading, Observation, Station

from .helpers import T0, make_station, reading

PUSH = Observation.SOURCE_AMBIENT_PUSH


class IntervalEndTests(TestCase):
    def test_reading_belongs_to_the_interval_it_falls_in(self):
        five = T0 + dt.timedelta(minutes=5)
        self.assertEqual(interval_end(T0 + dt.timedelta(seconds=1), 300), five)
        self.assertEqual(interval_end(T0 + dt.timedelta(minutes=4, seconds=59), 300), five)
        self.assertEqual(interval_end(five, 300), five)                       # boundary closes its interval
        self.assertEqual(interval_end(five + dt.timedelta(microseconds=1), 300), five + dt.timedelta(minutes=5))


class RecordPushTests(TestCase):
    def setUp(self):
        self.station = make_station()

    def test_pushes_in_one_bucket_merge_into_one_row(self):
        record_push(self.station, reading(0, 10, temp_c=20.0, wind_gust_ms=8.0, solar_wm2=500.0), PUSH)
        record_push(self.station, reading(1, 10, temp_c=20.4, wind_gust_ms=3.0, solar_wm2=650.0), PUSH)
        record_push(self.station, reading(4, 50, temp_c=20.6, wind_gust_ms=5.0, solar_wm2=600.0), PUSH)

        row = Observation.objects.get()
        self.assertEqual(row.timestamp, T0 + dt.timedelta(minutes=5))
        self.assertEqual(row.temp_c, 20.6)        # latest wins
        self.assertEqual(row.wind_gust_ms, 8.0)   # peak kept, not overwritten by a calmer sample
        self.assertEqual(row.solar_wm2, 650.0)
        self.assertEqual(row.sample_count, 3)

    def test_next_bucket_gets_new_row(self):
        record_push(self.station, reading(5, 0, temp_c=20.0), PUSH)
        record_push(self.station, reading(5, 1, temp_c=21.0), PUSH)
        self.assertEqual(Observation.objects.count(), 2)

    def test_latest_reading_tracks_newest_only(self):
        record_push(self.station, reading(10, temp_c=22.0), PUSH)
        record_push(self.station, reading(2, temp_c=19.0), PUSH)   # late, out-of-order upload
        self.assertEqual(LatestReading.objects.get(station=self.station).data['temp_c'], 22.0)

    def test_marks_rollups_dirty_from_earliest_write(self):
        record_push(self.station, reading(10, temp_c=22.0), PUSH)
        record_push(self.station, reading(2, temp_c=19.0), PUSH)
        record_push(self.station, reading(30, temp_c=23.0), PUSH)
        self.station.refresh_from_db()
        self.assertEqual(self.station.rollup_dirty_from, T0 + dt.timedelta(minutes=5))

    def test_push_merges_into_existing_api_row(self):
        record_archive(self.station, [reading(5, temp_c=18.0, wind_gust_ms=9.0)], Observation.SOURCE_API)
        record_push(self.station, reading(2, temp_c=18.5, wind_gust_ms=4.0), PUSH)   # 12:02 → row 12:05
        row = Observation.objects.get()
        self.assertEqual((row.temp_c, row.wind_gust_ms, row.source), (18.5, 9.0, PUSH))


class RecordArchiveTests(TestCase):
    def setUp(self):
        self.station = make_station()

    def test_fills_gaps_only(self):
        record_push(self.station, reading(3, temp_c=30.0), PUSH)                     # → row 12:05
        added = record_archive(self.station, [reading(m, temp_c=10.0) for m in (0, 5, 10)], Observation.SOURCE_API)
        self.assertEqual(added, 2)
        self.assertEqual(Observation.objects.get(timestamp=T0 + dt.timedelta(minutes=5)).temp_c, 30.0)

    def test_rerun_is_a_no_op(self):
        batch = [reading(m, temp_c=10.0) for m in range(0, 60, 5)]
        self.assertEqual(record_archive(self.station, batch, Observation.SOURCE_BACKFILL), 12)
        self.assertEqual(record_archive(self.station, batch, Observation.SOURCE_BACKFILL), 0)
        self.assertEqual(Observation.objects.count(), 12)

    def test_does_not_touch_other_stations(self):
        other = make_station(name='Other', mac_address='11:22:33:44:55:66')
        record_archive(other, [reading(0, temp_c=1.0)], Observation.SOURCE_API)
        self.assertEqual(record_archive(self.station, [reading(0, temp_c=2.0)], Observation.SOURCE_API), 1)


class MarkDirtyTests(TestCase):
    def test_only_moves_backwards(self):
        station = make_station()
        mark_dirty(station.pk, T0)
        mark_dirty(station.pk, T0 + dt.timedelta(hours=1))
        self.assertEqual(Station.objects.get().rollup_dirty_from, T0)
