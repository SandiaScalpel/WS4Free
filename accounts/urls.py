from django.urls import path

from . import views

app_name = 'accounts'

urlpatterns = [
    path('settings/', views.settings_view, name='settings'),
    path('settings/password/', views.password_change, name='password-change'),
    path('settings/passkeys/<int:pk>/delete/', views.delete_passkey, name='delete-passkey'),
    path('settings/theme/', views.set_theme, name='set-theme'),
    path('2fa-nag/', views.twofa_nag, name='2fa-nag'),
    path('2fa-skip/', views.twofa_skip, name='2fa-skip'),
]
