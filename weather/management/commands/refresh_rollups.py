import time

from django.core.management.base import BaseCommand

from weather.models import Observation, Station
from weather.rollups import refresh_station


class Command(BaseCommand):
    help = ('Recompute rain amounts and hourly/daily rollups for stations with new data. '
            'Cheap when nothing changed; run every 5 minutes.')

    def add_arguments(self, parser):
        parser.add_argument('--station', help='Slug of a single station (default: all).')
        parser.add_argument('--full', action='store_true', help='Rebuild the whole history, not just what changed.')

    def handle(self, *args, station=None, full=False, **options):
        stations = Station.objects.all()
        if station:
            stations = stations.filter(slug=station)
        for st in stations:
            if full:
                oldest = Observation.objects.filter(station=st).order_by('timestamp').values_list('timestamp', flat=True).first()
                if oldest is None:
                    continue
                Station.objects.filter(pk=st.pk).update(rollup_dirty_from=oldest)
            started = time.monotonic()
            result = refresh_station(st)
            if result and (options['verbosity'] > 1 or full or result[1] > 2):
                self.stdout.write(f'{st}: {result[0]} hours, {result[1]} days ({time.monotonic() - started:.1f}s)')
