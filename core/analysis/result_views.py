"""Choose a reading view for each operation at the application boundary."""

from audio.analysis.presentation import present as audio
from document.analysis.presentation import present as document
from geo.analysis.presentation import present as geo
from image.analysis.presentation import present as image
from phone.analysis.presentation import present as phone
from text.analysis.presentation import present as text

RESULT_PRESENTERS = {
    **dict.fromkeys(("image_metadata", "ocr", "plate_ocr"), image),
    "native_text": document,
    **dict.fromkeys(("audio_metadata", "transcription"), audio),
    **dict.fromkeys(
        (
            "entities",
            "style_profiles",
            "style_change",
            "copied_text",
            "text_topics",
            "content_events",
        ),
        text,
    ),
    **dict.fromkeys(
        (
            "sessions",
            "comm_patterns",
            "conversation_link",
            "session_summaries",
            "conversation_import",
        ),
        phone,
    ),
    **dict.fromkeys(
        (
            "normalize_addresses",
            "location_quality",
            "spatial_groups",
            "proximity",
            "candidate_visits",
            "candidate_trips",
            "movement_routines",
            "colocation",
            "travel_discrepancies",
            "location_conflicts",
            "geocode_locations",
            "geographic_context",
        ),
        geo,
    ),
}

RESULT_TEMPLATES = {
    **dict.fromkeys(
        ("image_metadata", "ocr", "plate_ocr"), "image/analysis/results.html"
    ),
    **dict.fromkeys(("audio_metadata", "transcription"), "audio/analysis/results.html"),
}
