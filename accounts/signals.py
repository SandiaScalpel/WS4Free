from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.db.models.signals import post_save
from django.dispatch import receiver

from .ratelimit import clear_failures, client_ip, record_attempt


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_profile(sender, instance, created, **kwargs):
    if created:
        from .models import UserProfile
        UserProfile.objects.get_or_create(user=instance)


@receiver(user_logged_in)
def check_password_strength_on_login(sender, request, user, **kwargs):
    weak = getattr(request, '_weak_password', None)
    if weak is None:
        return  # passkey or other non-password login — leave counter unchanged

    from .models import UserProfile
    profile, _ = UserProfile.objects.get_or_create(user=user)

    if weak:
        profile.weak_password_logins = min(profile.weak_password_logins + 1, 10)
        if profile.weak_password_logins >= 5:
            profile.password_strength_warning = True
    else:
        profile.weak_password_logins = 0
        profile.password_strength_warning = False

    profile.save(update_fields=['weak_password_logins', 'password_strength_warning'])


# ── Login throttling bookkeeping ──────────────────────────────────────────────
# The middleware decides whether to block; these two receivers only record what
# happened. They hang off the auth signals rather than the login view so that any
# authentication path (password form, admin, API) is counted the same way.

def _ua(request):
    return request.META.get('HTTP_USER_AGENT', '') if request else ''


@receiver(user_login_failed)
def record_failed_login(sender, credentials, request=None, **kwargs):
    username = (credentials or {}).get('username') or ''
    ip = client_ip(request) if request else ''
    record_attempt(username, ip, successful=False, user_agent=_ua(request))


@receiver(user_logged_in)
def record_successful_login(sender, request, user, **kwargs):
    ip = client_ip(request) if request else ''
    record_attempt(user.get_username(), ip, successful=True, user_agent=_ua(request))
    # A correct password clears the pair's failure history, so the next typo does
    # not land on top of an old streak.
    clear_failures(user.get_username(), ip)
