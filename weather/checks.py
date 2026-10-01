"""System checks.

The time zone check is tagged ``database``, so Django runs it during ``migrate``
and on ``manage.py check --database default`` — not on every runserver reload.
"""
import datetime
from zoneinfo import ZoneInfo

from django.core.checks import Tags, Warning, register
from django.db import DatabaseError, connections

TZ_CHECK_ID = 'weather.W001'
_PROBE_ZONE = ZoneInfo('America/New_York')
_PROBE_INSTANT = datetime.datetime(2026, 1, 15, 3, 0, tzinfo=datetime.UTC)  # 2026-01-14 22:00 in New York


def probe_local_day_truncation(using='default'):
    """Truncate a known UTC instant to a New York calendar day via the ORM.

    Returns the truncated value, or None when the database cannot convert time
    zones. On MySQL that happens when the server's time zone tables were never
    loaded: CONVERT_TZ() returns NULL, and so does every TruncDay/TruncHour that
    passes a tzinfo — which is how every local-day rollup is computed.
    """
    from django.contrib.contenttypes.models import ContentType
    from django.db.models import DateTimeField, Value
    from django.db.models.functions import TruncDay

    return (
        ContentType.objects.using(using)
        .annotate(day=TruncDay(Value(_PROBE_INSTANT, output_field=DateTimeField()), tzinfo=_PROBE_ZONE))
        .values_list('day', flat=True)
        .first()
    )


@register(Tags.database)
def check_time_zone_support(app_configs=None, databases=None, **kwargs):
    errors = []
    for alias in databases or []:
        try:
            if not connections[alias].settings_dict.get('NAME'):
                continue
            result = probe_local_day_truncation(alias)
        except DatabaseError:
            # Tables not migrated yet (first `migrate`) or database unreachable;
            # Django reports connection problems itself.
            continue
        if result is None:
            from django.contrib.contenttypes.models import ContentType
            if not ContentType.objects.using(alias).exists():
                continue  # nothing to annotate yet; re-checked on the next migrate
            errors.append(Warning(
                'The database cannot convert time zones, so local-day rollups will be NULL.',
                hint=(
                    'On MySQL, load the time zone tables (needs the MySQL root account):\n'
                    '    mysql_tzinfo_to_sql /usr/share/zoneinfo | sudo mysql -u root mysql\n'
                    'then re-run: python manage.py check --database default\n'
                    'See README, "MySQL time zone tables".'
                ),
                id=TZ_CHECK_ID,
            ))
    return errors
