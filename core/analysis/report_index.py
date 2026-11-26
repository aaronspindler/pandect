"""Versioned searchable projection of the measurements published by each method.

Names are the keys written by the owning analysis implementations. New methods
can publish their outcome and selection without exposing arbitrary payload fields.
Keep v1 available to the existing-result migration.
"""

import math

from django.contrib.postgres.search import SearchVector

TIMING_FIELDS = (
    "records",
    "timed_records",
    "first_recorded_at",
    "last_recorded_at",
    "response_interval_count",
    "median_response_interval_seconds",
)


def timing_values_v1(operation, metrics):
    if operation != "comm_patterns" or not isinstance(metrics, dict):
        return None
    return {
        key: value
        for key in TIMING_FIELDS
        if type(value := metrics.get(key)) in (int, float)
        or (isinstance(value, str) and len(value) <= 40)
    }


GEO_COVERAGE = (
    "Selected observations",
    "Selected interpretations",
    "With coordinates",
    "With event time",
    "Unlinked observations",
)
TEXT_COVERAGE = ("sources", "characters", "rejected_suggestions")
V1_FIELDS = {
    "sighting_group": ("Direct connections",),
    "question_synthesis": ("Selected records", "Selected characters"),
    "claim_comparison": (
        "Comparison sources",
        "Sources assessed by AI",
        "Copies of the claim source",
    ),
    "native_text": (
        "Selected documents",
        "Supported documents",
        "PDF pages",
        "PDF pages without usable native text",
        "PDF pages needing OCR review",
        "Emails with partial coverage",
        "UTF-8 text bytes",
    ),
    "image_metadata": ("Selected sources", "Supported images"),
    "audio_metadata": ("Selected sources", "Supported recordings"),
    "ocr": (
        "Selected sources",
        "Unsupported inputs",
        "Recognized text regions",
        "Images with no recognized text",
        "Partial/unreadable inputs",
        "UTF-8 text bytes",
    ),
    "plate_ocr": (
        "Possible plate regions",
        "Plate regions above filter",
        "Weak plate regions",
        "Text candidates",
        "Search limit reached",
    ),
    "transcription": (
        "Selected sources",
        "Supported recordings",
        "Unsupported inputs",
        "Unresolved/coarse/discrepant chunks",
        "UTF-8 text bytes",
    ),
    "entities": ("sources", "occurrences"),
    "style_profiles": (
        "sources_selected",
        "sources_usable",
        "words",
        "unique_words",
        "characters",
        "excluded_spans",
        "uppercase_letter_ratio",
        "mean_message_words",
        "mean_sentence_words",
        "punctuation_per_100_words",
        "vocabulary_ratio",
    ),
    "style_change": ("group_a_sources", "group_b_sources", "adequate_groups"),
    "copied_text": ("sources", "minimum_words"),
    "text_topics": TEXT_COVERAGE,
    "content_events": TEXT_COVERAGE,
    "session_summaries": (
        "selected_messages",
        "inferred_sessions",
        "summary_chunks",
        "range_covered",
        "gap_minutes",
    ),
    "sessions": ("selected_messages", "sessions", "missing_event_times", "gap_minutes"),
    "comm_patterns": (
        "records",
        "timed_records",
        "missing_event_times",
        "observed_turn_changes",
        "response_interval_count",
        "median_response_interval_seconds",
        "inferred_sessions",
        "gap_minutes",
    ),
    "conversation_link": (
        "selected_communications",
        "missing_event_times",
        "window_minutes",
        "omitted_temporal_candidates",
    ),
    "imessage_import": (
        "source_rows",
        "ready_rows",
        "date_excluded_rows",
        "unresolved_rows",
    ),
    "normalize_addresses": GEO_COVERAGE + ("Candidate wording groups",),
    "location_quality": GEO_COVERAGE + ("Unique original observations",),
    "spatial_groups": GEO_COVERAGE + ("Spatial groups",),
    "geographic_context": GEO_COVERAGE
    + (
        "Spatial groups",
        "Displayed context matches",
        "Failed observations",
        "Excluded observations",
        "Findings omitted by display-byte limit",
        "Geometry/display truncations",
    ),
    "proximity": GEO_COVERAGE + ("Compared observations",),
    "candidate_visits": GEO_COVERAGE
    + (
        "Candidate visits",
        "Excluded sampling gaps",
        "Place groups",
        "Maximum gap (minutes)",
        "Minimum observed span (minutes)",
    ),
    "candidate_trips": GEO_COVERAGE
    + ("Candidate journeys", "Excluded sampling gaps", "Maximum gap (minutes)"),
    "movement_routines": GEO_COVERAGE
    + ("Candidate journeys", "Directed place pairs", "Excluded sampling gaps"),
    "colocation": GEO_COVERAGE
    + ("Candidate pairs", "Time tolerance (minutes)", "Distance tolerance (m)"),
    "travel_discrepancies": GEO_COVERAGE + ("Reported discrepancies or gaps",),
    "location_conflicts": GEO_COVERAGE,
    "journey_claims": ("Claim interpretations", "Selected observations"),
    "geocode_locations": (
        "Selected observations",
        "Candidate matches",
        "Failed observations",
        "Unsupported observations",
    ),
}

V1_LABELS = {
    "uppercase_letter_ratio": "Uppercase letter ratio (fraction of letters)",
    "vocabulary_ratio": "Vocabulary ratio (unique words / total words)",
    "mean_message_words": "Mean message length (words per message)",
    "mean_sentence_words": "Mean sentence length (words per sentence)",
    "punctuation_per_100_words": "Punctuation marks per 100 words",
    "gap_minutes": "Session gap threshold (minutes)",
    "window_minutes": "Comparison window (minutes)",
    "median_response_interval_seconds": "Median response interval (seconds)",
}


def report_words():
    return SearchVector("text", config="english")


def measurements_v1(operation, metrics):
    lines = [operation.replace("_", " ")]
    if isinstance(metrics, dict):
        for key in V1_FIELDS.get(operation, ()):
            value = metrics.get(key)
            if key in ("Search limit reached", "range_covered") and type(value) is bool:
                value = "Yes" if value else "No"
            elif type(value) not in (int, float) or (
                isinstance(value, float) and not math.isfinite(value)
            ):
                continue
            label = V1_LABELS.get(key, key.replace("_", " "))
            lines.append(f"{label[0].upper()}{label[1:]}: {value}")
    return "\n".join(lines)
