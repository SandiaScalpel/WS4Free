import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse
from django_otp import user_has_device

# Station consoles post here with no session; nothing user-facing should ever
# redirect them.
INGEST_PREFIX = '/ingest/'


class SessionIdleTimeoutMiddleware:
    """Logs out authenticated users after SESSION_IDLE_TIMEOUT seconds of inactivity.

    Background polls (live dashboard tiles, which send ``X-Poll: 1`` via
    hx-headers) do not count as activity — otherwise a dashboard left open would
    keep the session alive forever.
    """
    EXEMPT_PREFIXES = ('/account/', '/admin/', '/webauthn/', INGEST_PREFIX)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and not any(
            request.path.startswith(p) for p in self.EXEMPT_PREFIXES
        ):
            now = time.time()
            last_activity = request.session.get('last_activity')
            timeout = getattr(settings, 'SESSION_IDLE_TIMEOUT', 7200)

            if last_activity and (now - last_activity) > timeout:
                logout(request)
                login_url = reverse(settings.LOGIN_URL)
                return redirect(f'{login_url}?next={request.path}')

            if not request.headers.get('X-Poll'):
                request.session['last_activity'] = now
                request.session.set_expiry(timeout)

        return self.get_response(request)


class ForcePasswordChangeMiddleware:
    EXEMPT_PREFIXES = ('/account/', '/admin/', INGEST_PREFIX)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and not any(request.path.startswith(p) for p in self.EXEMPT_PREFIXES)
        ):
            try:
                force_change = request.user.profile.force_password_change
            except Exception:
                force_change = False
            if force_change:
                change_url = reverse('accounts:password-change')
                if request.path != change_url:
                    messages.warning(request, 'You must change your password before continuing.')
                    return redirect(change_url)
        return self.get_response(request)


class Require2FASetupMiddleware:
    """Sends a signed-in user without a 2FA device to the setup nag once per session."""
    ALLOWED_URL_PREFIXES = ('/account/', '/admin/', '/webauthn/', '/static/', INGEST_PREFIX)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and not request.session.get('2fa_nag_dismissed')
            and not any(request.path.startswith(prefix) for prefix in self.ALLOWED_URL_PREFIXES)
            and request.path not in (reverse('accounts:2fa-nag'), reverse('accounts:2fa-skip'))
            and not user_has_device(request.user)
        ):
            return redirect(reverse('accounts:2fa-nag'))

        return self.get_response(request)


class LoginRateLimitMiddleware:
    """Blocks login POSTs once the failure thresholds in accounts.ratelimit are hit.

    This sits in front of the view rather than inside the auth backend so a blocked
    request never reaches the password hasher, and the user gets an explicit
    "locked out, try again in N minutes" message instead of a generic
    bad-credentials error. Only the login endpoints are inspected.
    """
    LOGIN_PATHS = ('/account/login/', '/admin/login/')

    def __init__(self, get_response):
        self.get_response = get_response

    def _is_login_post(self, request):
        if request.method != 'POST':
            return False
        path = request.path.rstrip('/')
        return any(path == p.rstrip('/') for p in self.LOGIN_PATHS)

    def __call__(self, request):
        if self._is_login_post(request):
            from .ratelimit import client_ip, lockout_state

            # two_factor's wizard posts the username under a step-prefixed name.
            username = (
                request.POST.get('auth-username')
                or request.POST.get('username')
                or ''
            ).strip()
            ip = client_ip(request)
            locked, remaining, reason = lockout_state(username, ip)
            if locked:
                minutes = max(1, (remaining + 59) // 60)
                messages.error(
                    request,
                    f'Account temporarily locked — {reason}. '
                    f'Try again in about {minutes} minute{"s" if minutes != 1 else ""}.',
                )
                return redirect(reverse(settings.LOGIN_URL))

        return self.get_response(request)
