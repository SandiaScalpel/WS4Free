from django.contrib import admin

from .models import LoginAttempt, UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'theme', 'force_password_change', 'password_strength_warning')
    list_filter = ('theme', 'force_password_change')
    search_fields = ('user__username',)


@admin.register(LoginAttempt)
class LoginAttemptAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'username', 'ip_address', 'successful')
    list_filter = ('successful',)
    search_fields = ('username', 'ip_address')
    readonly_fields = ('timestamp', 'username', 'ip_address', 'successful', 'user_agent')
