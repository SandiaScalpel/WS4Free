from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
env_file = BASE_DIR / '.env'
if env_file.exists():
    env.read_env(str(env_file))

ENVIRONMENT_NAME = env('DJANGO_ENVIRONMENT', default='prod')
DEBUG = env.bool('DJANGO_DEBUG', default=False)

# --- Required secrets, with fail-loud guards ---
# In production (DEBUG=False) these MUST come from the environment; the app refuses
# to boot on a missing value rather than silently falling back to a public default.
# In development a throwaway SECRET_KEY is generated so contributors can run the
# app with no .env at all.
SECRET_KEY = env('DJANGO_SECRET_KEY', default='')
if not SECRET_KEY:
    if DEBUG:
        from django.core.management.utils import get_random_secret_key
        SECRET_KEY = get_random_secret_key()
    else:
        raise ImproperlyConfigured(
            'DJANGO_SECRET_KEY must be set to a strong, unique value when DEBUG=False. '
            'Generate one with:\n'
            '  python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"'
        )

ALLOWED_HOSTS = env.list('DJANGO_ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])
if not DEBUG and not env('DJANGO_ALLOWED_HOSTS', default=''):
    raise ImproperlyConfigured(
        'DJANGO_ALLOWED_HOSTS must list your production hostname(s) when DEBUG=False '
        '(e.g. DJANGO_ALLOWED_HOSTS=weather.example.com).'
    )

CSRF_TRUSTED_ORIGINS = env.list('DJANGO_CSRF_TRUSTED_ORIGINS', default=[])

USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# ── Database ──────────────────────────────────────────────────────────────────
# MySQL is the reference deployment; the code sticks to the ORM so PostgreSQL and
# SQLite work too. See README "MySQL time zone tables" before running on MySQL.
DB_ENGINE = env('DB_ENGINE', default='mysql').lower()

if DB_ENGINE == 'mysql':
    # Prefer mysqlclient (C extension); fall back to pure-Python PyMySQL.
    try:
        import MySQLdb  # noqa: F401
    except ImportError:
        import pymysql
        pymysql.install_as_MySQLdb()
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.mysql',
            'NAME': env('DB_NAME', default='ws4free'),
            'USER': env('DB_USER', default=''),
            'PASSWORD': env('DB_PASSWORD', default=''),
            'HOST': env('DB_HOST', default='localhost'),
            'PORT': env('DB_PORT', default='3306'),
            'OPTIONS': {
                'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
                'charset': 'utf8mb4',
            },
        }
    }
elif DB_ENGINE in ('postgresql', 'postgres'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': env('DB_NAME', default='ws4free'),
            'USER': env('DB_USER', default=''),
            'PASSWORD': env('DB_PASSWORD', default=''),
            'HOST': env('DB_HOST', default='localhost'),
            'PORT': env('DB_PORT', default='5432'),
        }
    }
elif DB_ENGINE == 'sqlite':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': env('DB_NAME', default=str(BASE_DIR / 'db.sqlite3')),
        }
    }
else:
    raise ImproperlyConfigured(f"DB_ENGINE must be mysql, postgresql or sqlite (got {DB_ENGINE!r}).")

# ── Apps ──────────────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'accounts',
    'weather',
    'django_otp',
    'django_otp.plugins.otp_static',
    'django_otp.plugins.otp_totp',
    'django_otp_webauthn',
    'two_factor',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # Serves /static/ itself, so no proxy or web-server config is needed for assets.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'accounts.middleware.SessionIdleTimeoutMiddleware',
    'django_otp.middleware.OTPMiddleware',
    'accounts.middleware.Require2FASetupMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    # After MessageMiddleware — it calls messages.error(), which needs request._messages.
    'accounts.middleware.LoginRateLimitMiddleware',
    'accounts.middleware.ForcePasswordChangeMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'WS4Free.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'accounts.context_processors.environment_name',
                'accounts.context_processors.user_profile',
                'weather.context_processors.site',
            ],
        },
    },
]

WSGI_APPLICATION = 'WS4Free.wsgi.application'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 12}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

AUTHENTICATION_BACKENDS = [
    'accounts.backends.PasswordStrengthBackend',
    'django_otp_webauthn.backends.WebAuthnBackend',
]

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'two_factor:login'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'

TWO_FACTOR_PATCH_ADMIN = True
TWO_FACTOR_REQUIRED = False

OTP_WEBAUTHN_RP_NAME = env('OTP_WEBAUTHN_RP_NAME', default='WS4Free')
OTP_WEBAUTHN_RP_ID = env('OTP_WEBAUTHN_RP_ID', default='localhost')
OTP_WEBAUTHN_ALLOWED_ORIGINS = env.list('OTP_WEBAUTHN_ALLOWED_ORIGINS', default=['http://localhost:8000'])
OTP_WEBAUTHN_HELPER_CLASS = 'accounts.webauthn_helpers.WS4FreeWebAuthnHelper'

# ── Time ──────────────────────────────────────────────────────────────────────
# Everything is stored in UTC. Each Station carries its own IANA time zone, used
# for local-day rollups and display; TIME_ZONE only affects admin and logs.
LANGUAGE_CODE = 'en-us'
TIME_ZONE = env('TIME_ZONE', default='UTC')
USE_I18N = True
USE_TZ = True

# ── Static files ──────────────────────────────────────────────────────────────
STATIC_URL = '/static/'
STATIC_ROOT = env('DJANGO_STATIC_ROOT', default='') or BASE_DIR / 'staticfiles'

# WhiteNoise serves STATIC_ROOT with far-future cache headers; the manifest storage
# gives every file a content-hashed name, so a deploy can never leave a browser on a
# stale app.css. Run `collectstatic` after every deploy (the manifest must exist).
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}

# ── Weather station settings ──────────────────────────────────────────────────
# Ambient Weather REST API (https://ambientweather.docs.apiary.io). Both keys come
# from your ambientweather.net account page; leave blank to disable the poller.
AMBIENT_API_KEY = env('AMBIENT_API_KEY', default='')
AMBIENT_APPLICATION_KEY = env('AMBIENT_APPLICATION_KEY', default='')

# Default display units for anonymous visitors and new users: 'imperial' or 'metric'.
# Storage is always SI regardless.
DEFAULT_UNIT_SYSTEM = env('DEFAULT_UNIT_SYSTEM', default='imperial')

# Raw observations older than this many years are merged into 15-minute buckets.
# 0 disables downsampling (the default): keep every raw row forever.
RAW_RETENTION_YEARS = env.int('RAW_RETENTION_YEARS', default=0)

# Push ingest. INGEST_CAPTURE logs every raw upload (handy while setting up a
# console or debugging a parser); rejected and unusual uploads are always logged.
INGEST_CAPTURE = env.bool('INGEST_CAPTURE', default=False)
INGEST_CAPTURE_RETENTION_DAYS = env.int('INGEST_CAPTURE_RETENTION_DAYS', default=7)
# A console timestamp further than this from the server clock is ignored in favour
# of the receive time (consoles lose their clock after a power cut).
INGEST_MAX_CLOCK_SKEW_S = 900

# Weather station consoles upload over plain HTTP, so the ingest endpoints must not
# be redirected to HTTPS (the console will not follow the redirect).
SECURE_REDIRECT_EXEMPT = [r'^ingest/']

# ── Logging ───────────────────────────────────────────────────────────────────
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {'format': '{asctime} {levelname} {name} {message}', 'style': '{'},
    },
    'handlers': {
        'file': {
            'class': 'logging.FileHandler',
            # `or`, not default=: .env.example ships the key with an empty value.
            'filename': env('DJANGO_LOG_FILE', default='') or str(BASE_DIR / 'django_errors.log'),
            'formatter': 'verbose',
        },
        'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'},
    },
    'loggers': {
        'django': {'handlers': ['file'], 'level': 'ERROR', 'propagate': True},
        'weather': {'handlers': ['file', 'console'], 'level': 'INFO', 'propagate': False},
        'accounts': {'handlers': ['file'], 'level': 'ERROR', 'propagate': False},
    },
}

# ── Security ──────────────────────────────────────────────────────────────────
# Active in prod; Django skips enforcement when DEBUG=True.
# HTTPS is assumed in production. A LAN-only install served over plain HTTP sets
# DJANGO_HTTPS=False, or the redirect and secure-only cookies make sign-in impossible.
# (/ingest/ is exempt from the redirect either way — see SECURE_REDIRECT_EXEMPT.)
HTTPS = env.bool('DJANGO_HTTPS', default=not DEBUG)
SECURE_SSL_REDIRECT = HTTPS
SESSION_COOKIE_SECURE = HTTPS
CSRF_COOKIE_SECURE = HTTPS
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_IDLE_TIMEOUT = 7200  # seconds
X_FRAME_OPTIONS = 'SAMEORIGIN'
SECURE_CONTENT_TYPE_NOSNIFF = True
# HSTS is intentionally omitted — set it in the reverse proxy so it applies to all
# vhosts uniformly.

# ── Login throttling (accounts.ratelimit / LoginRateLimitMiddleware) ──────────
LOGIN_FAILURE_WINDOW = 900          # rolling window, seconds
LOGIN_LOCKOUT_DURATION = 900        # lock length, measured from the last failure
LOGIN_FAILURE_LIMIT = 5             # per (username, ip)
LOGIN_FAILURE_LIMIT_USER = 15       # per username, any address
LOGIN_FAILURE_LIMIT_IP = 25         # per address, any username
LOGIN_ATTEMPT_RETENTION_DAYS = 30

DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
