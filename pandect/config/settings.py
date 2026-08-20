"""Django settings for Pandect's trusted, single-machine runtime."""

import os
from pathlib import Path

from pandect.config.environment import (
    boolean,
    comma_separated,
    create_data_directory,
    load_secret_key,
)

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = create_data_directory(BASE_DIR)

SECRET_KEY = load_secret_key(DATA_DIR)
DEBUG = boolean("PANDECT_DEBUG", default=True)
ALLOWED_HOSTS = comma_separated(
    "PANDECT_ALLOWED_HOSTS",
    default=("localhost", "127.0.0.1", "[::1]"),
)

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "huey.contrib.djhuey",
    "pandect.catalog.apps.CatalogConfig",
    "pandect.audio.apps.AudioConfig",
    "pandect.identities.apps.IdentitiesConfig",
    "pandect.processing.apps.ProcessingConfig",
    "pandect.events.apps.EventsConfig",
    "pandect.messages.apps.MessagesConfig",
    "pandect.notes.apps.NotesConfig",
    "pandect.photos.apps.PhotosConfig",
    "pandect.videos.apps.VideosConfig",
    "pandect.core.apps.CoreConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "pandect.config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csrf",
            ],
        },
    },
]

WSGI_APPLICATION = "pandect.config.wsgi.application"

APPLICATION_DATABASE_PATH = DATA_DIR / "application.sqlite3"
HUEY_DATABASE_PATH = DATA_DIR / "huey.sqlite3"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": APPLICATION_DATABASE_PATH,
        "OPTIONS": {"timeout": 5},
    }
}

HUEY = {
    "huey_class": "huey.SqliteHuey",
    "name": "pandect",
    "filename": str(HUEY_DATABASE_PATH),
    "results": True,
    "store_none": False,
    # Keep web requests asynchronous even when local DEBUG is enabled.
    "immediate": False,
    "consumer": {
        "workers": 1,
        "worker_type": "thread",
        "periodic": True,
        "check_worker_health": True,
        "health_check_interval": 10,
    },
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("PANDECT_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = DATA_DIR / "media"
WORK_ROOT = Path(os.environ.get("PANDECT_WORK_DIR", DATA_DIR / "work")).resolve()
MODELS_ROOT = Path(os.environ.get("PANDECT_MODELS_DIR", DATA_DIR / "models")).resolve()
for private_directory in (WORK_ROOT, MODELS_ROOT):
    private_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
# Django streams uploads larger than FILE_UPLOAD_MAX_MEMORY_SIZE to this private,
# persistent workspace. Evidence uploads must not spill into the small /tmp tmpfs.
FILE_UPLOAD_TEMP_DIR = WORK_ROOT / "uploads"
FILE_UPLOAD_TEMP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)

PROCESSING_MAX_IMAGE_PIXELS = 200_000_000
PROCESSING_MAX_IMAGE_FRAMES = 100
PROCESSING_MAX_VIDEO_FRAMES = 120
PROCESSING_MAX_REMOTE_VIDEO_FRAMES = 12
PROCESSING_SUBPROCESS_OUTPUT_LIMIT = 16 * 1024 * 1024

CLOUDFLARE_AI_ENABLED = boolean("PANDECT_CLOUDFLARE_AI_ENABLED", default=False)
CLOUDFLARE_ACCOUNT_ID = os.environ.get("PANDECT_CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN = os.environ.get("PANDECT_CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_GATEWAY_ID = os.environ.get("PANDECT_CLOUDFLARE_GATEWAY_ID", "pandect")
CLOUDFLARE_TEXT_MODEL = os.environ.get(
    "PANDECT_CLOUDFLARE_TEXT_MODEL",
    "@cf/meta/llama-3.2-11b-vision-instruct",
)
CLOUDFLARE_VISION_MODEL = os.environ.get(
    "PANDECT_CLOUDFLARE_VISION_MODEL",
    "@cf/meta/llama-3.2-11b-vision-instruct",
)
CLOUDFLARE_TRANSCRIPTION_MODEL = os.environ.get(
    "PANDECT_CLOUDFLARE_TRANSCRIPTION_MODEL", "@cf/deepgram/nova-3"
)
CLOUDFLARE_CLIENT_TIMEOUT_SECONDS = int(
    os.environ.get("PANDECT_CLOUDFLARE_CLIENT_TIMEOUT_SECONDS", "300")
)
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "{asctime} {levelname} {name}: {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    # The Huey consumer installs its own task-aware handler.
    "loggers": {"huey": {"level": "INFO", "propagate": False}},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
X_FRAME_OPTIONS = "DENY"
