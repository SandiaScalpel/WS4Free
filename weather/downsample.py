"""Optional downsampling of old raw observations into 15-minute rows.

Off unless RAW_RETENTION_YEARS > 0. Raw rows older than that (whole local days
only) are merged three-to-one so the hourly/daily rollups — already computed
from the raw data — are unaffected, and recomputing them later from merged rows
gives the same answers:

  * temperature, humidity, pressure: interval-weighted mean, plus true min/max
    in the *_min/*_max columns;
  * wind: mean speed, max gust, speed-weighted vector-mean direction;
  * rain: summed `rain_mm`, max rain rate; running counters take the bucket's
    last values so rain increments recompute to the same totals;
  * solar and UV: mean plus max.

Each day is merged and its raw rows deleted in one transaction. A day is never
touched while the station's rollups are stale for it, because the raw detail
would be gone before it was summarised.
"""
import datetime as dt
import logging
import math

from django.db import transaction

from .ingest.store import interval_end
from .models import Observation, Station

log = logging.getLogger(__name__)

BUCKET_S = 900
_MEAN = ('dewpoint_c', 'pressure_abs_hpa', 'wind_speed_ms', 'temp_in_c', 'humidity_in')
_MEAN_MIN_MAX = {           # column -> (min column, max column)
    'temp_c': ('temp_min_c', 'temp_max_c'),
    'humidity': ('humidity_min', 'humidity_max'),
    'pressure_rel_hpa': ('pressure_min_hpa', 'pressure_max_hpa'),
}
_MEAN_MAX = {'solar_wm2': 'solar_max_wm2', 'uv_index': 'uv_max'}
_MAX = ('wind_gust_ms', 'rain_rate_mmh')
_LAST = ('rain_counter_mm', 'rain_daily_mm', 'rain_event_mm')


def _weighted_mean(rows, column):
    pairs = [(getattr(r, column), r.interval_s) for r in rows if getattr(r, column) is not None]
    weight = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / weight if weight else None


def _extreme(rows, column, own_column, pick):
    values = [getattr(r, own_column) if getattr(r, own_column) is not None else getattr(r, column) for r in rows]
    values = [v for v in values if v is not None]
    return pick(values) if values else None


def merge_rows(rows, timestamp):
    """Merge observations (oldest first) into one Observation ending at `timestamp`."""
    merged = Observation(
        station_id=rows[0].station_id, timestamp=timestamp, interval_s=BUCKET_S,
        sample_count=sum(r.sample_count for r in rows), source=Observation.SOURCE_DOWNSAMPLED,
        extra=dict(rows[-1].extra),
    )
    for column in _MEAN:
        setattr(merged, column, _weighted_mean(rows, column))
    for column, (min_col, max_col) in _MEAN_MIN_MAX.items():
        setattr(merged, column, _weighted_mean(rows, column))
        setattr(merged, min_col, _extreme(rows, column, min_col, min))
        setattr(merged, max_col, _extreme(rows, column, max_col, max))
    for column, max_col in _MEAN_MAX.items():
        setattr(merged, column, _weighted_mean(rows, column))
        setattr(merged, max_col, _extreme(rows, column, max_col, max))
    for column in _MAX:
        values = [getattr(r, column) for r in rows if getattr(r, column) is not None]
        setattr(merged, column, max(values) if values else None)
    for column in _LAST:
        setattr(merged, column, next((getattr(r, column) for r in reversed(rows) if getattr(r, column) is not None), None))
    rain = [r.rain_mm for r in rows if r.rain_mm is not None]
    merged.rain_mm = round(sum(rain), 3) if rain else None

    u = v = 0.0
    for r in rows:
        if r.wind_speed_ms and r.wind_dir_deg is not None:
            u += r.wind_speed_ms * math.sin(math.radians(r.wind_dir_deg)) * r.interval_s
            v += r.wind_speed_ms * math.cos(math.radians(r.wind_dir_deg)) * r.interval_s
    merged.wind_dir_deg = None if math.hypot(u, v) < 1e-9 else round(math.degrees(math.atan2(u, v)) % 360.0, 1)
    return merged


class StaleRollups(RuntimeError):
    pass


def downsample_day(station, day):
    """Merge one local day's sub-15-minute rows. Returns (rows before, rows after)."""
    tz = station.tzinfo
    start = dt.datetime.combine(day, dt.time(), tzinfo=tz).astimezone(dt.UTC)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=tz).astimezone(dt.UTC)
    with transaction.atomic():
        rows = list(Observation.objects.select_for_update()
                    .filter(station=station, timestamp__gt=start, timestamp__lte=end).order_by('timestamp'))
        if not any(r.interval_s < BUCKET_S for r in rows):
            return len(rows), len(rows)
        buckets = {}
        for r in rows:
            # A row covering (t − interval, t] lands in the 15-minute bucket that contains it.
            buckets.setdefault(interval_end(r.timestamp, BUCKET_S), []).append(r)
        merged = [merge_rows(group, ts) for ts, group in sorted(buckets.items())]
        Observation.objects.filter(pk__in=[r.pk for r in rows]).delete()
        Observation.objects.bulk_create(merged, batch_size=500)
    return len(rows), len(merged)


def downsample_station(station, retention_years, today=None, dry_run=False):
    """Downsample whole local days older than `retention_years`. Returns (days, before, after)."""
    station.refresh_from_db(fields=['rollup_dirty_from', 'timezone'])
    tz = station.tzinfo
    today = today or dt.datetime.now(tz).date()
    try:
        cutoff_day = today.replace(year=today.year - retention_years)
    except ValueError:      # Feb 29
        cutoff_day = today.replace(year=today.year - retention_years, day=28)
    cutoff = dt.datetime.combine(cutoff_day, dt.time(), tzinfo=tz).astimezone(dt.UTC)
    if station.rollup_dirty_from is not None and station.rollup_dirty_from <= cutoff:
        raise StaleRollups(f'{station}: rollups are stale from {station.rollup_dirty_from:%Y-%m-%d}; '
                           'run refresh_rollups first')

    oldest = (Observation.objects.filter(station=station, interval_s__lt=BUCKET_S, timestamp__lte=cutoff)
              .order_by('timestamp').values_list('timestamp', flat=True).first())
    if oldest is None:
        return 0, 0, 0
    day = (oldest - dt.timedelta(seconds=1)).astimezone(tz).date()
    days = before = after = 0
    while day < cutoff_day:
        if dry_run:
            start = dt.datetime.combine(day, dt.time(), tzinfo=tz).astimezone(dt.UTC)
            end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=tz).astimezone(dt.UTC)
            n = Observation.objects.filter(station=station, interval_s__lt=BUCKET_S, timestamp__gt=start,
                                           timestamp__lte=end).count()
            before, after = before + n, after + math.ceil(n / 3)
        else:
            b, a = downsample_day(station, day)
            before, after = before + b, after + a
        days += 1
        day += dt.timedelta(days=1)
    log.info('Downsampled %s: %d days, %d → %d rows', station, days, before, after)
    return days, before, after


__all__ = ['BUCKET_S', 'StaleRollups', 'downsample_day', 'downsample_station', 'merge_rows', 'Station']
