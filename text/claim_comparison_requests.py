"""Review exact retained sources and locate a claim before queueing comparison."""

import hashlib
import json
import re

from django.core.exceptions import ValidationError

from core.analysis.errors import AnalysisError
from core.analysis.services import snapshot_and_resolve_sources
from text.analysis.claim_comparison import (
    COMPARISON_KINDS,
    MAX_SOURCES,
    validate_sources,
)


def review_sources(case, selections, *, expected_digest=None):
    """Resolve the available originals and verify a previously reviewed selection."""
    if not 2 <= len(selections) <= MAX_SOURCES:
        raise ValidationError(f"Choose between 2 and {MAX_SOURCES} retained sources.")
    snapshots, sources = snapshot_and_resolve_sources(
        case, selections, allowed_kinds=COMPARISON_KINDS
    )
    validate_sources(sources)
    digest = hashlib.sha256(json.dumps(snapshots, sort_keys=True).encode()).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        raise AnalysisError("source_changed")
    return digest, sources


def quote_offsets(text, quote):
    # An HTML textarea normalizes line endings. Translate the match back to
    # character offsets in the unchanged retained source.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    quote = quote.replace("\r\n", "\n").replace("\r", "\n")
    start = normalized.find(quote) if quote.strip() else -1
    if start == -1:
        raise ValidationError("Copy the exact wording from the selected claim source.")
    if normalized.find(quote, start + 1) != -1:
        raise ValidationError(
            "This quotation occurs more than once. Include surrounding words "
            "to identify the passage."
        )
    end = start + len(quote)
    for line_end in re.finditer("\r\n", text):
        if line_end.start() >= end:
            break
        end += 1
        if line_end.start() < start:
            start += 1
    return start, end
