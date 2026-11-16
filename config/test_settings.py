"""Isolated tests: fast password fixtures and source assets without a manifest."""

from .settings import *  # noqa: F403
from .settings import PANDECT_AI_GATEWAY, STORAGES

# Deliberately fast for synthetic test users; production settings are unchanged.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Routine tests never inherit live gateway destinations or credentials from .env.
# Provider tests opt in with synthetic configuration and mocked HTTP transport.
PANDECT_AI_GATEWAY = PANDECT_AI_GATEWAY | {
    "account": "",
    "gateway": "",
    "gateway_token": "",
    "workers_token": "",
    "text_provider": "workers-ai",
    "openai_key_alias": "default",
    "models": {
        "text": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "transcription": "@cf/openai/whisper-large-v3-turbo",
    },
}

STORAGES = {
    **STORAGES,
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
