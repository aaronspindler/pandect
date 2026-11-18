"""Choose a reading view for each operation at the application boundary."""

from audio.analysis import presentation as audio
from core.analysis.presentation import prioritize_measurements
from document.analysis import presentation as document
from geo.analysis import presentation as geo
from image.analysis import presentation as image
from phone.analysis import presentation as phone
from text.analysis import presentation as text

GROUPED_RESULTS = "core/analysis/result_groups.html"
RESULT_VIEWS = {
    **dict.fromkeys(
        ("image_metadata", "ocr", "plate_ocr"),
        (image.present, "image/analysis/results.html"),
    ),
    "native_text": (document.present, GROUPED_RESULTS),
    **dict.fromkeys(
        ("audio_metadata", "transcription"),
        (audio.present, "audio/analysis/results.html"),
    ),
    **dict.fromkeys(
        (
            "entities",
            "style_profiles",
            "style_change",
            "copied_text",
            "text_topics",
            "content_events",
            "question_synthesis",
        ),
        (text.present, GROUPED_RESULTS),
    ),
    **dict.fromkeys(
        (
            "sessions",
            "comm_patterns",
            "conversation_link",
            "session_summaries",
            "conversation_import",  # Read retained HTML results; no import operation.
            "imessage_import",
        ),
        (phone.present, GROUPED_RESULTS),
    ),
    **dict.fromkeys(geo.PRIMARY_FIELDS, (geo.present, GROUPED_RESULTS)),
}


def _requested_value_page(rows, query):
    for row in rows:
        if row.get("pagination", {}).get("anchor") in query:
            return True
        if _requested_value_page(
            row.get("children", []), query
        ) or _requested_value_page(row.get("examples", []), query):
            return True
    return False


def result_view(operation, items, formatter, *, query=None):
    presenter, template = RESULT_VIEWS[operation]
    groups = presenter(operation, items, formatter)
    for item in items:
        item["display_sources"] = item.get("remaining_sources", item["sources"])
        prioritize_measurements(item, "Status", "Coverage", "Reason", "Interpretation")
        item["collapse_measurements"] = bool(
            item.get("text")
            or item.get("weak_ocr_text")
            or item.get("primary_measurements")
            or item.get("messages")
            or item.get("candidate_body")
        )
        item["details_open"] = _requested_value_page(
            item["measurements"], query or {}
        ) or any(
            _requested_value_page(message["references"], query or {})
            for message in item.get("message_details", [])
        )
    return groups, template
