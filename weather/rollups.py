"""Hourly and daily rollups, recomputed from the station's dirty watermark.

Every write path moves `Station.rollup_dirty_from` back to the earliest
observation it touched (weather.ingest.store.mark_dirty). `refresh_station`
claims that watermark, recomputes rain increments from it, then rebuilds every
hourly and daily rollup whose period could contain an observation at or after
it. Everything is grouped in the database through the ORM (no raw SQL), so it
runs on MySQL, PostgreSQL and SQLite alike.

Grouping rules that follow from the data model:
  * Observations are stamped with the END of their interval, so a row belongs
    to the period containing `timestamp − 1 s`: the 00:00 row is yesterday's
    last 5 minutes.
  * Hours are UTC hours (local hours are the same instant everywhere except in
    half-hour zones, and UTC avoids the duplicated hour when DST ends).
    Days are local calendar days in the station's time zone, so DST days are
    23 or 25 hours long and their coverage is measured against that.
  * Averages are weighted by `interval_s`; min/max read Coalesce(x_min, x) so
    downsampled rows contribute their true extremes.
"""
import datetime as dt
import logging
import math

from django.db import transaction
from django.db.models import (
    Count, DateTimeField, DurationField, ExpressionWrapper, F, FloatField, Max, Min, Q, Sum, Value,
)
from django.db.models.functions import Coalesce, Cos, Radians, Sin, TruncDay, TruncHour

from .ingest.rain import compute_rain_increments
from .models import DailyRollup, HourlyRollup, Observation, Station

log = logging.getLogger(__name__)

ONE_SECOND = Value(dt.timedelta(seconds=1), output_field=DurationField())
COVERING = ExpressionWrapper(F('timestamp') - ONE_SECOND, output_field=DateTimeField())
CHUNK = dt.timedelta(days=31)

# rollup field prefix -> (observation column, min column or None, max column or None)
_AVG_MIN_MAX = {
    'temp': ('temp_c', 'temp_min_c', 'temp_max_c'),
    'humidity': ('humidity', 'humidity_min', 'humidity_max'),
    'dewpoint': ('dewpoint_c', None, None),
    'pressure': ('pressure_rel_hpa', 'pressure_min_hpa', 'pressure_max_hpa'),
    'temp_in': ('temp_in_c', None, None),
}


def _weighted(column):
    """(numerator, weight) aggregates for an interval-weighted mean of `column`."""
    return (
        Sum(F(column) * F('interval_s'), output_field=FloatField()),
        Sum('interval_s', filter=Q(**{f'{column}__isnull': False})),
    )


def _aggregates():
    # Every alias starts with '_': an alias equal to an Observation column (rain_mm,
    # solar_max_wm2…) shadows that column for the other expressions in the query.
    aggs = {
        '_sample_count': Count('id'),
        '_covered_s': Sum('interval_s'),
        '_rain_sum': Sum('rain_mm'),
        '_rain_den': Sum('interval_s', filter=Q(rain_mm__isnull=False)),
        '_rain_rate_max_mmh': Max('rain_rate_mmh'),
        '_wind_gust_max_ms': Max('wind_gust_ms'),
        '_wind_speed_max_ms': Max('wind_speed_ms'),
        '_solar_max_wm2': Max(Coalesce('solar_max_wm2', 'solar_wm2')),
        '_uv_max': Max(Coalesce('uv_max', 'uv_index')),
        # Speed-weighted wind vector, finished in Python (atan2 of the sums).
        '_wind_u': Sum(F('wind_speed_ms') * Sin(Radians('wind_dir_deg')) * F('interval_s'), output_field=FloatField()),
        '_wind_v': Sum(F('wind_speed_ms') * Cos(Radians('wind_dir_deg')) * F('interval_s'), output_field=FloatField()),
    }
    for column, key in (('temp_c', 'temp'), ('humidity', 'humidity'), ('dewpoint_c', 'dewpoint'),
                        ('pressure_rel_hpa', 'pressure'), ('temp_in_c', 'temp_in'), ('humidity_in', 'humidity_in'),
                        ('wind_speed_ms', 'wind_speed'), ('solar_wm2', 'solar'), ('uv_index', 'uv')):
        aggs[f'_{key}_num'], aggs[f'_{key}_den'] = _weighted(column)
    for key, (column, min_col, max_col) in _AVG_MIN_MAX.items():
        aggs[f'_{key}_min'] = Min(Coalesce(min_col, column)) if min_col else Min(column)
        aggs[f'_{key}_max'] = Max(Coalesce(max_col, column)) if max_col else Max(column)
    return aggs


def _mean(row, key):
    den = row[f'_{key}_den']
    return row[f'_{key}_num'] / den if den else None


def _finish(row, period_seconds):
    """Turn one aggregated group into RollupFields kwargs."""
    values = {
        'sample_count': row['_sample_count'],
        'covered_s': row['_covered_s'] or 0,
        'coverage': min((row['_covered_s'] or 0) / period_seconds, 1.0),
        'temp_coverage': min((row['_temp_den'] or 0) / period_seconds, 1.0),
        'rain_coverage': min((row['_rain_den'] or 0) / period_seconds, 1.0),
        'rain_mm': None if row['_rain_sum'] is None else round(row['_rain_sum'], 3),
        'rain_rate_max_mmh': row['_rain_rate_max_mmh'],
        'wind_gust_max_ms': row['_wind_gust_max_ms'],
        'wind_speed_max_ms': row['_wind_speed_max_ms'],
        'wind_speed_avg_ms': _mean(row, 'wind_speed'),
        'solar_avg_wm2': _mean(row, 'solar'),
        'solar_max_wm2': row['_solar_max_wm2'],
        'uv_avg': _mean(row, 'uv'),
        'uv_max': row['_uv_max'],
        'humidity_in_avg': _mean(row, 'humidity_in'),
    }
    for key, (prefix_avg, prefix_min, prefix_max) in {
        'temp': ('temp_avg_c', 'temp_min_c', 'temp_max_c'),
        'humidity': ('humidity_avg', 'humidity_min', 'humidity_max'),
        'dewpoint': ('dewpoint_avg_c', 'dewpoint_min_c', 'dewpoint_max_c'),
        'pressure': ('pressure_avg_hpa', 'pressure_min_hpa', 'pressure_max_hpa'),
        'temp_in': ('temp_in_avg_c', 'temp_in_min_c', 'temp_in_max_c'),
    }.items():
        values[prefix_avg] = _mean(row, key)
        values[prefix_min] = row[f'_{key}_min']
        values[prefix_max] = row[f'_{key}_max']
    u, v = row['_wind_u'], row['_wind_v']
    if u is None or v is None or math.hypot(u, v) < 1e-9:
        values['wind_dir_avg_deg'] = None          # calm (or no direction data)
    else:
        values['wind_dir_avg_deg'] = round(math.degrees(math.atan2(u, v)) % 360.0, 1)
    return values


def _grouped(station, period_expr, start, end):
    """Aggregate observations in (start, end] grouped by `period_expr`."""
    return (
        Observation.objects.filter(station=station, timestamp__gt=start, timestamp__lte=end)
        .annotate(period=period_expr)
        .values('period')
        .annotate(**_aggregates())
        .order_by('period')
    )


def rebuild_hourly(station, since, until):
    """Rebuild hourly rollups for hours starting in [floor_hour(since), until)."""
    start = since.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)
    written = 0
    while start < until:
        end = min(start + CHUNK, until)
        rows = [
            HourlyRollup(station=station, period_start=row['period'].astimezone(dt.UTC), **_finish(row, 3600))
            for row in _grouped(station, TruncHour(COVERING, tzinfo=dt.UTC), start, end)
        ]
        with transaction.atomic():
            HourlyRollup.objects.filter(station=station, period_start__gte=start, period_start__lt=end).delete()
            HourlyRollup.objects.bulk_create(rows, batch_size=1000)
        written += len(rows)
        start = end
    return written


def _local_midnight(day, tzinfo):
    return dt.datetime.combine(day, dt.time(), tzinfo=tzinfo)


def rebuild_daily(station, since_date, until):
    """Rebuild daily rollups for local dates from `since_date` up to `until`."""
    tz = station.tzinfo
    written = 0
    day = since_date
    last_day = (until - dt.timedelta(seconds=1)).astimezone(tz).date()
    while day <= last_day:
        chunk_end = min(day + dt.timedelta(days=31), last_day + dt.timedelta(days=1))
        start, end = _local_midnight(day, tz), _local_midnight(chunk_end, tz)
        rows = []
        for row in _grouped(station, TruncDay(COVERING, tzinfo=tz), start, end):
            date = row['period'].astimezone(tz).date()
            next_day = date + dt.timedelta(days=1)
            # Convert first: subtracting two datetimes with the same tzinfo is wall-clock
            # arithmetic in Python and would make every DST day 24 h long.
            length = (_local_midnight(next_day, tz).astimezone(dt.UTC)
                      - _local_midnight(date, tz).astimezone(dt.UTC)).total_seconds()
            rows.append(DailyRollup(station=station, date=date, **_finish(row, length)))
        with transaction.atomic():
            DailyRollup.objects.filter(station=station, date__gte=day, date__lt=chunk_end).delete()
            DailyRollup.objects.bulk_create(rows, batch_size=1000)
        written += len(rows)
        day = chunk_end
    return written


def refresh_station(station, now=None):
    """Bring a station's rollups up to date. Returns (hours, days) rewritten, or None
    if nothing was dirty.

    The watermark is *claimed* before the work starts (conditional UPDATE to NULL):
    anything written while the rebuild runs sets it again and is picked up next
    time. If the rebuild fails the watermark is restored.
    """
    station.refresh_from_db(fields=['rollup_dirty_from', 'timezone'])
    dirty = station.rollup_dirty_from
    if dirty is None:
        return None
    if not Station.objects.filter(pk=station.pk, rollup_dirty_from=dirty).update(rollup_dirty_from=None):
        return None   # another run claimed it
    now = now or dt.datetime.now(dt.UTC)
    until = now + dt.timedelta(hours=1)
    try:
        compute_rain_increments(station, since=dirty)
        covering = dirty - dt.timedelta(seconds=1)
        hours = rebuild_hourly(station, covering, until)
        days = rebuild_daily(station, covering.astimezone(station.tzinfo).date(), until)
    except Exception:
        from .ingest.store import mark_dirty
        mark_dirty(station.pk, dirty)
        raise
    log.info('Rollups for %s from %s: %d hours, %d days', station, dirty, hours, days)
    return hours, days
