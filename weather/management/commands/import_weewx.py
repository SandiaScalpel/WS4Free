import datetime as dt

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from weather import weewx
from weather.ingest.parsers import Reading
from weather.ingest.store import mark_dirty, update_latest
from weather.models import Observation, Station
from weather.sensors import detect


def _date(value):
    return int(dt.datetime.fromisoformat(value).replace(tzinfo=dt.UTC).timestamp())


class Command(BaseCommand):
    help = ('Import history from a WeeWX archive database into a station. SOURCE is the path to a WeeWX '
            'SQLite file (e.g. /var/lib/weewx/weewx.sdb) or a MySQL URL (mysql://user:password@host/weewx). '
            'Readings WS4Free already has are kept; the archive only fills the gaps. Safe to re-run.')

    def add_arguments(self, parser):
        parser.add_argument('station', help='Station slug.')
        parser.add_argument('source', help='WeeWX SQLite file path, or mysql://user:password@host[:port]/database')
        parser.add_argument('--since', type=_date, help='Only records from this date (UTC, YYYY-MM-DD).')
        parser.add_argument('--until', type=_date, help='Only records up to this date (UTC, YYYY-MM-DD).')
        parser.add_argument('--dry-run', action='store_true', help='Report what would be imported and change nothing.')

    def handle(self, *args, station, source, since, until, dry_run, **options):
        try:
            st = Station.objects.get(slug=station)
        except Station.DoesNotExist:
            raise CommandError(f'No station with slug {station!r}.')
        try:
            reader = weewx.ArchiveReader(source)
            columns = reader.columns()
            count, first, last = reader.span(since, until)
        except Exception as exc:
            raise CommandError(f'Could not read a WeeWX archive from {source}: {exc}')
        mapped, extras, ignored = weewx.plan(columns)
        fmt = lambda t: dt.datetime.fromtimestamp(t, dt.UTC).strftime('%Y-%m-%d %H:%M') + 'Z'   # noqa: E731
        if not count:
            self.stdout.write('The archive has no records in that range.')
            return
        self.stdout.write(f'{count:,} records, {fmt(first)} to {fmt(last)}')
        self.stdout.write(f'Readings: {", ".join(mapped)}')
        if extras:
            self.stdout.write(f'Extra sensors: {", ".join(extras)}')
        if ignored:
            self.stdout.write(f'Not imported (no WS4Free equivalent yet): {", ".join(ignored)}')
        if dry_run:
            return

        converter = weewx.Converter(st.tzinfo)
        added = skipped = 0
        first_added = last_reading = None
        seen_extra = {}
        for rows in reader.pages(since, until):
            lo = dt.datetime.fromtimestamp(rows[0]['dateTime'], dt.UTC) - dt.timedelta(hours=1)
            hi = dt.datetime.fromtimestamp(rows[-1]['dateTime'], dt.UTC) + dt.timedelta(hours=1)
            existing = sorted((ts, ts - dt.timedelta(seconds=iv)) for ts, iv in Observation.objects.filter(
                station=st, timestamp__gt=lo, timestamp__lte=hi + dt.timedelta(hours=1)).values_list('timestamp', 'interval_s'))
            new = []
            for row in rows:
                when, interval_s, values, extra = converter.convert(row)
                if weewx.overlaps_existing(existing, when, interval_s):
                    skipped += 1
                    continue
                new.append(Observation(station=st, timestamp=when, interval_s=interval_s,
                                       source=Observation.SOURCE_WEEWX_IMPORT, extra=extra, **values))
                seen_extra.update(extra)
                last_reading = Reading(when, values, extra)
            with transaction.atomic():
                Observation.objects.bulk_create(new, batch_size=1000, ignore_conflicts=True)
            if new:
                added += len(new)
                first_added = first_added or new[0].timestamp
                from weather.calibration import apply_to_new_rows as calibrate_new_rows
                from weather.quality import apply_to_new_rows
                apply_to_new_rows(st, new[0].timestamp - dt.timedelta(seconds=1), new[-1].timestamp)
                calibrate_new_rows(st, new[0].timestamp - dt.timedelta(seconds=1), new[-1].timestamp)
            self.stdout.write(f'  … {fmt(rows[-1]["dateTime"])}: {added:,} added, {skipped:,} already covered')
        reader.close()

        if first_added:
            if seen_extra:
                detect(st, seen_extra)
            update_latest(st, last_reading, Observation.SOURCE_WEEWX_IMPORT)
            mark_dirty(st.pk, first_added)
        self.stdout.write(self.style.SUCCESS(
            f'Imported {added:,} records into {st} ({skipped:,} skipped: WS4Free already had readings for those times). '
            'Summaries are rebuilt by the next refresh_rollups run.'))
