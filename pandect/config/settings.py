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
