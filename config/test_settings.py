"""Isolated tests: fast password fixtures and source assets without a manifest."""

from .settings import *  # noqa: F403
from .settings import PANDECT_AI_GATEWAY, STORAGES

# Deliberately fast for synthetic test users; production settings are unchanged.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Routine tests never inherit live analysis destinations or credentials.
# Provider tests opt in with synthetic configuration and mocked HTTP transport.
PANDECT_DIRECT_AI = {}
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

PANDECT_LOCAL_AI = {}
PANDECT_SEARCH_EMBEDDING_PROVIDER = "local"
PANDECT_LOCAL_EMBEDDING = {}
PANDECT_SEARCH_RERANK_PROVIDER = "disabled"
PANDECT_LOCAL_RERANK = {}
PANDECT_GEOCODER_URL = ""
PANDECT_GEOCODER_LOCAL = False
PANDECT_MAP_CONTEXT_URL = ""
PANDECT_MAP_CONTEXT_LOCAL = False
PANDECT_MAP_TILE_URL = ""
PANDECT_MAP_TILE_ORIGIN = ""

PANDECT_INSTAGRAM_PILOT_URL = ""
PANDECT_INSTAGRAM_PILOT_KEY = ""

PANDECT_INSTAGRAM_LEGACY_REVOCATION = {}
