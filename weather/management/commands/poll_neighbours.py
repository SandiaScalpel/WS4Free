from django.conf import settings
from django.core.management.base import BaseCommand

from weather import neighbours


class Command(BaseCommand):
    help = ('Fetch the current conditions of every neighbouring Weather Underground station that is due '
            '(every WU_POLL_MINUTES). Run every 5 minutes; does nothing without WU_API_KEY.')

    def handle(self, *args, **options):
        if not settings.WU_API_KEY:
            if options['verbosity'] > 1:
                self.stdout.write('WU_API_KEY is not set; nothing to do.')
            return
        added = neighbours.poll()
        if options['verbosity'] > 1 or added:
            self.stdout.write(f'{added} new neighbour reading(s)')
