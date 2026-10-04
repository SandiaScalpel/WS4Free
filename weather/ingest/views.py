"""Custom-server upload endpoints for station consoles.

    Ambient / Wunderground protocol:  GET  /ingest/ambient/<token>/?tempf=…
    Ecowitt protocol:                 POST /ingest/ecowitt/<token>/   (form body)

Consoles build the URL by gluing their parameters onto the configured path, and
not all of them add the '?' — so anything after the token is parsed as
parameters too ('/ingest/ambient/<token>/&PASSKEY=…&tempf=…' works).

These views run without a session, CSRF token or HTTPS (see SECURE_REDIRECT_EXEMPT
and accounts.middleware.INGEST_PREFIX). The token in the URL is the credential.
"""
import logging
from urllib.parse import parse_qsl, urlencode

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, HttpResponseNotFound
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.ratelimit import client_ip

from ..models import IngestCapture, Observation, SensorGateway, Station
from .parsers import ParseError, parse_ambient_push, parse_ecowitt_push
from .store import record_gateway, record_push

log = logging.getLogger(__name__)


def _params(request, rest):
    params = {}
    tail = (rest or '').lstrip('/?&')
    if '=' in tail:
        params.update(parse_qsl(tail, keep_blank_values=False))
    params.update(request.GET.dict())
    if request.method == 'POST':
        params.update(request.POST.dict())
    # A path configured with a trailing '?' plus a console that adds its own
    # gives keys like '?PASSKEY'.
    return {key.lstrip('?&'): value for key, value in params.items()}


def _capture(station, request, protocol, params, accepted, note='', token=None):
    payload = urlencode(params)
    path = request.path.replace(token or station.push_token, '<token>') if station else request.path
    IngestCapture.objects.create(
        station=station, protocol=protocol, method=request.method, remote_addr=client_ip(request)[:45],
        path=path[:500], payload=payload, accepted=accepted, note=note[:255],
    )


def _ingest(request, token, rest, protocol, parser, source):
    station = Station.objects.filter(push_token=token).first()
    gateway = None
    if station is None:
        # A sensor gateway's upload path: only its extra sensors are kept.
        gateway = SensorGateway.objects.select_related('station').filter(push_token=token).first()
        station = gateway.station if gateway else None
    if station is None:
        # No capture: unknown tokens are scanners, and logging them would let anyone fill the table.
        return HttpResponseNotFound('unknown station\n', content_type='text/plain')
    if gateway is not None:
        return _ingest_gateway(request, gateway, rest, protocol, parser, source)

    params = _params(request, rest)
    max_skew = getattr(settings, 'INGEST_MAX_CLOCK_SKEW_S', 900)
    try:
        reading = parser(params, timezone.now(), max_skew, rain_gauge=station.rain_gauge)
    except ParseError as exc:
        _capture(station, request, protocol, params, False, f'parse error: {exc}')
        return HttpResponseBadRequest(f'{exc}\n', content_type='text/plain')

    note = ''
    if reading.passkey:
        passkey = reading.passkey.upper()
        if station.push_passkey and passkey != station.push_passkey:
            log.warning('Rejected %s upload for %s: PASSKEY does not match the learned one', protocol, station)
            _capture(station, request, protocol, params, False, 'PASSKEY does not match this station')
            return HttpResponseForbidden('passkey mismatch\n', content_type='text/plain')
        if not station.push_passkey:
            Station.objects.filter(pk=station.pk, push_passkey='').update(push_passkey=passkey)
            note = 'learned PASSKEY'
            if passkey not in station.passkey_candidates():
                note += ' (does not look like this station\'s MAC — check the console)'
            log.info('Station %s: %s', station, note)
    if reading.clock_adjusted:
        note = (note + '; ' if note else '') + 'console clock implausible, used server time'

    record_push(station, reading, source)
    if getattr(settings, 'INGEST_CAPTURE', False) or note:
        _capture(station, request, protocol, params, True, note)
    return HttpResponse('success\n', content_type='text/plain')


def _ingest_gateway(request, gateway, rest, protocol, parser, source):
    """A sensor gateway's upload: same checks as a console's, its own learned
    PASSKEY, and only extra sensors stored (weather.ingest.store.record_gateway)."""
    station = gateway.station
    params = _params(request, rest)
    max_skew = getattr(settings, 'INGEST_MAX_CLOCK_SKEW_S', 900)

    def capture(accepted, note=''):
        _capture(station, request, f'{protocol} gateway', params, accepted,
                 f'{gateway.name}: {note}' if note else gateway.name, token=gateway.push_token)

    try:
        reading = parser(params, timezone.now(), max_skew, rain_gauge=station.rain_gauge)
    except ParseError as exc:
        capture(False, f'parse error: {exc}')
        return HttpResponseBadRequest(f'{exc}\n', content_type='text/plain')
    note = ''
    if reading.passkey:
        passkey = reading.passkey.upper()
        if gateway.push_passkey and passkey != gateway.push_passkey:
            log.warning('Rejected %s upload for gateway %s: PASSKEY does not match the learned one', protocol, gateway)
            capture(False, 'PASSKEY does not match this gateway')
            return HttpResponseForbidden('passkey mismatch\n', content_type='text/plain')
        if not gateway.push_passkey:
            SensorGateway.objects.filter(pk=gateway.pk, push_passkey='').update(push_passkey=passkey)
            note = 'learned PASSKEY'
    kept = record_gateway(gateway, reading, source)
    if not kept:
        note = (note + '; ' if note else '') + 'no extra sensors in this upload'
    if getattr(settings, 'INGEST_CAPTURE', False) or note:
        capture(True, note)
    return HttpResponse('success\n', content_type='text/plain')


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def ambient_push(request, token, rest=''):
    return _ingest(request, token, rest, 'ambient', parse_ambient_push, Observation.SOURCE_AMBIENT_PUSH)


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def ecowitt_push(request, token, rest=''):
    return _ingest(request, token, rest, 'ecowitt', parse_ecowitt_push, Observation.SOURCE_ECOWITT_PUSH)
