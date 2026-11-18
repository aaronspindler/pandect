"""Pure, versioned projections of immutable analysis snapshots."""

import hashlib
import json
import math
import re
from urllib.parse import urlsplit, urlunsplit

from core.analysis.review_signals import review_fields_v1


def is_timing_only(operation, item):
    """Recognize only known adjacency observations without a content/reply cue."""
    if (
        operation != "conversation_link"
        or not isinstance(item, dict)
        or item.get("label") != "Candidate exchange sequence"
    ):
        return False
    values = item.get("values")
    if not isinstance(values, dict):
        return False
    elapsed = values.get("elapsed_seconds")
    return (
        "status" not in values
        and isinstance(values.get("before"), dict)
        and isinstance(values.get("after"), dict)
        and type(elapsed) in (int, float)
        and (type(elapsed) is int or math.isfinite(elapsed))
        and elapsed >= 0
        and values.get("shared_words") == []
        and not values["before"].get("reply_to_id")
        and not values["after"].get("reply_to_id")
    )


def is_ai_suggestion(operation, finding):
    """Distinguish model output from observations in mixed-method results."""
    values = finding.get("values", {})
    if operation == "question_synthesis":
        return "section" in values
    if operation in {
        "content_events",
        "session_summaries",
        "text_topics",
        "conversation_link",
    }:
        return "status" in values
    return operation == "transcription" and bool(finding.get("text"))


def equivalence_key(run):
    """Order and exact retained versions are meaningful analysis inputs."""
    selection_keys = (
        "kind",
        "evidence_id",
        "attachment_id",
        "result_id",
        "geocode_candidate",
        "review_id",
        "sha256",
    )
    value = {
        "version": 1,
        "operation": run.operation,
        "method_version": run.method_version,
        "external_processing": run.external_processing,
        "parameters": {
            key: value
            for key, value in run.parameters.items()
            if key not in {"automatic_batch", "automatic_followup"}
        },
        "inputs": [
            {key: source[key] for key in selection_keys if key in source}
            for source in run.inputs
        ],
    }
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def scalar_text(value):
    if isinstance(value, dict):
        return "\n".join(f"{key}: {scalar_text(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(scalar_text(item) for item in value)
    return "" if value is None else str(value)


def identifier_key(operation, item):
    """Comparison candidates only; no entity resolution or country inference."""
    values = item.get("values", {})
    label = item.get("label", "")
    raw = values.get("raw", item.get("text", ""))
    if not isinstance(raw, str):
        return "", ""
    if operation == "plate_ocr":
        if values.get("Weak plate region") or not item.get("text"):
            return "", ""
        key = "".join(c.upper() for c in raw if c.isascii() and c.isalnum())
        return ("plate", key) if 3 <= len(key) <= 12 else ("", "")
    if operation != "entities":
        return "", ""
    if label == "Email" and "@" in raw:
        local, domain = raw.rsplit("@", 1)
        return "email", f"{local}@{domain.lower()}"
    if label == "Domain candidate":
        return "domain", raw.lower()
    if label == "Phone candidate":
        # Preserve whether the source explicitly used an international prefix.
        digits = re.sub(r"[^0-9]", "", raw)
        return "phone", ("+" if raw.startswith("+") else "") + digits
    if label == "Identifier candidate":
        return "identifier", raw
    if label == "URL":
        try:
            parsed = urlsplit(raw)
            if not parsed.hostname or parsed.username or parsed.password:
                return "", ""
            host = parsed.hostname.lower()
            if ":" in host:
                host = f"[{host}]"
            if parsed.port is not None:
                host += f":{parsed.port}"
            return "url", urlunsplit(
                (
                    parsed.scheme.lower(),
                    host,
                    parsed.path,
                    parsed.query,
                    parsed.fragment,
                )
            )
        except ValueError:
            return "", ""
    return "", ""


def finding_values(operation, item, *, inputs=None):
    kind, key = identifier_key(operation, item)
    return {
        "label": item.get("label", ""),
        "preview": (
            item.get("text", "").strip() or scalar_text(item.get("values", {}))
        )[:300],
        "search_text": "\n".join(
            (
                item.get("label", ""),
                item.get("text", ""),
                scalar_text(item.get("values", {})),
            )
        ),
        "weak": item.get("values", {}).get("Weak plate region") is True
        or is_timing_only(operation, item),
        "match_kind": kind,
        "match_key": key,
        "match_digest": hashlib.sha256(key.encode()).hexdigest() if key else "",
        "match_version": "identifier-strings.v1" if key else "",
        **review_fields_v1(operation, item, inputs or {}, kind, key),
    }
