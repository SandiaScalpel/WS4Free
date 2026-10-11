"""Calibrations used to recalculate the dew point from the corrected temperature and
the raw humidity. They now keep the dew point and recalculate the humidity instead
(see weather.calibration._correct). Bring readings already corrected into line:
put their dew point back and correct their humidity."""
from django.db import migrations
from django.db.models import Min, Q

from weather import units

BATCH = 2000
HUMIDITY_FIELDS = ('humidity', 'humidity_min', 'humidity_max')


def forwards(apps, schema_editor):
    Observation = apps.get_model('weather', 'Observation')
    CalibratedValue = apps.get_model('weather', 'CalibratedValue')
    ExcludedValue = apps.get_model('weather', 'ExcludedValue')
    Station = apps.get_model('weather', 'Station')

    dirty = {}
    for row in (CalibratedValue.objects.filter(field='dewpoint_c').values('observation__station_id')
                .annotate(first=Min('observation__timestamp'))):
        dirty[row['observation__station_id']] = row['first']

    while True:
        chunk = list(CalibratedValue.objects.filter(field='dewpoint_c').order_by('pk')
                     .values_list('pk', 'observation_id', 'calibration_id', 'value')[:BATCH])
        if not chunk:
            break
        obs_ids = [c[1] for c in chunk]
        raw_temp = dict(CalibratedValue.objects.filter(observation_id__in=obs_ids, field='temp_c')
                        .values_list('observation_id', 'value'))
        corrected = set(CalibratedValue.objects.filter(observation_id__in=obs_ids, field__in=HUMIDITY_FIELDS)
                        .values_list('observation_id', 'field'))
        excluded = set(ExcludedValue.objects.filter(observation_id__in=obs_ids, field='dewpoint_c')
                       .values_list('observation_id', flat=True))
        rows = {o.pk: o for o in Observation.objects.filter(pk__in=obs_ids)
                .only('pk', 'temp_c', 'dewpoint_c', *HUMIDITY_FIELDS)}
        backups = []
        for _, obs_id, calibration_id, dewpoint in chunk:
            obs = rows[obs_id]
            if obs_id in excluded:          # set aside since: keep it excluded, with the original value
                ExcludedValue.objects.filter(observation_id=obs_id, field='dewpoint_c').update(value=dewpoint)
            else:
                obs.dewpoint_c = dewpoint
            if obs.temp_c is None or obs_id not in raw_temp:
                continue
            for f in HUMIDITY_FIELDS:
                value = getattr(obs, f)
                if value is None or (obs_id, f) in corrected:
                    continue
                backups.append(CalibratedValue(observation_id=obs_id, calibration_id=calibration_id, field=f, value=value))
                setattr(obs, f, units.humidity_at(value, raw_temp[obs_id], obs.temp_c))
        Observation.objects.bulk_update(list(rows.values()), ['dewpoint_c', *HUMIDITY_FIELDS])
        CalibratedValue.objects.bulk_create(backups, ignore_conflicts=True)
        CalibratedValue.objects.filter(pk__in=[c[0] for c in chunk]).delete()

    for station_id, since in dirty.items():     # rollups summarise humidity and dew point
        Station.objects.filter(pk=station_id).filter(
            Q(rollup_dirty_from__isnull=True) | Q(rollup_dirty_from__gt=since)).update(rollup_dirty_from=since)


class Migration(migrations.Migration):

    dependencies = [
        ('weather', '0020_calibration_neighbours_mode'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
