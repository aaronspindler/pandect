"""Choose a reading view for each operation at the application boundary."""

from audio.analysis import presentation as audio
from core.analysis.presentation import prioritize_measurements
from core.analysis.review_signals import REVIEW_SIGNALS
from document.analysis import presentation as document
from geo.analysis import presentation as geo
from image.analysis import presentation as image
from phone.analysis import presentation as phone
from phone.analysis.ingest import import_selection_warning
from text.analysis import presentation as text
from text.analysis.ai import CITED_SUGGESTION_BASIS, CITED_SUGGESTION_WARNING

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
            "claim_comparison",
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


def focused_finding_context(operation, item, data):
    """Disclose technical locators and shorten only recognized retained wording."""
    if operation == "question_synthesis" and item["ai_suggestion"]:
        item["label"] = item["label"].removesuffix(" · AI suggestion")
    message_ids = {
        message["source"]["id"] for message in item.get("message_details", [])
    }
    item["source_details"] = [
        source
        for source in item["display_sources"]
        if source["id"] not in message_ids
        and any(
            anchor.get("region")
            or any(
                anchor.get(key) is not None for key in ("end", "html_end", "csv_end")
            )
            for anchor in source.get("anchors", [])
        )
    ]
    values = item.get("values", {})
    if (
        operation == "content_events"
        and item["ai_suggestion"]
        and values.get("status") == "supported"
        and values.get("basis") == CITED_SUGGESTION_BASIS
        and any(source.get("excerpt") for source in item["display_sources"])
    ):
        item["citation_checked"] = True
        primary = item.get("primary_measurements", [])
        item["measurements"] = [
            row for row in primary if row["label"] in {"Status", "Basis"}
        ] + item["measurements"]
        item["primary_measurements"] = [
            row for row in primary if row["label"] not in {"Status", "Basis"}
        ]
        if item.get("review_reason") == REVIEW_SIGNALS["quoted_claim"]:
            item["review_reason"] = ""

    warnings = list(data.get("warnings") or ())
    selection = data.get("metrics", {}).get("import_selection")
    coverage = ""
    if isinstance(selection, dict):
        selected = selection.get("selected_messages")
        imported = selection.get("imported_messages")
        if (
            type(selected) is int
            and type(imported) is int
            and 0 <= selected <= imported
        ):
            retained = import_selection_warning(selected, imported)
            if retained in warnings:
                warnings.remove(retained)
                coverage = (
                    f"Selected {selected} of {imported} newly imported messages. "
                    "Results cover the saved selection."
                )
    if operation == "content_events" and item["ai_suggestion"]:
        warnings = [
            "AI can misread tone, quotations, dates, or missing context."
            if warning == CITED_SUGGESTION_WARNING
            else warning
            for warning in warnings
        ]
    return {"finding_warnings": warnings, "finding_coverage": coverage}
