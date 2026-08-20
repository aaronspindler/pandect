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

# Never let credentials or remote-processing switches from the invoking shell
# leak into the hermetic suite. Tests that exercise consented remote workflows
# opt in explicitly and use an in-memory HTTP transport.
CLOUDFLARE_AI_ENABLED = False
CLOUDFLARE_ACCOUNT_ID = ""
CLOUDFLARE_API_TOKEN = ""
CLOUDFLARE_GATEWAY_ID = "pandect"
CLOUDFLARE_TEXT_MODEL = "@cf/meta/llama-3.2-11b-vision-instruct"
CLOUDFLARE_VISION_MODEL = "@cf/meta/llama-3.2-11b-vision-instruct"
CLOUDFLARE_TRANSCRIPTION_MODEL = "@cf/deepgram/nova-3"
CLOUDFLARE_CLIENT_TIMEOUT_SECONDS = 300

HUEY = {**HUEY, "immediate": True}  # noqa: F405
STATIC_ROOT = Path(_TEST_DATA_DIR) / "staticfiles"
STATIC_ROOT.mkdir()
STORAGES = {
    **STORAGES,  # noqa: F405
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
