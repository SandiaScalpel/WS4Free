import logging

from django.core.management.base import BaseCommand, CommandError

from weather.models import DataExclusion
from weather.quality import apply_exclusion, remove_exclusion

log = logging.getLogger('weather.quality')


class Command(BaseCommand):
    help = 'Apply or remove a data-quality exclusion (started in the background by the Data quality page).'

    def add_arguments(self, parser):
        parser.add_argument('exclusion_id', type=int)
        parser.add_argument('action', choices=['apply', 'remove'])

    def handle(self, *args, exclusion_id, action, **options):
        try:
            exclusion = DataExclusion.objects.select_related('station').get(pk=exclusion_id)
        except DataExclusion.DoesNotExist:
            raise CommandError(f'No exclusion {exclusion_id}')
        try:
            if action == 'apply':
                n = apply_exclusion(exclusion)
                self.stdout.write(f'Applied exclusion {exclusion_id}: {n} readings')
            else:
                n = remove_exclusion(exclusion)
                self.stdout.write(f'Removed exclusion {exclusion_id}: {n} values restored')
        except Exception:
            log.exception('Exclusion %s %s failed', exclusion_id, action)
            DataExclusion.objects.filter(pk=exclusion_id).update(status='failed')
            raise
