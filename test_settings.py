"""Test settings — run the suite on in-memory sqlite.

    python manage.py test --settings=test_settings

The suite does not need MySQL: the production DB user typically has no CREATE
DATABASE grant, and sqlite keeps the suite runnable on a checkout with no database
server at all. Everything is ORM-only, so all migrations apply cleanly on sqlite.
"""
import os

os.environ.setdefault('DJANGO_DEBUG', 'True')

from WS4Free.settings import *  # noqa: E402,F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
SECURE_SSL_REDIRECT = False

# The manifest storage refuses to render {% static %} until collectstatic has run.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}
LOGGING = {'version': 1, 'disable_existing_loggers': False}
