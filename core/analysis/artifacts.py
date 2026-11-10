"""The two supported private derived-file profiles and their publication bounds."""

import base64
import json
import re

from core.analysis.errors import AnalysisError
from core.models import AnalysisArtifact

MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_TOTAL_BYTES = MAX_ARTIFACT_BYTES
MAX_REFERENCE_TOTAL_BYTES = 8 * 1024 * 1024


def _invalid_constant(value):
    raise ValueError("Non-finite JSON value")


def decode_artifact(payload, source_ids):
    """Validate a concrete preview or reference response before retaining bytes."""
    try:
        kind = payload.get("kind", AnalysisArtifact.Kind.IMAGE_PREVIEW)
        raw = base64.b64decode(payload["data_base64"], validate=True)
        if not raw or len(raw) > MAX_ARTIFACT_BYTES:
            raise ValueError
        source_id = str(payload["source_id"])
        if source_id not in source_ids:
            raise ValueError
        values = {"source_id": source_id, "kind": kind}
        if kind == AnalysisArtifact.Kind.IMAGE_PREVIEW:
            if payload["content_type"] != "image/jpeg" or not raw.startswith(
                b"\xff\xd8\xff"
            ):
                raise ValueError
            if not all(
                type(payload.get(key)) is int and 1 <= payload[key] <= 4096
                for key in ("width", "height")
            ):
                raise ValueError
            values |= {key: payload[key] for key in ("width", "height")}
            if "metadata" in payload:
                metadata = payload["metadata"]
                if (
                    not isinstance(metadata, dict)
                    or set(metadata) != {"finding_index"}
                    or type(metadata["finding_index"]) is not int
                    or not 0 <= metadata["finding_index"] < 10000
                ):
                    raise ValueError
                values["metadata"] = metadata
        elif kind == AnalysisArtifact.Kind.GEOGRAPHIC_REFERENCE:
            if payload["content_type"] != "application/json" or any(
                payload.get(key) is not None for key in ("width", "height")
            ):
                raise ValueError
            if not isinstance(json.loads(raw, parse_constant=_invalid_constant), dict):
                raise ValueError
            metadata = payload["metadata"]
            if (
                not isinstance(metadata, dict)
                or len(json.dumps(metadata, allow_nan=False).encode()) > 32768
            ):
                raise ValueError
            if not re.fullmatch(r"context-[1-9][0-9]{0,2}", metadata["reference_key"]):
                raise ValueError
            members = metadata["source_ids"]
            if (
                not isinstance(members, list)
                or not members
                or not all(isinstance(x, str) and x in source_ids for x in members)
                or source_id not in members
            ):
                raise ValueError
            for key, limit in (
                ("query", 8192),
                ("endpoint", 2048),
                ("source_name", 255),
                ("reference_version", 255),
                ("attribution", 2000),
                ("retrieved_at", 100),
            ):
                if (
                    not isinstance(metadata[key], str)
                    or not 1 <= len(metadata[key]) <= limit
                ):
                    raise ValueError
            for key in ("requested_as_of", "dataset_timestamp"):
                if metadata.get(key) is not None and (
                    not isinstance(metadata[key], str) or len(metadata[key]) > 100
                ):
                    raise ValueError
            values["metadata"] = metadata
        else:
            raise ValueError
    except KeyError, ValueError, TypeError, AttributeError, RecursionError:
        raise AnalysisError("invalid_artifact") from None
    return raw, values
