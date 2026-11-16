"""Choose a reading view for each operation at the application boundary."""

from audio.analysis import presentation as audio
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
            "conversation_import",
            "imessage_import",
        ),
        (phone.present, GROUPED_RESULTS),
    ),
    **dict.fromkeys(geo.PRIMARY_FIELDS, (geo.present, GROUPED_RESULTS)),
}


def result_view(operation, items, formatter):
    presenter, template = RESULT_VIEWS[operation]
    return presenter(operation, items, formatter), template
