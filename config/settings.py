"""Shared Django configuration for local and operator-hosted Pandect."""

import os
import re
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from django.core.exceptions import ImproperlyConfigured
from django.http.request import split_domain_port

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY before starting Pandect.")

DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
APP_VERSION = os.environ.get("APP_VERSION", "dev")
APP_COMMIT_SHA = os.environ.get("APP_COMMIT_SHA", "unknown")
PANDECT_PUBLIC_ORIGIN = os.environ.get(
    "PANDECT_PUBLIC_ORIGIN", "http://localhost:8000"
).rstrip("/")
try:
    origin = urlsplit(PANDECT_PUBLIC_ORIGIN)
    public_host, _ = split_domain_port(origin.netloc)
    public_port = origin.port
except ValueError as error:
    raise ImproperlyConfigured(
        "PANDECT_PUBLIC_ORIGIN must be an HTTP(S) origin."
    ) from error
if (
    origin.scheme not in {"http", "https"}
    or not public_host
    or origin.username is not None
    or origin.password is not None
    or origin.path
    or origin.query
    or origin.fragment
    or any(character.isspace() for character in PANDECT_PUBLIC_ORIGIN)
):
    raise ImproperlyConfigured("PANDECT_PUBLIC_ORIGIN must be an HTTP(S) origin.")
default_hosts = list(dict.fromkeys([public_host, "localhost", "127.0.0.1", "[::1]"]))
ALLOWED_HOSTS = [
    host.strip()
    for host in (
        os.environ.get("DJANGO_ALLOWED_HOSTS") or ",".join(default_hosts)
    ).split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.postgres",
    "whitenoise.runserver_nostatic",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_tasks_db",
    "accounts",
    "cases",
    "core.apps.CoreConfig",
    "people",
    "vehicle",
    "text",
    "image.apps.ImageConfig",
    "audio.apps.AudioConfig",
    "document.apps.DocumentConfig",
    "geo",
    "phone.apps.PhoneConfig",
    "socialmedia",
    "video",
    "marketing",
    "ui",
    "allauth",
    "allauth.account",
]
MIDDLEWARE = [
    "config.streaming.AsyncStreamingMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "config.access.ProductUploadAccessMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.middleware.TimezoneMiddleware",
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
# Authentication limits must be shared by every web process. The table uses
# Django's upstream cache schema and is created by an accounts migration.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "pandect_authentication_cache",
        "OPTIONS": {"MAX_ENTRIES": 100000},
    }
}
# Use the durable queue in development too; DEBUG must not run work inline.
TASKS = {
    "default": {
        "BACKEND": "django_tasks_db.DatabaseBackend",
        "QUEUES": ["default", "instagram"],
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
ACCOUNT_FORMS = {"login": "accounts.forms.PandectLoginForm"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "username*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_ADAPTER = "accounts.adapter.PandectAccountAdapter"
# Full CSV verification is deliberate work; summaries and progress do not consume it.
ACCOUNT_RATE_LIMITS = {"imessage_row_review": "10/m/user,30/m/key"}
# Opt in only when the application is reachable through a controlled proxy chain.
try:
    ALLAUTH_TRUSTED_PROXY_COUNT = int(
        os.environ.get("PANDECT_TRUSTED_PROXY_COUNT", "0")
    )
except ValueError as error:
    raise ImproperlyConfigured(
        "PANDECT_TRUSTED_PROXY_COUNT must be a nonnegative integer."
    ) from error
if ALLAUTH_TRUSTED_PROXY_COUNT < 0:
    raise ImproperlyConfigured(
        "PANDECT_TRUSTED_PROXY_COUNT must be a nonnegative integer."
    )
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
SECURE_HSTS_SECONDS = int(
    os.environ.get("DJANGO_SECURE_HSTS_SECONDS")
    or ("86400" if origin.scheme == "https" else "0")
)
if SECURE_HSTS_SECONDS < 0:
    raise ImproperlyConfigured("DJANGO_SECURE_HSTS_SECONDS must not be negative.")
CSRF_TRUSTED_ORIGINS = [
    value.strip()
    for value in (
        os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS") or PANDECT_PUBLIC_ORIGIN
    ).split(",")
    if value.strip()
]


# Product policy lives in code; deployment-specific connections stay in the environment.
PANDECT_MAX_FILE_BYTES = 1024 * 1024 * 1024
PANDECT_MAX_TEXT_BYTES = 10 * 1024 * 1024
PANDECT_MAX_BATCH_FILES = 100
PANDECT_INVITATION_DAYS = 7
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

sender_domain = origin.hostname
try:
    ip_address(sender_domain)
except ValueError:
    if sender_domain.endswith(".localhost"):
        sender_domain = "localhost"
else:
    sender_domain = "localhost"
DEFAULT_FROM_EMAIL = (
    os.environ.get("DJANGO_DEFAULT_FROM_EMAIL") or f"pandect@{sender_domain}"
)
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

# A single direct key is enough to configure text inference. Explicit provider
# configuration takes precedence, including on existing Gateway installations.
PANDECT_DIRECT_AI = {
    "openai_api_key": os.environ.get("OPENAI_API_KEY", "").strip(),
    "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", "").strip(),
    "openai_model": os.environ.get("PANDECT_OPENAI_MODEL") or "gpt-6-luna",
    "anthropic_model": os.environ.get("PANDECT_ANTHROPIC_MODEL") or "claude-haiku-5-5",
}
direct_text_providers = [
    f"{provider}-direct"
    for provider in ("openai", "anthropic")
    if PANDECT_DIRECT_AI[f"{provider}_api_key"]
]
default_text_provider = "workers-ai"
if len(direct_text_providers) == 1:
    default_text_provider = direct_text_providers[0]
elif direct_text_providers:
    default_text_provider = ""

PANDECT_AI_GATEWAY = {
    "account": os.environ.get("PANDECT_AI_ACCOUNT_ID", ""),
    "gateway": os.environ.get("PANDECT_AI_GATEWAY_ID", ""),
    "gateway_token": os.environ.get("PANDECT_AI_GATEWAY_TOKEN", ""),
    "workers_token": os.environ.get("PANDECT_AI_WORKERS_TOKEN", ""),
    "text_provider": os.environ.get("PANDECT_AI_TEXT_PROVIDER")
    or default_text_provider,
    "openai_key_alias": os.environ.get("PANDECT_AI_OPENAI_KEY_ALIAS", "default"),
    "models": {
        "text": os.environ.get("PANDECT_AI_TEXT_MODEL", "auto"),
        "transcription": os.environ.get(
            "PANDECT_AI_TRANSCRIPTION_MODEL", "@cf/openai/whisper-large-v3-turbo"
        ),
    },
    "model_choices": {
        purpose: tuple(
            model.strip()
            for model in os.environ.get(variable, "").split(",")
            if model.strip()
        )
        for purpose, variable in (
            ("text", "PANDECT_AI_TEXT_MODEL_CHOICES"),
            ("transcription", "PANDECT_AI_TRANSCRIPTION_MODEL_CHOICES"),
        )
    },
    "timeout": 60,
    "max_request_bytes": 8 * 1024 * 1024,
    "max_response_bytes": 2 * 1024 * 1024,
}
# Only explicitly configured, operator-controlled runtimes are local. Their
# model and runtime identities are checked before evidence is sent.
PANDECT_LOCAL_AI = {
    "text": {
        "url": os.environ.get("PANDECT_LOCAL_TEXT_URL", ""),
        "model": os.environ.get("PANDECT_LOCAL_TEXT_MODEL", "qwen3.5:9b"),
        "sha256": os.environ.get("PANDECT_LOCAL_TEXT_SHA256", ""),
        "runtime": os.environ.get("PANDECT_LOCAL_TEXT_RUNTIME", ""),
        "context_tokens": 16384,
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
    "timeout": 600,
    "max_request_bytes": 8 * 1024 * 1024,
    "max_response_bytes": 2 * 1024 * 1024,
    "max_output_tokens": 4096,
}
PANDECT_PREFER_EXTERNAL_TRANSCRIPTION_PROFILE = os.environ.get(
    "PANDECT_PREFER_EXTERNAL_TRANSCRIPTION_PROFILE", ""
)
PANDECT_SEARCH_EMBEDDING_PROVIDER = os.environ.get(
    "PANDECT_SEARCH_EMBEDDING_PROVIDER", "local"
)
PANDECT_LOCAL_EMBEDDING = {
    "url": os.environ.get("PANDECT_LOCAL_EMBEDDING_URL", ""),
    "model": os.environ.get("PANDECT_LOCAL_EMBEDDING_MODEL", "all-minilm:22m"),
    "sha256": os.environ.get("PANDECT_LOCAL_EMBEDDING_SHA256", ""),
    "runtime": os.environ.get("PANDECT_LOCAL_EMBEDDING_RUNTIME", ""),
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

PANDECT_GEOCODER_TIMEOUT = 10

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
PANDECT_MAP_CONTEXT_TIMEOUT = 10
PANDECT_MAP_CONTEXT_HISTORY_START = os.environ.get(
    "PANDECT_MAP_CONTEXT_HISTORY_START", ""
)

# Optional offline Instagram pilot using the isolated synthetic browser.
PANDECT_INSTAGRAM_PILOT_URL = os.environ.get("PANDECT_INSTAGRAM_PILOT_URL", "")
PANDECT_INSTAGRAM_PILOT_KEY = os.environ.get("PANDECT_INSTAGRAM_PILOT_KEY", "")
if PANDECT_INSTAGRAM_PILOT_URL:
    _pilot = urlsplit(PANDECT_INSTAGRAM_PILOT_URL)
    if (
        _pilot.scheme != "http"
        or _pilot.hostname not in {"instagram-pilot", "127.0.0.1", "localhost", "::1"}
        or _pilot.username is not None
        or _pilot.password is not None
        or _pilot.path not in {"", "/"}
        or _pilot.query
        or _pilot.fragment
        or len(PANDECT_INSTAGRAM_PILOT_KEY) < 32
    ):
        raise ImproperlyConfigured(
            "Use the private Instagram pilot service or loopback and a "
            "dedicated pilot key of at least 32 characters."
        )

# An optional single-investigator qualification on the local desktop. Loopback
# avoids transmitting the private connector binding outside this machine.
PANDECT_INSTAGRAM_LIVE = {}
if _live_url := os.environ.get("PANDECT_INSTAGRAM_LIVE_URL", ""):
    try:
        _live = urlsplit(_live_url)
        _target_text = os.environ.get("PANDECT_INSTAGRAM_LIVE_TARGETS", "")
        _targets = tuple(
            dict.fromkeys(h.strip().lower() for h in _target_text.split(","))
        )
        _observer = (
            os.environ.get("PANDECT_INSTAGRAM_LIVE_OBSERVER", "").strip().lower()
        )
        _live_token = os.environ.get("PANDECT_INSTAGRAM_LIVE_TOKEN", "")
        _minimum = int(os.environ.get("PANDECT_INSTAGRAM_LIVE_MIN_HOURS", "1"))
        if (
            PANDECT_INSTAGRAM_PILOT_URL
            or _live.scheme != "http"
            or _live.hostname != "127.0.0.1"
            or not _live.port
            or _live_url != f"http://127.0.0.1:{_live.port}"
            or _minimum not in (1, 6, 12, 24)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", _live_token)
            or (_observer and not re.fullmatch(r"[a-z0-9._]{1,30}", _observer))
            or not all(re.fullmatch(r"[a-z0-9._]{1,30}", h) for h in _targets)
        ):
            raise ValueError
        PANDECT_INSTAGRAM_LIVE = {
            "url": _live_url,
            "token": _live_token,
            "targets": _targets,
            "observer": _observer,
            "min_hours": _minimum,
            "user_id": str(UUID(os.environ["PANDECT_INSTAGRAM_LIVE_USER"])),
            "case_id": str(UUID(os.environ["PANDECT_INSTAGRAM_LIVE_CASE"])),
        }
    except KeyError, ValueError:
        raise ImproperlyConfigured(
            "Check the local Instagram pilot configuration."
        ) from None
