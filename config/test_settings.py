"""Test settings: serve source assets without a release-time static manifest."""

from .settings import *  # noqa: F403
from .settings import STORAGES

STORAGES = {
    **STORAGES,
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
