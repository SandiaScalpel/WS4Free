"""Login throttling.

Password guessing was previously unlimited: nothing in the stack counted failures,
so an attacker could try credentials against /account/login/ as fast as the server
would answer. This module records every attempt and blocks further ones once a
threshold is crossed.

Three independent limits, because each alone has a hole:

  * (username, ip)  — the tight one. Catches an ordinary guessing run.
  * username        — catches an attacker rotating IPs against one account.
  * ip              — catches credential stuffing: many usernames, one source.

The username-wide limit means someone can deliberately lock a known username out
for the window length. That is accepted: the lockout is short and self-healing,
and the alternative (no username-wide limit) leaves rotation wide open. The
(username, ip) limit is much tighter so a legitimate user retrying from their own
address is never the one who trips the wide limit first.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone


def _setting(name, default):
    return getattr(settings, name, default)


def client_ip(request):
    """Best-effort client address.

    WS4Free is usually deployed behind a reverse proxy (often a tunnel as well),
    so REMOTE_ADDR is usually the proxy and is useless for distinguishing clients — every attempt
    would share one address and the per-IP limit would lock out the world. The
    left-most X-Forwarded-For entry is the real client as appended by the proxy
    chain. That header is client-forgeable, which is why it is not the only
    limit: an attacker who rotates it still trips the username-wide counter.
    """
    header = _setting('LOGIN_IP_HEADER', 'HTTP_X_FORWARDED_FOR')
    fwd = request.META.get(header, '')
    if fwd:
        first = fwd.split(',')[0].strip()
        if first:
            return first[:45]
    return (request.META.get('REMOTE_ADDR') or '')[:45]


def record_attempt(username, ip, successful, user_agent=''):
    from .models import LoginAttempt
    LoginAttempt.objects.create(
        username=(username or '')[:254],
        ip_address=ip or '',
        successful=successful,
        user_agent=(user_agent or '')[:255],
    )


def clear_failures(username, ip):
    """Drop the failure history for a pair after a successful login, so a user who
    fumbled their password a few times does not stay one mistake from a lockout."""
    from .models import LoginAttempt
    if not username:
        return
    LoginAttempt.objects.filter(
        username=username, ip_address=ip or '', successful=False,
    ).delete()


def lockout_state(username, ip):
    """Return (is_locked, seconds_remaining, reason) for this username/ip pair."""
    from .models import LoginAttempt

    window   = int(_setting('LOGIN_FAILURE_WINDOW', 900))
    lockout  = int(_setting('LOGIN_LOCKOUT_DURATION', 900))
    lim_pair = int(_setting('LOGIN_FAILURE_LIMIT', 5))
    lim_user = int(_setting('LOGIN_FAILURE_LIMIT_USER', 15))
    lim_ip   = int(_setting('LOGIN_FAILURE_LIMIT_IP', 25))

    since = timezone.now() - timedelta(seconds=window)
    fails = LoginAttempt.objects.filter(successful=False, timestamp__gte=since)

    checks = []
    if username:
        checks.append((fails.filter(username=username, ip_address=ip or ''), lim_pair,
                       'too many failed attempts for this account from your address'))
        checks.append((fails.filter(username=username), lim_user,
                       'too many failed attempts for this account'))
    if ip:
        checks.append((fails.filter(ip_address=ip), lim_ip,
                       'too many failed attempts from your address'))

    for qs, limit, reason in checks:
        recent = list(qs.order_by('-timestamp').values_list('timestamp', flat=True)[:limit])
        if len(recent) >= limit:
            # The lockout runs from the most recent failure, so continuing to hammer
            # extends it rather than waiting it out.
            unlock_at = recent[0] + timedelta(seconds=lockout)
            remaining = (unlock_at - timezone.now()).total_seconds()
            if remaining > 0:
                return True, int(remaining), reason
    return False, 0, ''


def purge_old_attempts(days=None):
    """Delete attempt rows older than the retention window, so the table cannot
    grow without bound. Called from the nightly maintenance job."""
    from .models import LoginAttempt
    days = int(days if days is not None else _setting('LOGIN_ATTEMPT_RETENTION_DAYS', 30))
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = LoginAttempt.objects.filter(timestamp__lt=cutoff).delete()
    return deleted
