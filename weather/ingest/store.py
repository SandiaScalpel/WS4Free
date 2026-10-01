"""Writing readings to the database.

Pushes and API records describe the same weather at different cadences: the
console pushes every 16–60 s, while ambientweather.net archives one record per
5 minutes. Both land on the station's archive grid so they meet on the
(station, timestamp) unique key. Like Ambient's archive and WeeWX, a row is
stamped with the END of its interval and covers (timestamp − interval, timestamp]:

  * a push is assigned to the interval it falls in and *merged* into that row —
    peak fields keep the highest value seen, everything else the latest;
  * API/backfill rows are inserted only where the bucket is empty, so re-running a
    backfill, or polling a period the console already pushed, changes nothing.
"""
import datetime as dt
import logging
import math

from django.db import IntegrityError, transaction
from django.db.models import Q

from ..models import OBSERVATION_FIELDS, LatestReading, Observation, Station
from .rain import compute_rain_increments

log = logging.getLogger(__name__)

# Within one bucket these keep the maximum seen; a gust or a burst of heavy rain
# must not be overwritten by a calmer sample a few seconds later.
PEAK_FIELDS = ('wind_gust_ms', 'rain_rate_mmh', 'solar_wm2', 'uv_index')


def interval_end(timestamp, interval_s):
    """End of the archive interval containing `timestamp`: 12:03:17 → 12:05:00,
    and a reading exactly on a boundary (12:05:00) closes that interval."""
    end = math.ceil(timestamp.timestamp() / interval_s) * interval_s
    return dt.datetime.fromtimestamp(end, dt.UTC)


def mark_dirty(station_id, since):
    """Move the station's rollup watermark back to `since` if it is later (or unset).
    One conditional UPDATE, so concurrent writers can only ever widen the range."""
    Station.objects.filter(pk=station_id).filter(
        Q(rollup_dirty_from__isnull=True) | Q(rollup_dirty_from__gt=since)
    ).update(rollup_dirty_from=since)


def update_latest(station, reading, source):
    latest = LatestReading.objects.filter(station=station).only('timestamp').first()
    if latest is not None and latest.timestamp > reading.timestamp:
        return False
    LatestReading.objects.update_or_create(station=station, defaults={
        'timestamp': reading.timestamp, 'source': source,
        'data': dict(reading.values), 'extra': dict(reading.extra),
    })
    return True


def record_push(station, reading, source):
    """Merge one pushed reading into its archive bucket. Returns the Observation."""
    bucket = interval_end(reading.timestamp, station.archive_interval_s)
    with transaction.atomic():
        obs = Observation.objects.select_for_update().filter(station=station, timestamp=bucket).first()
        if obs is None:
            try:
                with transaction.atomic():
                    obs = Observation.objects.create(
                        station=station, timestamp=bucket, interval_s=station.archive_interval_s,
                        source=source, extra=dict(reading.extra), **reading.values,
                    )
                created = True
            except IntegrityError:
                # Another push for the same bucket won the race; merge into it.
                obs = Observation.objects.select_for_update().get(station=station, timestamp=bucket)
                created = False
        else:
            created = False

        if not created:
            for column, value in reading.values.items():
                current = getattr(obs, column)
                if column in PEAK_FIELDS and current is not None:
                    value = max(current, value)
                setattr(obs, column, value)
            obs.extra = {**obs.extra, **reading.extra}
            obs.sample_count += 1
            obs.source = source
            obs.save()

    from ..calibration import apply_to_new_rows as calibrate_new_rows
    from ..quality import apply_to_new_rows
    apply_to_new_rows(station, bucket - dt.timedelta(seconds=1), bucket)
    # A merge overwrote these columns with raw values; a fresh row has no backups anyway.
    calibrate_new_rows(station, bucket - dt.timedelta(seconds=1), bucket, written_fields=set(reading.values))
    update_latest(station, reading, source)
    mark_dirty(station.pk, bucket)
    compute_rain_increments(station, since=bucket)
    return obs


def record_archive(station, readings, source, batch_size=500):
    """Insert archive records (API poll/backfill) into empty buckets only.

    Returns the number of new rows. Existing rows — pushed or previously fetched —
    are left untouched. Rain increments are NOT recomputed here; callers run
    compute_rain_increments once over the whole range they wrote.
    """
    if not readings:
        return 0
    interval = station.archive_interval_s
    by_bucket = {}
    for reading in readings:
        by_bucket.setdefault(interval_end(reading.timestamp, interval), reading)
    start, end = min(by_bucket), max(by_bucket)
    existing = set(
        Observation.objects.filter(station=station, timestamp__range=(start, end))
        .values_list('timestamp', flat=True)
    )
    new_rows = [
        Observation(station=station, timestamp=bucket, interval_s=interval, source=source,
                    extra=dict(reading.extra), **reading.values)
        for bucket, reading in sorted(by_bucket.items()) if bucket not in existing
    ]
    # ignore_conflicts covers a push landing between the read above and this insert.
    Observation.objects.bulk_create(new_rows, batch_size=batch_size, ignore_conflicts=True)

    newest = max(readings, key=lambda r: r.timestamp)
    update_latest(station, newest, source)
    if new_rows:
        from ..calibration import apply_to_new_rows as calibrate_new_rows
        from ..quality import apply_to_new_rows
        apply_to_new_rows(station, new_rows[0].timestamp - dt.timedelta(seconds=1), new_rows[-1].timestamp)
        calibrate_new_rows(station, new_rows[0].timestamp - dt.timedelta(seconds=1), new_rows[-1].timestamp)
        mark_dirty(station.pk, new_rows[0].timestamp)
    return len(new_rows)


__all__ = ['OBSERVATION_FIELDS', 'PEAK_FIELDS', 'interval_end', 'mark_dirty',
           'record_archive', 'record_push', 'update_latest']
