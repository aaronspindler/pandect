"""Isolated tests: fast password fixtures and source assets without a manifest."""

from .settings import *  # noqa: F403
from .settings import INSTALLED_APPS, MIDDLEWARE, PANDECT_AI_GATEWAY, STORAGES

DEBUG = False
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "debug_toolbar"]
MIDDLEWARE = [
    path
    for path in MIDDLEWARE
    if path != "debug_toolbar.middleware.DebugToolbarMiddleware"
]

# Deliberately fast for synthetic test users; production settings are unchanged.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Routine tests never inherit live analysis destinations or credentials.
# Provider tests opt in with synthetic configuration and mocked HTTP transport.
PANDECT_CLOUDFLARE_SEARCH = {"account": "", "namespace": "test", "token": ""}
PANDECT_CLOUDFLARE_AGENT = {"url": "", "secret": ""}
PANDECT_AI_PRICES_USD_PER_MILLION = {}
PANDECT_AI_GATEWAY = PANDECT_AI_GATEWAY | {
    "account": "",
    "gateway": "",
    "gateway_token": "",
    "workers_token": "",
    "unified_billing": False,
    "stored_key_providers": (),
    "text_provider": "workers-ai",
    "openai_key_alias": "default",
    "model_choices": {},
    "models": {
        "text": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "transcription": "@cf/openai/whisper-large-v3-turbo",
    },
}

STORAGES = {
    **STORAGES,
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

PANDECT_GEOCODER_URL = ""
PANDECT_GEOCODER_LOCAL = False
PANDECT_MAP_CONTEXT_URL = ""
PANDECT_MAP_CONTEXT_LOCAL = False
PANDECT_MAP_TILE_URL = ""
PANDECT_MAP_TILE_ORIGIN = ""

PANDECT_INSTAGRAM_PILOT_URL = ""
PANDECT_INSTAGRAM_PILOT_KEY = ""

PANDECT_INSTAGRAM_LEGACY_REVOCATION = {}
PANDECT_REQUEST_ACTIVITY_ENABLED = False
