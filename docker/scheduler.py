"""Background jobs for the Docker image (a cron replacement with no extra packages).

    every 5 minutes, at :01 :06 …   poll_neighbours     (only when WU_API_KEY is set)
    every 5 minutes, at :02 :07 …   poll_ambient        (only when AMBIENT_* keys are set)
    every 5 minutes, at :04 :09 …   refresh_rollups
    daily at HOUSEKEEPING_TIME       ws4free_housekeeping (local time, TIME_ZONE)

Each job runs as its own `manage.py` process, so one failing never stops the
others. Times follow the TIME_ZONE environment variable (default UTC).
Set SCHEDULER_ONCE=1 to run every job once and exit (used by the tests).
"""
import datetime as dt
import os
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

APP = Path(__file__).resolve().parent.parent
TZ = ZoneInfo(os.environ.get('TIME_ZONE', 'UTC'))
HOUSEKEEPING = dt.time.fromisoformat(os.environ.get('HOUSEKEEPING_TIME', '03:30'))


def run(*args):
    started = time.monotonic()
    result = subprocess.run([sys.executable, str(APP / 'manage.py'), *args], cwd=APP)
    print(f'[scheduler] {" ".join(args)} → exit {result.returncode} in {time.monotonic() - started:.1f}s', flush=True)


def jobs_due(now, last_housekeeping):
    due = []
    if now.minute % 5 == 1 and os.environ.get('WU_API_KEY'):
        due.append(('poll_neighbours',))
    if now.minute % 5 == 2 and os.environ.get('AMBIENT_API_KEY') and os.environ.get('AMBIENT_APPLICATION_KEY'):
        due.append(('poll_ambient',))
    if now.minute % 5 == 4:
        due.append(('refresh_rollups',))
    if (now.hour, now.minute) == (HOUSEKEEPING.hour, HOUSEKEEPING.minute) and last_housekeeping != now.date():
        due.append(('ws4free_housekeeping',))
    return due


def main():
    if os.environ.get('SCHEDULER_ONCE'):
        for job in (('poll_neighbours',), ('poll_ambient',), ('refresh_rollups',), ('ws4free_housekeeping',)):
            if job[0] == 'poll_ambient' and not os.environ.get('AMBIENT_API_KEY'):
                continue
            if job[0] == 'poll_neighbours' and not os.environ.get('WU_API_KEY'):
                continue
            run(*job)
        return
    print(f'[scheduler] started; time zone {TZ.key}, housekeeping at {HOUSEKEEPING:%H:%M}', flush=True)
    last_housekeeping = None
    while True:
        now = dt.datetime.now(TZ)
        for job in jobs_due(now, last_housekeeping):
            if job == ('ws4free_housekeeping',):
                last_housekeeping = now.date()
            run(*job)
        # Sleep to the start of the next minute.
        time.sleep(60 - dt.datetime.now(TZ).second + 0.5)


if __name__ == '__main__':
    main()
