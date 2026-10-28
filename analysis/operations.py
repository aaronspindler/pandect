"""Explicit supported operations; implementations live with their domain."""

from dataclasses import dataclass
from importlib import import_module

from django.core.exceptions import ValidationError


@dataclass(frozen=True)
class Operation:
    label: str
    path: str
    kinds: tuple
    external: bool = False
    version: str = "1"

    @property
    def function(self):
        module, name = self.path.rsplit(".", 1)
        return getattr(import_module(module), name)


TEXT = ("text", "text-message", "document", "image", "audio", "phone-call", "facetime")
LOCATIONS = ("location", "person-location", "vehicle-location")
COMMUNICATIONS = ("text-message", "phone-call", "facetime")
INPUT_GUIDANCE = {
    "native_text": (
        "UTF-8 text or unencrypted native-text PDF, up to 16 MiB. Scanned PDF "
        "pages require separate page-image OCR; office formats are "
        "unsupported."
    ),
    "image_metadata": (
        "JPEG or PNG originals, up to 16 MiB. Metadata describes source "
        "claims and is kept separate from supplied details."
    ),
    "audio_metadata": (
        "Uncompressed 16-bit mono/stereo PCM WAV, including retained call "
        "additions. Other audio formats require a supported conversion "
        "retained as separate evidence."
    ),
    "ocr": (
        "JPEG or PNG page images with printed English text. Handwriting and "
        "direct PDF rendering are unsupported. Extracted wording and regions "
        "remain model suggestions."
    ),
    "transcription": (
        "English, uncompressed 16-bit mono/stereo PCM WAV. Supplied "
        "transcripts remain separate. Automatic text, uncertain intervals and "
        "omissions require review."
    ),
    "conversation_import": (
        "The documented iMessage HTML export profile, up to 2 MiB/500 rows. "
        "Map the exact source account labels to case people and select the "
        "export timezone. Unmapped labels stay unresolved."
    ),
}

OPERATIONS = {
    "native_text": Operation(
        "Extract document text", "analysis.documents.native_text", ("document",)
    ),
    "image_metadata": Operation(
        "Inspect image metadata", "analysis.media.image_metadata", ("image",)
    ),
    "audio_metadata": Operation(
        "Inspect audio metadata",
        "analysis.media.audio_metadata",
        ("audio", "phone-call", "facetime"),
    ),
    "ocr": Operation(
        "Read printed text in images", "analysis.media.ocr", ("image", "document"), True
    ),
    "transcription": Operation(
        "Transcribe recordings",
        "analysis.media.transcription",
        ("audio", "phone-call", "facetime"),
        True,
    ),
    "entities": Operation(
        "Extract identifiers and dates", "analysis.text.entities", TEXT
    ),
    "style_profiles": Operation(
        "Describe writing style",
        "analysis.text.style_profiles",
        ("text", "text-message"),
    ),
    "style_change": Operation(
        "Compare writing over time",
        "analysis.text.style_change",
        ("text", "text-message"),
    ),
    "copied_text": Operation(
        "Find repeated wording", "analysis.text.copied_text", TEXT
    ),
    "text_topics": Operation(
        "Inspect keywords and topics", "analysis.text.text_topics", TEXT
    ),
    "content_events": Operation(
        "Suggest claims and commitments",
        "analysis.conversations.content_events",
        TEXT,
        True,
    ),
    "session_summaries": Operation(
        "Summarize selected conversations",
        "analysis.conversations.session_summaries",
        COMMUNICATIONS,
        True,
    ),
    "sessions": Operation(
        "Read inferred conversation sessions",
        "analysis.conversations.sessions",
        COMMUNICATIONS,
    ),
    "comm_patterns": Operation(
        "Inspect communication timing",
        "analysis.conversations.comm_patterns",
        COMMUNICATIONS,
    ),
    "conversation_link": Operation(
        "Compare exchanges across channels",
        "analysis.conversations.conversation_link",
        COMMUNICATIONS,
    ),
    "conversation_import": Operation(
        "Import an iMessage export",
        "analysis.communication_imports.prepare_import",
        ("document",),
    ),
    "normalize_addresses": Operation(
        "Compare recorded addresses",
        "analysis.locations.normalize_addresses",
        LOCATIONS,
    ),
    "location_quality": Operation(
        "Review observation quality", "analysis.locations.location_quality", LOCATIONS
    ),
    "spatial_groups": Operation(
        "Group nearby observations", "analysis.locations.spatial_groups", LOCATIONS
    ),
    "proximity": Operation(
        "Find observations in an area", "analysis.locations.proximity", LOCATIONS
    ),
    "candidate_visits": Operation(
        "Inspect candidate visits and frequent places",
        "analysis.locations.candidate_visits",
        LOCATIONS,
    ),
    "candidate_trips": Operation(
        "Inspect candidate journeys and drives",
        "analysis.locations.candidate_trips",
        LOCATIONS,
    ),
    "movement_routines": Operation(
        "Compare recurring movement patterns",
        "analysis.locations.movement_routines",
        LOCATIONS,
    ),
    "colocation": Operation(
        "Compare possible co-location", "analysis.locations.colocation", LOCATIONS
    ),
    "travel_discrepancies": Operation(
        "Review unusual location transitions",
        "analysis.locations.travel_discrepancies",
        LOCATIONS,
    ),
    "location_conflicts": Operation(
        "Compare reported location conflicts",
        "analysis.locations.location_conflicts",
        LOCATIONS,
    ),
    "geocode_locations": Operation(
        "Look up geographic coordinates or addresses",
        "analysis.geocoding.geocode_locations",
        LOCATIONS,
        True,
    ),
}


def get_operation(name):
    try:
        return OPERATIONS[name]
    except KeyError:
        raise ValidationError("Choose a supported analysis.") from None
