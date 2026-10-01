import datetime as dt
import importlib.util
import os
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

spec = importlib.util.spec_from_file_location('scheduler', Path(settings.BASE_DIR) / 'docker' / 'scheduler.py')
scheduler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduler)


class SchedulerTimingTests(SimpleTestCase):
    def at(self, h, m):
        return dt.datetime(2026, 10, 1, h, m, tzinfo=dt.UTC)

    @mock.patch.dict(os.environ, {'AMBIENT_API_KEY': 'k', 'AMBIENT_APPLICATION_KEY': 'a'})
    def test_five_minute_jobs(self):
        self.assertEqual(scheduler.jobs_due(self.at(10, 2), None), [('poll_ambient',)])
        self.assertEqual(scheduler.jobs_due(self.at(10, 4), None), [('refresh_rollups',)])
        self.assertEqual(scheduler.jobs_due(self.at(10, 5), None), [])

    @mock.patch.dict(os.environ, {'AMBIENT_API_KEY': '', 'AMBIENT_APPLICATION_KEY': ''})
    def test_no_poll_without_keys(self):
        self.assertEqual(scheduler.jobs_due(self.at(10, 2), None), [])

    def test_housekeeping_once_a_day(self):
        hk = self.at(scheduler.HOUSEKEEPING.hour, scheduler.HOUSEKEEPING.minute)
        self.assertIn(('ws4free_housekeeping',), scheduler.jobs_due(hk, None))
        self.assertNotIn(('ws4free_housekeeping',), scheduler.jobs_due(hk, hk.date()))
