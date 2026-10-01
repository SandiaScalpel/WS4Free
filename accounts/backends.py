import re

from django.contrib.auth.backends import ModelBackend


def _is_password_strong(password):
    if not password:
        return False
    n = len(password)
    has_upper = bool(re.search(r'[A-Z]', password))
    has_lower = bool(re.search(r'[a-z]', password))
    has_digit = bool(re.search(r'\d', password))
    has_special = bool(re.search(r'[^A-Za-z0-9]', password))
    if n >= 19:
        return True
    return n >= 13 and has_upper and has_lower and has_digit and has_special


class PasswordStrengthBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        user = super().authenticate(request, username=username, password=password, **kwargs)
        if user is not None and request is not None and password:
            request._weak_password = not _is_password_strong(password)
        return user
