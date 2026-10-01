from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST
from django_otp import devices_for_user
from django_otp_webauthn.models import WebAuthnCredential

from weather.units import UNIT_CHOICES, prefs_for_request

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
