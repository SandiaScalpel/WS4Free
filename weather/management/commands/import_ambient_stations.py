from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from weather.ambient import AmbientAPIError, AmbientClient
from weather.models import Station, normalize_mac


class Command(BaseCommand):
    help = 'Create a Station for each device on your ambientweather.net account (skips ones that exist).'

    def add_arguments(self, parser):
        parser.add_argument('--owner', required=True, help='Username that will own the new stations.')
        parser.add_argument('--public', action='store_true', help='Make the new stations publicly viewable.')

    def handle(self, *args, owner, public, **options):
        try:
            user = get_user_model().objects.get(username=owner)
        except get_user_model().DoesNotExist:
            raise CommandError(f'No user named {owner!r}.')
        try:
            devices = AmbientClient().devices()
        except AmbientAPIError as exc:
            raise CommandError(str(exc))

        for device in devices:
            mac = normalize_mac(device.get('macAddress', ''))
            info = device.get('info') or {}
            last = device.get('lastData') or {}
            if not mac:
                self.stderr.write(f'Skipping device without a MAC: {info.get("name")!r}')
                continue
            existing = Station.objects.filter(mac_address=mac).first()
            if existing:
                self.stdout.write(f'Exists:  {existing.name} ({mac})')
                continue
            place = info.get('coords') or {}
            point = place.get('coords') or {}
            station = Station.objects.create(
                owner=user, name=info.get('name') or mac, mac_address=mac,
                timezone=last.get('tz') or 'UTC', is_public=public,
                latitude=round(point['lat'], 6) if point.get('lat') is not None else None,
                longitude=round(point['lon'], 6) if point.get('lon') is not None else None,
                elevation_m=place.get('elevation'),
            )
            self.stdout.write(self.style.SUCCESS(
                f'Created: {station.name} ({mac}), time zone {station.timezone}, slug "{station.slug}"'))
