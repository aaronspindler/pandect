"""Isolated tests: fast password fixtures and source assets without a manifest."""

from .settings import *  # noqa: F403
from .settings import STORAGES

# Deliberately fast for synthetic test users; production settings are unchanged.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

STORAGES = {
    **STORAGES,
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
