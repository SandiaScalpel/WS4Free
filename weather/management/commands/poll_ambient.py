import logging

from django.core.management.base import BaseCommand, CommandError

from weather.ambient import AmbientAPIError, AmbientClient
from weather.ingest.parsers import ParseError, parse_api_record
from weather.ingest.rain import compute_rain_increments
from weather.ingest.store import record_archive, update_latest
from weather.models import Observation, Station

log = logging.getLogger('weather.poll')


def _parse_all(records):
    readings = []
    for record in records:
        try:
            readings.append(parse_api_record(record))
        except ParseError as exc:
            log.warning('Skipping API record: %s', exc)
    return readings


class Command(BaseCommand):
    help = ('Fetch the last 24 h of archive records from the Ambient API for each station with '
            'polling enabled, filling any gaps the console\'s pushes left. Run every 5 minutes.')

    def add_arguments(self, parser):
        parser.add_argument('--station', help='Slug of a single station (default: all with polling enabled).')

    def handle(self, *args, station=None, **options):
        stations = Station.objects.filter(ambient_api_enabled=True, source=Station.SOURCE_AMBIENT, mac_address__isnull=False)
        if station:
            stations = stations.filter(slug=station)
        stations = list(stations)
        if not stations:
            return
        try:
            client = AmbientClient()
            devices = {d.get('macAddress', '').upper(): d for d in client.devices()}
        except AmbientAPIError as exc:
            raise CommandError(str(exc))

        for st in stations:
            device = devices.get(st.mac_address)
            if device is None:
                log.warning('Station %s (%s) is not on this Ambient account', st, st.mac_address)
                continue
            # lastData is the console's newest reading — fresher than the archive.
            if device.get('lastData'):
                try:
                    update_latest(st, parse_api_record(device['lastData']), Observation.SOURCE_API)
                except ParseError as exc:
                    log.warning('Station %s: unusable lastData: %s', st, exc)
            try:
                readings = _parse_all(client.device_data(st.mac_address))
            except AmbientAPIError as exc:
                log.error('Station %s: %s', st, exc)
                continue
            added = record_archive(st, readings, Observation.SOURCE_API)
            if added:
                compute_rain_increments(st, since=min(r.timestamp for r in readings))
            if options['verbosity'] > 1 or added:
                self.stdout.write(f'{st}: {added} new record(s)')
