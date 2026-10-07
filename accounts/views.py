from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.views import PasswordResetConfirmView
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.views.decorators.http import require_POST
from django_otp import devices_for_user, user_has_device
from django_otp_webauthn.models import WebAuthnCredential

from weather import access
from weather.models import StationAccess
from weather.units import UNIT_CHOICES, prefs_for_request

from .forms import StationAllowanceForm, UserForm
from .models import UserProfile


@login_required
def twofa_nag(request):
    return render(request, 'accounts/2fa_nag.html')


@login_required
def twofa_skip(request):
    request.session['2fa_nag_dismissed'] = True
    return redirect('weather:home')


@login_required
def settings_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        theme = request.POST.get('theme')
        if theme in dict(UserProfile.THEME_CHOICES):
            profile.theme = theme
            profile.save(update_fields=['theme'])
            messages.success(request, 'Preferences saved.')
        if request.POST.get('form') == 'units':
            changed = []
            for quantity, choices in UNIT_CHOICES.items():
                value = request.POST.get(f'unit_{quantity}', '')
                if value in dict(choices):
                    setattr(profile, f'unit_{quantity}', value)
                    changed.append(f'unit_{quantity}')
            profile.save(update_fields=changed)
            messages.success(request, 'Units saved.')
        return redirect('accounts:settings')
    prefs = prefs_for_request(request)
    return render(request, 'accounts/settings.html', {
        'theme_choices': UserProfile.THEME_CHOICES,
        'unit_rows': [
            {'name': f'unit_{q}', 'label': label, 'choices': UNIT_CHOICES[q], 'current': getattr(prefs, q)}
            for q, label in (('temp', 'Temperature'), ('wind', 'Wind speed'), ('pressure', 'Pressure'), ('rain', 'Rain'))
        ],
        'otp_devices': [d for d in devices_for_user(request.user) if not isinstance(d, WebAuthnCredential)],
        'passkeys': WebAuthnCredential.objects.filter(user=request.user).order_by('-created_at'),
    })


@require_POST
def set_theme(request):
    """Persist the header theme toggle. Anonymous visitors keep it in localStorage
    only, so this is a no-op for them."""
    theme = request.POST.get('theme')
    if request.user.is_authenticated and theme in dict(UserProfile.THEME_CHOICES):
        UserProfile.objects.filter(user=request.user).update(theme=theme)
    return HttpResponse(status=204)


@login_required
@require_POST
def delete_passkey(request, pk):
    WebAuthnCredential.objects.filter(pk=pk, user=request.user).delete()
    messages.success(request, 'Passkey removed.')
    return redirect('accounts:settings')


@login_required
def password_change(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        UserProfile.objects.filter(user=user).update(force_password_change=False)
        messages.success(request, 'Password changed.')
        return redirect('accounts:settings')
    return render(request, 'accounts/password_change.html', {'form': form})


# ── Users (site administrators) ───────────────────────────────────────────────

staff_required = user_passes_test(lambda u: u.is_active and u.is_staff)
WELCOME_LINK_SESSION_KEY = 'ws4f_welcome_link'


def welcome_link(request, user):
    """A one-time link where the user chooses a password: Django's password-reset token,
    so it stops working once used (the password hash changes) or after
    PASSWORD_RESET_TIMEOUT (3 days)."""
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    return request.build_absolute_uri(reverse('accounts:welcome', args=[uid, default_token_generator.make_token(user)]))


def _remember_link(request, user):
    request.session[WELCOME_LINK_SESSION_KEY] = {'user': user.pk, 'url': welcome_link(request, user)}


def _active_staff_count():
    return get_user_model().objects.filter(is_active=True, is_staff=True).count()


@staff_required
def user_list(request):
    users = (get_user_model().objects.order_by('-is_active', 'username')
             .select_related('profile').prefetch_related('stations', 'station_access__station'))
    rows = [{
        'u': u, 'owned': list(u.stations.all()),
        'shared': [(a.station, a.get_role_display()) for a in u.station_access.all()],
        'two_factor': user_has_device(u),
        'allowance': (None if u.is_staff or not getattr(u, 'profile', None) or not u.profile.may_add_stations
                      else u.profile.station_limit),
    } for u in users]
    return render(request, 'accounts/user_list.html', {'rows': rows})


@staff_required
def user_create(request):
    form = UserForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save(commit=False)
        user.set_unusable_password()
        user.save()
        UserProfile.objects.get_or_create(user=user)
        _remember_link(request, user)
        messages.success(request, f'{user.get_username()} added. Send them the welcome link below.')
        return redirect('accounts:user-detail', pk=user.pk)
    return render(request, 'accounts/user_form.html', {'form': form})


@staff_required
def user_detail(request, pk):
    user = get_object_or_404(get_user_model(), pk=pk)
    was_staff = user.is_staff                       # is_valid() copies the posted values onto `user`
    form = UserForm(request.POST or None, instance=user, editing=True)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    allowance = StationAllowanceForm(request.POST or None, instance=profile, prefix='a')
    if request.method == 'POST' and form.is_valid() and allowance.is_valid():
        if was_staff and not form.cleaned_data['is_staff']:
            if user.pk == request.user.pk:
                form.add_error('is_staff', 'You can\'t remove your own administrator access.')
            elif user.is_active and _active_staff_count() <= 1:
                form.add_error('is_staff', 'The site needs at least one administrator.')
        if not form.errors:
            form.save()
            allowance.save()
            messages.success(request, 'Saved.')
            return redirect('accounts:user-detail', pk=user.pk)
    link = request.session.pop(WELCOME_LINK_SESSION_KEY, None)
    return render(request, 'accounts/user_detail.html', {
        'subject': get_user_model().objects.get(pk=user.pk), 'form': form,   # as saved, not as posted
        'allowance': allowance,
        'link': link['url'] if link and link['user'] == user.pk else None,
        'owned': list(user.stations.all()),
        'shared': list(user.station_access.select_related('station')),
        'two_factor': user_has_device(user),
        'has_password': user.has_usable_password(),
    })


@staff_required
@require_POST
def user_new_link(request, pk):
    user = get_object_or_404(get_user_model(), pk=pk, is_active=True)
    _remember_link(request, user)
    messages.info(request, 'New welcome link below. Using it sets a new password.')
    return redirect('accounts:user-detail', pk=user.pk)


@staff_required
@require_POST
def user_set_active(request, pk):
    """Deactivate (signed out at once, station access removed) or reactivate. Someone who
    owns stations is deactivated only with `take_stations`: they're transferred to you."""
    user = get_object_or_404(get_user_model(), pk=pk)
    activate = request.POST.get('active') == '1'
    if activate:
        user.is_active = True
        user.save(update_fields=['is_active'])
        messages.success(request, f'{user.get_username()} can sign in again. Their station access was removed when '
                                  'they were deactivated; add it back from each station\'s People tab.')
        return redirect('accounts:user-detail', pk=user.pk)
    if user.pk == request.user.pk:
        messages.error(request, 'You can\'t deactivate yourself.')
        return redirect('accounts:user-detail', pk=user.pk)
    if user.is_staff and _active_staff_count() <= 1:
        messages.error(request, 'The site needs at least one administrator.')
        return redirect('accounts:user-detail', pk=user.pk)
    owned = list(user.stations.all())
    if owned and request.POST.get('take_stations') != '1':
        messages.error(request, 'Transfer their stations first.')
        return redirect('accounts:user-detail', pk=user.pk)
    with transaction.atomic():
        for station in owned:
            access.transfer(station, request.user, by=request.user, keep_previous=False)
        StationAccess.objects.filter(user=user).delete()
        user.is_active = False
        user.save(update_fields=['is_active'])
    # Their sessions stop working on the next request: Django's backends refuse inactive users.
    messages.success(request, f'{user.get_username()} deactivated.'
                     + (f' You now own {", ".join(s.name for s in owned)}.' if owned else ''))
    return redirect('accounts:user-detail', pk=user.pk)


class WelcomeView(PasswordResetConfirmView):
    """Where a welcome (or new) link lands: choose a password, then sign in."""
    template_name = 'accounts/welcome.html'
    success_url = reverse_lazy('two_factor:login')

    def get_user(self, uidb64):
        user = super().get_user(uidb64)
        return user if user is not None and user.is_active else None

    def form_valid(self, form):
        response = super().form_valid(form)
        UserProfile.objects.filter(user=form.user).update(force_password_change=False)
        messages.success(self.request, 'Password set. Sign in with your username and new password.')
        return response
