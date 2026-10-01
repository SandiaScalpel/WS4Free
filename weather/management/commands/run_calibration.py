import logging

from django.core.management.base import BaseCommand, CommandError

from weather.calibration import FitError, apply_calibration, fit_calibration, remove_calibration
from weather.models import TempCalibration

log = logging.getLogger('weather.calibration')


class Command(BaseCommand):
    help = 'Fit, apply or remove a temperature calibration (started in the background by the Data quality page).'

    def add_arguments(self, parser):
        parser.add_argument('calibration_id', type=int)
        parser.add_argument('action', choices=['fit', 'apply', 'remove'])

    def handle(self, *args, calibration_id, action, **options):
        try:
            calibration = TempCalibration.objects.select_related('station').get(pk=calibration_id)
        except TempCalibration.DoesNotExist:
            raise CommandError(f'No calibration {calibration_id}')
        try:
            if action == 'fit':
                fit_calibration(calibration)
                self.stdout.write(f'Fitted calibration {calibration_id}: {calibration.validation}')
            elif action == 'apply':
                n = apply_calibration(calibration)
                self.stdout.write(f'Applied calibration {calibration_id}: {n} readings')
            else:
                n = remove_calibration(calibration)
                self.stdout.write(f'Removed calibration {calibration_id}: {n} values restored')
        except FitError as exc:
            TempCalibration.objects.filter(pk=calibration_id).update(status='failed', message=str(exc))
            self.stderr.write(str(exc))
        except Exception as exc:
            log.exception('Calibration %s %s failed', calibration_id, action)
            TempCalibration.objects.filter(pk=calibration_id).update(status='failed', message=f'{type(exc).__name__}: {exc}')
            raise
