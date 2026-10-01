import datetime as dt

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.ratelimit import purge_old_attempts
from weather.downsample import StaleRollups, downsample_station
from weather.models import IngestCapture, Station


class Command(BaseCommand):
    help = 'Daily clean-up: old ingest captures and login attempts, and downsampling when enabled.'

    def handle(self, *args, **options):
        cutoff = timezone.now() - dt.timedelta(days=settings.INGEST_CAPTURE_RETENTION_DAYS)
        captures, _ = IngestCapture.objects.filter(received_at__lt=cutoff).delete()
        attempts = purge_old_attempts()
        self.stdout.write(f'Purged {captures} ingest capture(s), {attempts} login attempt(s).')

        if settings.RAW_RETENTION_YEARS > 0:
            for station in Station.objects.all():
                try:
                    days, before, after = downsample_station(station, settings.RAW_RETENTION_YEARS)
                except StaleRollups as exc:
                    self.stderr.write(f'Skipped downsampling: {exc}')
                    continue
                if days:
                    self.stdout.write(f'{station}: downsampled {days} day(s), {before} → {after} rows')
