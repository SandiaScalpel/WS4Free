from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from weather.downsample import StaleRollups, downsample_station
from weather.models import Station


class Command(BaseCommand):
    help = ('Merge raw readings older than RAW_RETENTION_YEARS into 15-minute rows to save space. '
            'Does nothing while RAW_RETENTION_YEARS is 0 (the default).')

    def add_arguments(self, parser):
        parser.add_argument('--station', help='Slug of a single station (default: all).')
        parser.add_argument('--dry-run', action='store_true', help='Report what would be merged; change nothing.')

    def handle(self, *args, station=None, dry_run=False, **options):
        years = settings.RAW_RETENTION_YEARS
        if years <= 0:
            self.stdout.write('Downsampling is off (RAW_RETENTION_YEARS=0).')
            return
        stations = Station.objects.all()
        if station:
            stations = stations.filter(slug=station)
        failed = False
        for st in stations:
            try:
                days, before, after = downsample_station(st, years, dry_run=dry_run)
            except StaleRollups as exc:
                self.stderr.write(str(exc))
                failed = True
                continue
            if days or options['verbosity'] > 1:
                verb = 'Would merge' if dry_run else 'Merged'
                self.stdout.write(f'{st}: {verb} {days} day(s), {before} → {after} rows')
        if failed:
            raise CommandError('Some stations were skipped; run refresh_rollups and try again.')
