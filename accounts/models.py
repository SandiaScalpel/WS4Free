from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    THEME_CHOICES = [
        ('auto', 'Match system'),
        ('light', 'Light'),
        ('dark', 'Dark'),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    theme = models.CharField(max_length=5, choices=THEME_CHOICES, default='auto')
    # Display units; blank = the site default (DEFAULT_UNIT_SYSTEM). Storage is always SI.
    # Choices live in weather.units so the two apps agree on the codes.
    unit_temp = models.CharField(max_length=4, blank=True)
    unit_wind = models.CharField(max_length=4, blank=True)
    unit_pressure = models.CharField(max_length=4, blank=True)
    unit_rain = models.CharField(max_length=4, blank=True)
    force_password_change = models.BooleanField(default=False)
    # Consecutive logins with a weak password (capped at 10); see accounts.signals.
    weak_password_logins = models.PositiveSmallIntegerField(default=0)
    password_strength_warning = models.BooleanField(default=False)
    # Set by site administrators: whether a non-administrator may add stations of their
    # own, and how many they may own in all (weather.access.can_add_station).
    may_add_stations = models.BooleanField('may add their own stations', default=False)
    station_limit = models.PositiveSmallIntegerField('how many', default=1)

    def __str__(self):
        return f'Profile for {self.user}'


class LoginAttempt(models.Model):
    """One row per login attempt; feeds the throttle in accounts.ratelimit."""
    username = models.CharField(max_length=254, db_index=True)
    ip_address = models.CharField(max_length=45, db_index=True)
    successful = models.BooleanField(default=False)
    user_agent = models.CharField(max_length=255, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{'ok' if self.successful else 'FAIL'} {self.username} @ {self.ip_address}"
