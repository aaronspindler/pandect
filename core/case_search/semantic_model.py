"""Meaning search profiles and bounded, case-authorized embedding requests."""

import hashlib
import json
import math
import re

from django.conf import settings

from core.analysis.errors import AnalysisError
from core.analysis.local import LocalClient, validate_local_configuration
from core.analysis.ollama import _version, pinned_model

LOCAL_DIMENSIONS = DIMENSIONS = 384
HOSTED_DIMENSIONS = 1536
BATCH_SIZE = 32
# A UTF-8 byte is a conservative upper bound on one WordPiece. Reserve room
# for the model's special tokens even in long identifiers or non-English text.
CHUNK_BYTES = 246


def is_external():
    return settings.PANDECT_SEARCH_EMBEDDING_PROVIDER == "openai-gateway"


def execution():
    if is_external():
        gateway = settings.PANDECT_AI_GATEWAY
        return {
            "provider": "openai",
            "backend": "gateway",
            "account": gateway.get("account", ""),
            "gateway": gateway.get("gateway", ""),
            "key_alias": gateway.get("openai_key_alias", "default"),
            "model": "text-embedding-3-large",
            "purpose": "embedding",
            "timeout": 15,
            "max_request_bytes": 48 * 1024,
            "max_response_bytes": 2 * 1024 * 1024,
        }
    if settings.PANDECT_SEARCH_EMBEDDING_PROVIDER != "local":
        return {}
    config = settings.PANDECT_LOCAL_EMBEDDING
    return {
        **config,
        "backend": "local",
        "purpose": "embedding",
        "driver": "ollama",
        "timeout": 10,
        "max_request_bytes": 48 * 1024,
        "max_response_bytes": 512 * 1024,
        "max_output_tokens": 1,
    }


def identity(config):
    if not config:
        return {}
    common = {
        "chunker": "utf8-word-boundary-1",
        "chunk_bytes": CHUNK_BYTES,
    }
    if config["backend"] == "gateway":
        return common | {
            "provider": "openai-gateway",
            **{
                key: config[key] for key in ("account", "gateway", "key_alias", "model")
            },
            "dimensions": HOSTED_DIMENSIONS,
        }
    return common | {
        **{key: config.get(key, "") for key in ("model", "sha256", "runtime")},
        "dimensions": LOCAL_DIMENSIONS,
    }


def _configured_profile(config):
    if config.get("backend") == "gateway":
        if not (
            re.fullmatch(r"[a-fA-F0-9]{32}", config["account"])
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", config["gateway"])
            and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", config["key_alias"])
            and re.fullmatch(
                r"[!-~]+", settings.PANDECT_AI_GATEWAY.get("gateway_token", "")
            )
        ):
            return ""
        public = identity(config)
    else:
        try:
            validate_local_configuration(config)
        except AnalysisError:
            return ""
        # The private runtime URL is part of existing local profile hashes.
        public = [config["url"], identity(config)]
    payload = json.dumps(public, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def profile():
    return _configured_profile(execution())


def valid_vector(vector, dimensions):
    return (
        isinstance(vector, list)
        and len(vector) == dimensions
        and all(type(n) in (int, float) and -1.001 <= n <= 1.001 for n in vector)
        and math.isclose(math.hypot(*vector), 1, abs_tol=0.01)
    )


def embed(texts, authorize, *, case=None, purpose="index"):
    config = execution()
    if config.get("backend") == "gateway":
        from .hosted_embeddings import request_embeddings

        if not _configured_profile(config):
            raise AnalysisError("gateway_not_configured")
        if not 0 < len(texts) <= BATCH_SIZE:
            raise AnalysisError("gateway_input_limit")

        def check_access():
            current = authorize()
            if case is None or current is None or current.pk != case.pk:
                raise AnalysisError("access_removed")
            if config != execution() or not _configured_profile(config):
                raise AnalysisError("execution_changed")

        return request_embeddings(texts, config, check_access, case, purpose)
    if not config:
        raise AnalysisError("local_ai_not_configured")
    if not 0 < len(texts) <= BATCH_SIZE:
        raise AnalysisError("local_ai_input_limit")
    client = LocalClient(authorize, config)
    model = pinned_model(client, capability="embedding", families={"bert"})
    result = client._request(
        "/api/embed",
        {"model": model, "input": texts, "truncate": False, "keep_alive": "5m"},
    )
    _version(client)
    if (
        result.get("model") != model
        or result.get("remote_model")
        or result.get("remote_host")
        or not isinstance(result.get("embeddings"), list)
        or len(result["embeddings"]) != len(texts)
    ):
        raise AnalysisError("local_ai_invalid_response")
    for vector in result["embeddings"]:
        if not valid_vector(vector, LOCAL_DIMENSIONS):
            raise AnalysisError("local_ai_invalid_response")
    return result["embeddings"]


def next_chunk(text):
    """Return a verbatim prefix; prefer a word boundary without losing bytes."""
    bounded = text.encode()[:CHUNK_BYTES].decode(errors="ignore")
    if len(bounded) < len(text):
        boundary = max(bounded.rfind(" "), bounded.rfind("\n"))
        if boundary > len(bounded) // 2:
            bounded = bounded[: boundary + 1]
    return bounded
