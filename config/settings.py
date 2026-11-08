"""Shared Django configuration for local and operator-hosted Pandect."""

import os
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY before starting Pandect.")

DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
APP_COMMIT_SHA = os.environ.get("APP_COMMIT_SHA", "unknown")
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]"
    ).split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "whitenoise.runserver_nostatic",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_tasks_db",
    "background.apps.BackgroundConfig",
    "accounts.apps.AccountsConfig",
    "cases.apps.CasesConfig",
    "core.apps.CoreConfig",
    "people.apps.PeopleConfig",
    "vehicle.apps.VehicleConfig",
    "text.apps.TextConfig",
    "image.apps.ImageConfig",
    "audio.apps.AudioConfig",
    "document.apps.DocumentConfig",
    "geo.apps.GeoConfig",
    "phone.apps.PhoneConfig",
    "socialmedia.apps.SocialmediaConfig",
    "video.apps.VideoConfig",
    "marketing.apps.MarketingConfig",
    "ui.apps.UiConfig",
    "allauth",
    "allauth.account",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "config.privacy.PrivatePageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "accounts.context_processors.registration",
            ],
        },
    },
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Browser basemap tiles are separate from retained Overpass/geocoding results.
PANDECT_MAP_TILE_URL = os.environ.get("PANDECT_MAP_TILE_URL", "")
_tile_url = urlsplit(PANDECT_MAP_TILE_URL)
if PANDECT_MAP_TILE_URL and (
    _tile_url.scheme != "https"
    or not _tile_url.hostname
    or _tile_url.username is not None
    or _tile_url.password is not None
    or _tile_url.query
    or _tile_url.fragment
    or any(character in _tile_url.netloc for character in " ;'\"<>{}\\\r\n\t")
    or any(PANDECT_MAP_TILE_URL.count(token) != 1 for token in ("{z}", "{x}", "{y}"))
):
    raise ImproperlyConfigured(
        "PANDECT_MAP_TILE_URL must be an HTTPS tile URL with {z}, {x}, and {y}, "
        "without credentials, query, or fragment."
    )
PANDECT_MAP_TILE_ORIGIN = f"https://{_tile_url.netloc}" if PANDECT_MAP_TILE_URL else ""
PANDECT_MAP_TILE_ATTRIBUTION = os.environ.get("PANDECT_MAP_TILE_ATTRIBUTION", "")

# PostgreSQL is also used by Django's test runner. There is no SQLite fallback.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "pandect"),
        "USER": os.environ.get("POSTGRES_USER", "pandect"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "OPTIONS": {"connect_timeout": 5},
    }
}
# Use the durable queue in development too; DEBUG must not run work inline.
TASKS = {
    "default": {
        "BACKEND": "django_tasks_db.DatabaseBackend",
        "QUEUES": ["default"],
    }
}
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
        )
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
ACCOUNT_LOGIN_METHODS = {"username", "email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "username*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_ADAPTER = "accounts.adapter.PandectAccountAdapter"
PANDECT_ALLOW_SIGNUPS = (
    os.environ.get("PANDECT_ALLOW_SIGNUPS", "true").lower() == "true"
)
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "cases:case_list"
LOGOUT_REDIRECT_URL = "account_login"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
# Pandect models declare UUID primary keys explicitly. This fallback is for
# Django/allauth internals and automatically generated many-to-many tables.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STATIC_URL = "/static/"
STATICFILES_DIRS = []
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}
# Private evidence is deliberately outside WhiteNoise's static root.
MEDIA_ROOT = Path(
    os.environ.get("PANDECT_MEDIA_ROOT", str(BASE_DIR / "data" / "media"))
)

SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
X_FRAME_OPTIONS = "DENY"
# Enable only when a controlled proxy overwrites X-Forwarded-Proto and the
# application port cannot be reached directly by untrusted clients.
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https")
    if os.environ.get("DJANGO_TRUST_PROXY_HEADERS", "false").lower() == "true"
    else None
)
SECURE_SSL_REDIRECT = (
    os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "false").lower() == "true"
)
SECURE_REDIRECT_EXEMPT = [r"^health/$"]
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "0"))
if SECURE_HSTS_SECONDS < 0:
    raise ImproperlyConfigured("DJANGO_SECURE_HSTS_SECONDS must not be negative.")
CSRF_TRUSTED_ORIGINS = [
    value.strip()
    for value in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if value.strip()
]


def positive_env(name, default):
    try:
        value = int(os.environ.get(name, default))
    except ValueError as error:
        raise ImproperlyConfigured(f"{name} must be a positive integer.") from error
    if value <= 0:
        raise ImproperlyConfigured(f"{name} must be a positive integer.")
    return value


PANDECT_MAX_FILE_BYTES = positive_env("PANDECT_MAX_FILE_BYTES", 100 * 1024 * 1024)
PANDECT_MAX_TEXT_BYTES = positive_env("PANDECT_MAX_TEXT_BYTES", 1024 * 1024)
PANDECT_MAX_BATCH_FILES = positive_env("PANDECT_MAX_BATCH_FILES", 20)
PANDECT_INVITATION_DAYS = positive_env("PANDECT_INVITATION_DAYS", 7)
PANDECT_PUBLIC_ORIGIN = os.environ.get(
    "PANDECT_PUBLIC_ORIGIN", "http://localhost:8000"
).rstrip("/")
origin = urlsplit(PANDECT_PUBLIC_ORIGIN)
if (
    origin.scheme not in {"http", "https"}
    or not origin.netloc
    or origin.username
    or origin.password
    or origin.path
    or origin.query
    or origin.fragment
):
    raise ImproperlyConfigured("PANDECT_PUBLIC_ORIGIN must be an HTTP(S) origin.")
FILE_UPLOAD_HANDLERS = [
    "core.evidence.uploads.LimitedUploadHandler",
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
    "django.core.files.uploadhandler.TemporaryFileUploadHandler",
]
DATA_UPLOAD_MAX_MEMORY_SIZE = max(2 * 1024 * 1024, PANDECT_MAX_TEXT_BYTES + 65536)
DATA_UPLOAD_MAX_NUMBER_FILES = 1
FILE_UPLOAD_PERMISSIONS = 0o600
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"private_urls": {"()": "config.privacy.RedactPrivateURLs"}},
    "handlers": {
        "private_console": {
            "class": "logging.StreamHandler",
            "filters": ["private_urls"],
        }
    },
    "loggers": {
        "django.server": {
            "handlers": ["private_console"],
            "level": "INFO",
            "propagate": False,
        },
        "django": {
            "handlers": ["private_console"],
            "level": "INFO",
            "propagate": False,
        },
        "core": {
            "handlers": ["private_console"],
            "level": "WARNING",
            "propagate": False,
        },
        "django_tasks_db": {
            "handlers": ["private_console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

DEFAULT_FROM_EMAIL = os.environ.get("DJANGO_DEFAULT_FROM_EMAIL", "pandect@localhost")
if DEBUG:
    MAILERS = {"default": {"BACKEND": "django.core.mail.backends.console.EmailBackend"}}
else:
    MAILERS = {
        "default": {
            "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "OPTIONS": {
                "host": os.environ.get("DJANGO_EMAIL_HOST", "localhost"),
                "port": int(os.environ.get("DJANGO_EMAIL_PORT", "587")),
                "username": os.environ.get("DJANGO_EMAIL_HOST_USER", ""),
                "password": os.environ.get("DJANGO_EMAIL_HOST_PASSWORD", ""),
                "use_tls": True,
                "timeout": 10,
            },
        }
    }

# Operator credentials remain backend-only. Empty credentials disable inference.
PANDECT_AI_GATEWAY = {
    "account": os.environ.get("PANDECT_AI_ACCOUNT_ID", ""),
    "gateway": os.environ.get("PANDECT_AI_GATEWAY_ID", ""),
    "gateway_token": os.environ.get("PANDECT_AI_GATEWAY_TOKEN", ""),
    "workers_token": os.environ.get("PANDECT_AI_WORKERS_TOKEN", ""),
    "models": {
        "text": os.environ.get(
            "PANDECT_AI_TEXT_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
        ),
        "transcription": os.environ.get(
            "PANDECT_AI_TRANSCRIPTION_MODEL", "@cf/openai/whisper-large-v3-turbo"
        ),
    },
    "timeout": min(120, positive_env("PANDECT_AI_TIMEOUT", 60)),
    "daily_requests": positive_env("PANDECT_AI_DAILY_CASE_REQUESTS", 100),
    "max_request_bytes": positive_env("PANDECT_AI_MAX_REQUEST_BYTES", 8 * 1024 * 1024),
    "max_response_bytes": positive_env(
        "PANDECT_AI_MAX_RESPONSE_BYTES", 2 * 1024 * 1024
    ),
}
# Only explicitly configured, operator-controlled runtimes are local. Their
# mounted model digests and image revisions are checked before evidence is sent.
PANDECT_LOCAL_AI = {
    "text": {
        "url": os.environ.get("PANDECT_LOCAL_TEXT_URL", ""),
        "model": os.environ.get("PANDECT_LOCAL_TEXT_MODEL", "Qwen3-8B-Q4_K_M"),
        "sha256": os.environ.get("PANDECT_LOCAL_TEXT_SHA256", ""),
        "runtime": os.environ.get(
            "PANDECT_LOCAL_TEXT_RUNTIME",
            "llama.cpp@8642541b255bfb5dc39cd351a178be09013a55d6",
        ),
        "context_tokens": positive_env("PANDECT_LOCAL_TEXT_CONTEXT_TOKENS", 32768),
    },
    "transcription": {
        "url": os.environ.get("PANDECT_LOCAL_TRANSCRIPTION_URL", ""),
        "model": os.environ.get(
            "PANDECT_LOCAL_TRANSCRIPTION_MODEL", "whisper-large-v3-turbo"
        ),
        "sha256": os.environ.get("PANDECT_LOCAL_TRANSCRIPTION_SHA256", ""),
        "runtime": os.environ.get(
            "PANDECT_LOCAL_TRANSCRIPTION_RUNTIME",
            "whisper.cpp@d1be6fde11ac6e0407606b4e42fe72d34add8037",
        ),
    },
    "token": os.environ.get("PANDECT_LOCAL_AI_TOKEN", ""),
    "timeout": min(1800, positive_env("PANDECT_LOCAL_AI_TIMEOUT", 600)),
    "max_request_bytes": positive_env(
        "PANDECT_LOCAL_AI_MAX_REQUEST_BYTES", 8 * 1024 * 1024
    ),
    "max_response_bytes": positive_env(
        "PANDECT_LOCAL_AI_MAX_RESPONSE_BYTES", 2 * 1024 * 1024
    ),
    "max_output_tokens": positive_env("PANDECT_LOCAL_AI_MAX_OUTPUT_TOKENS", 4096),
}
PANDECT_ANALYSIS_MAX_SOURCES = 500
PANDECT_ANALYSIS_MAX_BYTES = 16 * 1024 * 1024
PANDECT_ANALYSIS_MAX_TEXT = 2 * 1024 * 1024
PANDECT_GEOCODER_URL = os.environ.get("PANDECT_GEOCODER_URL", "")
PANDECT_GEOCODER_LOCAL = (
    os.environ.get("PANDECT_GEOCODER_LOCAL", "false").lower() == "true"
)
PANDECT_GEOCODER_NAME = os.environ.get("PANDECT_GEOCODER_NAME", "")
PANDECT_GEOCODER_VERSION = os.environ.get("PANDECT_GEOCODER_VERSION", "")
PANDECT_GEOCODER_ATTRIBUTION = os.environ.get("PANDECT_GEOCODER_ATTRIBUTION", "")
PANDECT_GEOCODER_USER_AGENT = os.environ.get(
    "PANDECT_GEOCODER_USER_AGENT", "Pandect/0.1"
)

PANDECT_GEOCODER_TIMEOUT = min(30, positive_env("PANDECT_GEOCODER_TIMEOUT", 10))

# Reference context is a separate configured lookup, not an inference endpoint.
PANDECT_MAP_CONTEXT_URL = os.environ.get("PANDECT_MAP_CONTEXT_URL", "")
PANDECT_MAP_CONTEXT_LOCAL = (
    os.environ.get("PANDECT_MAP_CONTEXT_LOCAL", "false").lower() == "true"
)
PANDECT_MAP_CONTEXT_NAME = os.environ.get("PANDECT_MAP_CONTEXT_NAME", "")
PANDECT_MAP_CONTEXT_VERSION = os.environ.get("PANDECT_MAP_CONTEXT_VERSION", "")
PANDECT_MAP_CONTEXT_ATTRIBUTION = os.environ.get("PANDECT_MAP_CONTEXT_ATTRIBUTION", "")
PANDECT_MAP_CONTEXT_USER_AGENT = os.environ.get(
    "PANDECT_MAP_CONTEXT_USER_AGENT", "Pandect/0.1"
)
PANDECT_MAP_CONTEXT_TIMEOUT = min(30, positive_env("PANDECT_MAP_CONTEXT_TIMEOUT", 10))
PANDECT_MAP_CONTEXT_HISTORY_START = os.environ.get(
    "PANDECT_MAP_CONTEXT_HISTORY_START", ""
)
