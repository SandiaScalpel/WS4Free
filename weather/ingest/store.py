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
from django.utils import timezone

from ..models import OBSERVATION_FIELDS, LatestReading, Observation, SensorGateway, Station
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
    if reading.extra:
        from ..sensors import detect
        detect(station, reading.extra)
    return obs


def console_source(station):
    """The push source a station's own console uploads as."""
    return Observation.SOURCE_ECOWITT_PUSH if station.source == Station.SOURCE_ECOWITT else Observation.SOURCE_AMBIENT_PUSH


def record_gateway(gateway, reading, source):
    """Merge a sensor gateway's upload into its station: extra sensors only.

    Lands in the same archive bucket as the console's readings. If the console
    hasn't written that bucket yet, the row is created with no main readings and
    sample_count 0, which the rollups don't count as coverage; the console's
    push (or an API gap-fill) then fills it in. Returns the kept extra dict."""
    from ..sensors import detect, gateway_extra
    station = gateway.station
    kept = gateway_extra(reading.extra, source, station.source)
    SensorGateway.objects.filter(pk=gateway.pk).update(last_upload_at=timezone.now())
    if not kept:
        return kept
    bucket = interval_end(reading.timestamp, station.archive_interval_s)
    with transaction.atomic():
        obs = Observation.objects.select_for_update().filter(station=station, timestamp=bucket).first()
        if obs is None:
            try:
                with transaction.atomic():
                    Observation.objects.create(station=station, timestamp=bucket, interval_s=station.archive_interval_s,
                                               source=console_source(station), extra=dict(kept), sample_count=0)
                obs = None
            except IntegrityError:
                obs = Observation.objects.select_for_update().get(station=station, timestamp=bucket)
        if obs is not None:
            obs.extra = {**obs.extra, **kept}
            obs.save(update_fields=['extra'])
    SensorGateway.objects.filter(pk=gateway.pk).filter(Q(latest_at__isnull=True) | Q(latest_at__lte=reading.timestamp)).update(
        latest_extra=kept, latest_at=reading.timestamp)
    from ..quality import apply_to_new_rows
    apply_to_new_rows(station, bucket - dt.timedelta(seconds=1), bucket)
    mark_dirty(station.pk, bucket)
    detect(station, kept)
    return kept


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
    existing = set()
    gateway_only = {}           # rows a sensor gateway created before any main reading arrived
    for row in Observation.objects.filter(station=station, timestamp__range=(start, end)).values('pk', 'timestamp', 'sample_count'):
        if row['sample_count'] == 0:
            gateway_only[row['timestamp']] = row['pk']
        existing.add(row['timestamp'])
    filled = []
    for bucket, pk in sorted(gateway_only.items()):
        reading = by_bucket.get(bucket)
        if reading is None:
            continue
        obs = Observation.objects.get(pk=pk)
        for column, value in reading.values.items():
            setattr(obs, column, value)
        obs.extra = {**reading.extra, **obs.extra}
        obs.sample_count, obs.source = 1, source
        obs.save()
        filled.append(obs)
    new_rows = [
        Observation(station=station, timestamp=bucket, interval_s=interval, source=source,
                    extra=dict(reading.extra), **reading.values)
        for bucket, reading in sorted(by_bucket.items()) if bucket not in existing
    ]
    # ignore_conflicts covers a push landing between the read above and this insert.
    Observation.objects.bulk_create(new_rows, batch_size=batch_size, ignore_conflicts=True)

    newest = max(readings, key=lambda r: r.timestamp)
    update_latest(station, newest, source)
    from ..sensors import detect
    seen = {}
    for reading in readings:
        seen.update(reading.extra)
    if seen:
        detect(station, seen)
    written = sorted([r.timestamp for r in new_rows] + [r.timestamp for r in filled])
    if written:
        from ..calibration import apply_to_new_rows as calibrate_new_rows
        from ..quality import apply_to_new_rows
        apply_to_new_rows(station, written[0] - dt.timedelta(seconds=1), written[-1])
        calibrate_new_rows(station, written[0] - dt.timedelta(seconds=1), written[-1])
        mark_dirty(station.pk, written[0])
    return len(written)


__all__ = ['OBSERVATION_FIELDS', 'PEAK_FIELDS', 'console_source', 'interval_end', 'mark_dirty',
           'record_archive', 'record_gateway', 'record_push', 'update_latest']
