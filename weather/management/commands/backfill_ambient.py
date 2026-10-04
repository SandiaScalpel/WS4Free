import datetime as dt
import time

from django.core.management.base import BaseCommand, CommandError

from weather.ambient import AmbientAPIError, AmbientClient
from weather.ingest.rain import compute_rain_increments
from weather.ingest.store import record_archive
from weather.management.commands.poll_ambient import _parse_all
from weather.models import Observation, Station


def _date(value):
    try:
        return dt.datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=dt.UTC)
    except ValueError:
        raise CommandError(f'Dates are YYYY-MM-DD, got {value!r}')


class Command(BaseCommand):
    help = ('Import a station\'s history from the Ambient API, paging backwards one day (288 records) '
            'per request at 1 request/second. Safe to re-run: existing rows are never changed.')

    def add_arguments(self, parser):
        parser.add_argument('station', help='Station slug.')
        parser.add_argument('--since', type=_date, help='Oldest date to fetch (UTC, YYYY-MM-DD). Default: as far back as the API goes.')
        parser.add_argument('--until', type=_date, help='Start paging back from this date (UTC). Default: now.')
        parser.add_argument('--max-empty-days', type=int, default=120,
                            help='Without --since, stop after this many consecutive days with no data '
                                 '(the API returns nothing for days the station was offline). Default 120.')
        parser.add_argument('--resume', action='store_true',
                            help='Start from the oldest observation already stored (continue an interrupted run).')

    def handle(self, *args, station, since, until, resume, max_empty_days, **options):
        try:
            st = Station.objects.get(slug=station)
        except Station.DoesNotExist:
            raise CommandError(f'No station with slug {station!r}.')
        if not st.mac_address:
            raise CommandError(f'{st} has no MAC address; the Ambient Weather API identifies stations by MAC.')
        if resume:
            oldest = Observation.objects.filter(station=st).order_by('timestamp').values_list('timestamp', flat=True).first()
            if oldest:
                until = oldest
                self.stdout.write(f'Resuming from {oldest:%Y-%m-%d %H:%M}Z')
        try:
            client = AmbientClient()
        except AmbientAPIError as exc:
            raise CommandError(str(exc))

        started = time.monotonic()
        pages = added = 0
        oldest_written = None
        try:
            for page in client.iter_history(st.mac_address, start=since, end=until, max_empty_days=max_empty_days):
                readings = _parse_all(page)
                added += record_archive(st, readings, Observation.SOURCE_BACKFILL)
                pages += 1
                reached = min(r.timestamp for r in readings) if readings else None
                if reached and (oldest_written is None or reached < oldest_written):
                    oldest_written = reached
                if pages % 30 == 0 or options['verbosity'] > 1:
                    self.stdout.write(f'  {pages} days fetched, back to {reached:%Y-%m-%d}, {added} new rows '
                                      f'({time.monotonic() - started:.0f}s)')
        except AmbientAPIError as exc:
            self.stderr.write(self.style.ERROR(f'Stopped: {exc}. Re-run with --resume to continue.'))
        except KeyboardInterrupt:
            self.stderr.write('Interrupted. Re-run with --resume to continue.')
        finally:
            if oldest_written is not None:
                self.stdout.write('Computing rain amounts…')
                compute_rain_increments(st, since=oldest_written)
        self.stdout.write(self.style.SUCCESS(
            f'Done: {pages} requests, {added} new observations'
            + (f', oldest {oldest_written:%Y-%m-%d %H:%M}Z' if oldest_written else '')))
