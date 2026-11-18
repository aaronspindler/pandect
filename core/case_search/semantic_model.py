"""Pinned, local-only embedding requests with bounded CPU-model batches."""

import hashlib
import json
import math

from django.conf import settings

from core.analysis.errors import AnalysisError
from core.analysis.local import LocalClient, validate_local_configuration
from core.analysis.ollama import _version, pinned_model

DIMENSIONS = 384
BATCH_SIZE = 32
# A UTF-8 byte is a conservative upper bound on one WordPiece. Reserve room
# for the model's special tokens even in long identifiers or non-English text.
CHUNK_BYTES = 246


def execution():
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
    return {
        **{key: config[key] for key in ("model", "sha256", "runtime")},
        "chunker": "utf8-word-boundary-1",
        "chunk_bytes": CHUNK_BYTES,
        "dimensions": DIMENSIONS,
    }


def profile():
    config = execution()
    try:
        validate_local_configuration(config)
    except AnalysisError:
        return ""
    payload = json.dumps([config["url"], identity(config)], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def embed(texts, authorize):
    if not 0 < len(texts) <= BATCH_SIZE:
        raise AnalysisError("local_ai_input_limit")
    client = LocalClient(authorize, execution())
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
        if (
            not isinstance(vector, list)
            or len(vector) != DIMENSIONS
            or any(
                type(n) not in (int, float) or not -1.001 <= n <= 1.001 for n in vector
            )
            or not math.isclose(math.hypot(*vector), 1, abs_tol=0.01)
        ):
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
