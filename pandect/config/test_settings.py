"""Hermetic settings used by the automated test suite."""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="pandect-tests-")
atexit.register(shutil.rmtree, _TEST_DATA_DIR, ignore_errors=True)
os.environ["PANDECT_DATA_DIR"] = _TEST_DATA_DIR
os.environ["PANDECT_SECRET_KEY"] = "pandect-hermetic-test-secret"

from pandect.config.settings import *  # noqa: E402,F403

HUEY = {**HUEY, "immediate": True}  # noqa: F405
STATIC_ROOT = Path(_TEST_DATA_DIR) / "staticfiles"
STATIC_ROOT.mkdir()
STORAGES = {
    **STORAGES,  # noqa: F405
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
