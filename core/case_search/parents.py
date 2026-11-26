"""Resolve active groupings referenced by retained summaries."""

from core.evidence.queries import scoped_evidence
from phone.saved_sessions import valid_sessions


def active_sessions(case):
    return valid_sessions(
        case,
        scoped_evidence(case, "text-message")
        .filter(is_archived=False)
        .exclude(communication_context__source_document__is_archived=True),
    )
