"""Per-row rain amounts from the console's running counters.

Consoles never report "rain in the last 5 minutes"; they report running totals
that reset — `dailyrainin` at local midnight, `eventrainin` after a dry spell,
`totalrainin` only when the console is reset. Summing any of those across rows
counts the same rain many times. This pass turns them into `rain_mm`, the rain
that fell since the previous row, which rollups and downsampling can safely sum.

Order of preference for each row (prev → cur):
  1. within one console day, the `dailyrainin` delta — the 0.01 in counter the
     console and ambientweather.net total by, so daily/monthly/yearly sums match
     what the owner sees there (`totalrainin` has finer resolution and drifts
     from it by 1–2 % a day);
  2. across the midnight reset (or when the daily counter is missing or went
     backwards): the `eventrainin` delta, else the `totalrainin` delta — this keeps
     the rain of the last interval before midnight, which the daily counter
     drops when it resets on the 00:00 record. Not used across gaps over a day:
     after a long outage the rain is real but its day is unknown;
  3. otherwise, on a new console day, its daily total, i.e. the rain since
     midnight; within the same day with no previous counter, unknown (NULL).
A delta faster than MAX_RATE_MMH over the gap is a counter glitch → NULL.
"""
import datetime as dt
import logging

from django.db.models import Q

from ..models import Observation

log = logging.getLogger(__name__)

MAX_RATE_MMH = 400.0           # beyond any recorded 5-minute rainfall intensity
_SLOP_MM = 0.01                # float noise from inch→mm conversion
_MAX_COUNTER_GAP = dt.timedelta(days=1)
_FIELDS = ('pk', 'timestamp', 'rain_counter_mm', 'rain_event_mm', 'rain_daily_mm', 'rain_mm')


def _delta(before, after):
    """after − before for a running counter, or None if either is missing or it went backwards."""
    if before is None or after is None or after < before - _SLOP_MM:
        return None
    return after - before


def increment(prev, cur, tzinfo):
    """Rain (mm) that fell between two consecutive observations, or None if unknown."""
    if prev is None:
        return None
    gap = cur.timestamp - prev.timestamp
    same_day = prev.timestamp.astimezone(tzinfo).date() == cur.timestamp.astimezone(tzinfo).date()

    delta = _delta(prev.rain_daily_mm, cur.rain_daily_mm) if same_day else None
    if delta is None and gap <= _MAX_COUNTER_GAP:
        delta = _delta(prev.rain_event_mm, cur.rain_event_mm)
        if delta is None:
            delta = _delta(prev.rain_counter_mm, cur.rain_counter_mm)
    if delta is None:
        if same_day and prev.rain_daily_mm is None:
            # Same console day but the previous row has no counter (excluded or not
            # reported): "since midnight" would count this morning's rain again.
            return None
        delta = cur.rain_daily_mm   # across the reset: rain since the console's daily reset (or None)

    if delta is None:
        return None
    delta = max(delta, 0.0)
    hours = max(gap.total_seconds() / 3600.0, 5 / 60)
    if delta > MAX_RATE_MMH * hours:
        log.warning('Ignoring implausible rain delta %.1f mm over %s at %s', delta, gap, cur.timestamp)
        return None
    return round(delta, 3)


RAIN_COUNTERS = ('rain_counter_mm', 'rain_daily_mm', 'rain_event_mm')


def _has_counter(obs):
    return any(getattr(obs, f) is not None for f in RAIN_COUNTERS)


def compute_rain_increments(station, since=None, until=None, batch_size=2000):
    """(Re)compute rain_mm for the station's rows in [since, until]. Returns the
    number of rows changed.

    Each row is measured against the last row that HAD counters, not merely the
    previous row: Ambient's archive sometimes alternates records with and without
    rain counters, and the rain across the blank records must be counted exactly
    once. A row whose counters a data-quality exclusion set aside breaks the chain
    instead — the excluded rain must not come back through the next delta.
    """
    from ..models import ExcludedValue

    qs = Observation.objects.filter(station=station).only(*_FIELDS).order_by('timestamp')
    if until is not None:
        qs = qs.filter(timestamp__lte=until)
    excluded = ExcludedValue.objects.filter(observation__station=station, field__in=RAIN_COUNTERS)
    if since is not None:
        excluded = excluded.filter(observation__timestamp__gte=since)
    excluded_ids = set(excluded.values_list('observation_id', flat=True))

    prev = None
    if since is not None:
        before = qs.filter(timestamp__lt=since).last()
        if before is not None and not _has_counter(before):
            if ExcludedValue.objects.filter(observation=before, field__in=RAIN_COUNTERS).exists():
                before = None
            else:
                counted = Q(rain_counter_mm__isnull=False) | Q(rain_daily_mm__isnull=False) | Q(rain_event_mm__isnull=False)
                before = qs.filter(timestamp__lt=since).filter(counted).last()
        prev = before

    # Keyset pagination rather than .iterator(): MySQL's driver buffers a whole
    # result set client-side, and a full backfill is ~500k rows.
    tzinfo = station.tzinfo
    changed = 0
    cursor_ts, inclusive = since, True
    while True:
        page = qs
        if cursor_ts is not None:
            page = page.filter(timestamp__gte=cursor_ts) if inclusive else page.filter(timestamp__gt=cursor_ts)
        page = list(page[:batch_size])
        if not page:
            break
        pending = []
        for obs in page:
            if obs.pk in excluded_ids:
                value, prev = None, None
            else:
                value = increment(prev, obs, tzinfo)
                if _has_counter(obs):
                    prev = obs
            if value != obs.rain_mm:
                obs.rain_mm = value
                pending.append(obs)
        if pending:
            Observation.objects.bulk_update(pending, ['rain_mm'])
            changed += len(pending)
        cursor_ts, inclusive = page[-1].timestamp, False
    return changed
