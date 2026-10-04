"""Data-quality exclusions: set bad readings aside without losing them.

Applying an exclusion copies each affected value into ExcludedValue and sets the
Observation column to NULL. Every reader — rollups, charts, almanac, reports,
the dashboard sparklines — already treats NULL as "not measured", so nothing
downstream needs to know exclusions exist. Removing an exclusion copies the
values back (unless another exclusion still covers them) and marks the rollups
dirty, so the summaries return to exactly what they were.

Ongoing exclusions (no end) also apply to readings that arrive later: the ingest
store calls `apply_to_new_rows` after every write.

Extra sensors live in the `extra` JSON column rather than their own columns. An
exclusion field 'x:<key>' sets that key aside the same way: the value goes to
ExcludedValue (field 'x:<key>') and the key is removed from `extra`.

Limitation: values excluded inside a period that is later downsampled cannot be
restored, because the rows they belonged to are merged away.
"""
import datetime as dt
import subprocess
import sys

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from .ingest.store import mark_dirty
from .models import EXCLUSION_GROUPS, EXTRA_PREFIX, DataExclusion, ExcludedValue, Observation

FAR_FUTURE = dt.datetime(9999, 1, 1, tzinfo=dt.UTC)
BATCH = 2000


def _window(station, start, end):
    return Observation.objects.filter(station=station, timestamp__gt=start, timestamp__lte=end or FAR_FUTURE)


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _set_aside_extra(rows, field, touched):
    key = field[len(EXTRA_PREFIX):]
    qs = rows.filter(extra__has_key=key)
    last_pk = 0
    while True:
        chunk = list(qs.filter(pk__gt=last_pk).order_by('pk').only('pk', 'extra')[:BATCH])
        if not chunk:
            break
        last_pk = chunk[-1].pk
        backups, changed = [], []
        for obs in chunk:
            value = _number(obs.extra.get(key))
            if value is not None:
                backups.append(ExcludedValue(observation_id=obs.pk, field=field, value=value))
            obs.extra = {k: v for k, v in obs.extra.items() if k != key}
            changed.append(obs)
            touched.add(obs.pk)
        ExcludedValue.objects.bulk_create(backups, ignore_conflicts=True)
        Observation.objects.bulk_update(changed, ['extra'])


def _set_aside(rows, fields):
    """Back up and null `fields` on `rows`. Returns the number of rows touched."""
    touched = set()
    for field in fields:
        if field.startswith(EXTRA_PREFIX):
            _set_aside_extra(rows, field, touched)
            continue
        qs = rows.exclude(**{f'{field}__isnull': True})
        last_pk = 0
        while True:
            # Keyset pagination: MySQL's driver buffers whole result sets.
            chunk = list(qs.filter(pk__gt=last_pk).order_by('pk').values_list('pk', field)[:BATCH])
            if not chunk:
                break
            ExcludedValue.objects.bulk_create(
                [ExcludedValue(observation_id=pk, field=field, value=value) for pk, value in chunk],
                ignore_conflicts=True)       # already held by an overlapping exclusion: keep the original
            touched.update(pk for pk, _ in chunk)
            last_pk = chunk[-1][0]
        qs.update(**{field: None})
    return len(touched)


def apply_exclusion(exclusion):
    with transaction.atomic():
        rows = _window(exclusion.station, exclusion.start, exclusion.end)
        _set_aside(rows, exclusion.fields())
        exclusion.readings = rows.count()
        exclusion.status = 'applied'
        exclusion.save(update_fields=['readings', 'status'])
    mark_dirty(exclusion.station_id, exclusion.start)
    return exclusion.readings


def _covering(exclusions, field):
    """Q matching backups whose observation lies inside any of `exclusions` that cover `field`."""
    q = Q()
    for other in exclusions:
        if field in other.fields():
            q |= Q(observation__timestamp__gt=other.start, observation__timestamp__lte=other.end or FAR_FUTURE)
    return q


def remove_exclusion(exclusion):
    """Restore the values this exclusion set aside, then delete it."""
    station = exclusion.station
    others = list(DataExclusion.objects.filter(station=station).exclude(pk=exclusion.pk))
    restored = 0
    with transaction.atomic():
        for field in exclusion.fields():
            backups = ExcludedValue.objects.filter(
                observation__station=station, field=field,
                observation__timestamp__gt=exclusion.start,
                observation__timestamp__lte=exclusion.end or FAR_FUTURE)
            still_covered = _covering(others, field)
            if still_covered:
                backups = backups.exclude(still_covered)
            last_pk = 0
            while True:
                chunk = list(backups.filter(pk__gt=last_pk).order_by('pk').values_list('pk', 'observation_id', 'value')[:BATCH])
                if not chunk:
                    break
                if field.startswith(EXTRA_PREFIX):
                    key = field[len(EXTRA_PREFIX):]
                    values = {obs_id: value for _, obs_id, value in chunk}
                    rows = list(Observation.objects.filter(pk__in=values).only('pk', 'extra'))
                    for obs in rows:
                        obs.extra = {**obs.extra, key: values[obs.pk]}
                    Observation.objects.bulk_update(rows, ['extra'])
                else:
                    Observation.objects.bulk_update(
                        [Observation(pk=obs_id, **{field: value}) for _, obs_id, value in chunk], [field])
                ExcludedValue.objects.filter(pk__in=[pk for pk, _, _ in chunk]).delete()
                restored += len(chunk)
                last_pk = chunk[-1][0]
        start, end = exclusion.start, exclusion.end
        exclusion.delete()
        # Restored readings are raw: correct them again if a calibration covers them.
        from .calibration import reapply_window
        reapply_window(station, start, end)
    mark_dirty(station.pk, start)
    return restored


def apply_to_new_rows(station, start, end):
    """Called by the ingest store after writing rows in (start, end]: any exclusion
    overlapping that span (usually an ongoing one) applies to them too."""
    exclusions = DataExclusion.objects.filter(station=station, start__lt=end, status__in=('pending', 'applied')).filter(
        Q(end__isnull=True) | Q(end__gt=start))
    for exclusion in exclusions:
        lo = max(start, exclusion.start)
        hi = min(end, exclusion.end) if exclusion.end else end
        _set_aside(_window(station, lo, hi), exclusion.fields())


def launch(exclusion, action):
    """Run apply/remove in a detached process: a long window can take longer than a
    web request may (Cloudflare gives up at ~100 s). A process, not a thread, so a
    gunicorn worker recycle or graceful reload can't kill it halfway."""
    log = open(settings.BASE_DIR / 'exclusions.log', 'ab')
    subprocess.Popen([sys.executable, str(settings.BASE_DIR / 'manage.py'), 'run_exclusion', str(exclusion.pk), action],
                     cwd=settings.BASE_DIR, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def excluded_fields_at(station, when):
    """Observation columns (and 'x:<key>' extra sensors) excluded at instant `when`, for the live dashboard."""
    fields = set()
    for exclusion in DataExclusion.objects.filter(station=station, start__lt=when, status__in=('pending', 'applied')).filter(
            Q(end__isnull=True) | Q(end__gte=when)):
        fields.update(exclusion.fields())
    return fields


__all__ = ['EXCLUSION_GROUPS', 'apply_exclusion', 'apply_to_new_rows', 'excluded_fields_at', 'remove_exclusion']
