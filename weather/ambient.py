"""Ambient Weather REST API client (https://ambientweather.docs.apiary.io).

Limits that shape this module:
  * 1 request/second per API key — every request goes through one throttle,
    spaced 1.2 s because exactly-1 s spacing still drew occasional 429s;
  * GET /v1/devices/{mac} returns at most 288 records, newest first, from the
    24 hours before `endDate` — history is read by paging backwards, and an empty
    page only means the station was offline that day;
  * HTTP 429 when over the limit — retried with exponential backoff.
"""
import datetime as dt
import logging
import threading
import time

import requests
from django.conf import settings

log = logging.getLogger(__name__)

BASE_URL = 'https://rt.ambientweather.net/v1'
MAX_LIMIT = 288
# GET /v1/devices/{mac}?endDate=X returns records from (X − 24 h, X] only.
HISTORY_WINDOW = dt.timedelta(days=1)


class AmbientAPIError(RuntimeError):
    pass


class AmbientClient:
    def __init__(self, api_key=None, application_key=None, *, min_interval=1.2, max_retries=6,
                 timeout=30, session=None, sleep=time.sleep, monotonic=time.monotonic):
        self.api_key = api_key if api_key is not None else settings.AMBIENT_API_KEY
        self.application_key = application_key if application_key is not None else settings.AMBIENT_APPLICATION_KEY
        if not (self.api_key and self.application_key):
            raise AmbientAPIError('AMBIENT_API_KEY and AMBIENT_APPLICATION_KEY must both be set in .env')
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = session or requests.Session()
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._last_request = None

    def _throttle(self):
        if self._last_request is not None:
            wait = self.min_interval - (self._monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._monotonic()

    def _get(self, path, **params):
        params = {'apiKey': self.api_key, 'applicationKey': self.application_key,
                  **{k: v for k, v in params.items() if v is not None}}
        with self._lock:
            for attempt in range(self.max_retries + 1):
                self._throttle()
                try:
                    response = self.session.get(BASE_URL + path, params=params, timeout=self.timeout)
                except requests.RequestException as exc:
                    error = f'network error: {exc}'
                else:
                    if response.status_code == 200:
                        return response.json()
                    if response.status_code not in (429, 500, 502, 503, 504):
                        # Never echo the URL: it carries both keys.
                        raise AmbientAPIError(f'GET {path} failed: HTTP {response.status_code} {response.text[:200]}')
                    error = f'HTTP {response.status_code}'
                if attempt == self.max_retries:
                    raise AmbientAPIError(f'GET {path} failed after {attempt + 1} attempts ({error})')
                backoff = min(2 ** attempt, 60)
                log.info('Ambient API %s on %s; retrying in %ss', error, path, backoff)
                self._sleep(backoff)

    def devices(self):
        """All devices on the account, each with `macAddress`, `info` and `lastData`."""
        return self._get('/devices')

    def device_data(self, mac, end_date=None, limit=MAX_LIMIT):
        """Up to `limit` records ending at `end_date` (aware datetime), newest first."""
        end_ms = int(end_date.timestamp() * 1000) if end_date is not None else None
        return self._get(f'/devices/{mac}', endDate=end_ms, limit=min(limit, MAX_LIMIT))

    def iter_history(self, mac, start=None, end=None, max_empty_days=120):
        """Yield pages (lists of records, newest first) walking backwards from `end`
        (default now) to `start`.

        The API returns only records from the 24 hours before endDate — not "the
        newest 288 before endDate" — so a day the station was offline comes back
        empty and is NOT the start of its history. Empty days are stepped over one
        day at a time; without `start`, the walk ends after `max_empty_days`
        consecutive empty days.
        """
        cursor = end or dt.datetime.now(dt.UTC)
        empty_run = 0
        while True:
            if start is not None and cursor <= start:
                return
            page = self.device_data(mac, end_date=cursor)
            if not page:
                empty_run += 1
                if start is None and empty_run >= max_empty_days:
                    return
                cursor -= HISTORY_WINDOW
                continue
            empty_run = 0
            oldest_ms = min(r['dateutc'] for r in page)
            if start is not None:
                start_ms = start.timestamp() * 1000
                page = [r for r in page if r['dateutc'] >= start_ms]
            if page:
                yield page
            if start is not None and oldest_ms <= start.timestamp() * 1000:
                return
            # endDate is inclusive; step 1 ms back so the oldest record isn't fetched twice.
            next_cursor = dt.datetime.fromtimestamp((oldest_ms - 1) / 1000, dt.UTC)
            if next_cursor >= cursor:
                return  # no progress; stop rather than loop forever
            cursor = next_cursor
