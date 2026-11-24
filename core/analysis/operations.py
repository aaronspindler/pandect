"""Explicit supported operations; implementations live with their domain."""

from collections.abc import Callable
from dataclasses import dataclass

from django.core.exceptions import ValidationError

from audio.analysis import ai as audio_ai
from audio.analysis import metadata as audio_metadata
from core.analysis.sightings import build_sighting_group
from core.evidence.types import COMMUNICATION_KINDS as COMMUNICATIONS
from core.evidence.types import KINDS
from core.evidence.types import LOCATION_KINDS as LOCATIONS
from document.analysis import deterministic as documents
from geo.analysis.deterministic import journey_claims, locations
from geo.analysis.external import geocoding, geographic_context
from image.analysis import ai as image_ai
from image.analysis import metadata as image_metadata
from image.analysis.plates import plate_ocr
from phone.analysis import ai as phone_ai
from phone.analysis import workflows as phone_workflows
from phone.analysis.deterministic import conversations, imessage
from text.analysis import ai as text_ai
from text.analysis import deterministic as text
from text.analysis import workflows as text_workflows
from text.analysis.claim_comparison import COMPARISON_KINDS, compare_claim
from text.analysis.synthesis import synthesize


@dataclass(frozen=True)
class Operation:
    label: str
    function: Callable
    kinds: tuple
    additional_on_evidence: bool = False
    version: str = "1"


TEXT = ("text", "text-message", "document", "image", "audio", "phone-call", "facetime")
INPUT_GUIDANCE = {
    "plate_ocr": (
        "One retained JPEG or PNG image. Local geometric detection searches for "
        "possible plates and reads their central text line. Misses and false "
        "candidates are possible; review every reading against the original."
    ),
    "native_text": (
        "UTF-8 text, .eml email, or unencrypted PDF, up to 16 MiB. Local OCR "
        "reads printed English on PDF pages without a text layer. Inspect "
        "uncertain or skipped pages against the original. Office formats "
        "are unsupported."
    ),
    "image_metadata": (
        "JPEG or PNG originals, up to 16 MiB. Metadata describes source "
        "claims and is kept separate from supplied details."
    ),
    "audio_metadata": (
        "Uncompressed 16-bit PCM WAV or AAC M4A, including retained call additions. "
        "Mono/stereo, 8–48 kHz, up to 30 minutes; original and decoded audio "
        "are each limited to 16 MiB."
    ),
    "ocr": (
        "Printed English in JPEG or PNG page images, processed locally. "
        "Images stay on this server. Handwriting, mixed text, other languages "
        "and direct PDF rendering are unsupported. Inspect extracted text "
        "against the original."
    ),
    "geographic_context": (
        "Reported coordinates or selected address estimates. "
        "Address-only miTrail observations need a selected address estimate. "
        "Lookup sends bounded coordinate areas and the chosen map reference "
        "date to the configured service. Map features are contextual candidates."
    ),
    "transcription": (
        "English PCM WAV or AAC M4A, mono/stereo at 8–48 kHz, up to 30 minutes. "
        "Original and decoded audio are each limited to 16 MiB. Supplied "
        "transcripts remain separate. Automatic text, uncertain intervals and "
        "omissions require review."
    ),
    "sessions": (
        "Retained text messages, grouped by reported participants and message "
        "times. Session boundaries are inferred and can be adjusted."
    ),
    "comm_patterns": (
        "Retained text messages with reported participants and message times. "
        "Open Case Tools → Reports → Call patterns for call counts and durations."
    ),
    "session_summaries": (
        "Retained text messages, summarized in inferred session groups. For "
        "call transcripts, choose Inspect keywords and topics with a "
        "source-cited topic summary."
    ),
    "claim_comparison": (
        "Choose a message or call transcript containing the claim and one to "
        "five other text sources. Scans, documents and recordings need retained "
        "text extraction or a transcript."
    ),
}

OPERATIONS = {
    "sighting_group": Operation("Sighting group", build_sighting_group, tuple(KINDS)),
    "question_synthesis": Operation(
        "Answer a question from selected material", synthesize, (*TEXT, *LOCATIONS)
    ),
    "claim_comparison": Operation("Compare a claim", compare_claim, COMPARISON_KINDS),
    "native_text": Operation(
        "Extract document text", documents.native_text, ("document",), version="2"
    ),
    "image_metadata": Operation(
        "Inspect image metadata", image_metadata.image_metadata, ("image",), version="3"
    ),
    "audio_metadata": Operation(
        "Inspect audio metadata",
        audio_metadata.audio_metadata,
        ("audio", "phone-call", "facetime"),
        version="2",
    ),
    "ocr": Operation(
        "Read text in images", image_ai.ocr, ("image", "document"), version="6"
    ),
    "plate_ocr": Operation(
        "Find vehicle plates",
        plate_ocr,
        ("image",),
        additional_on_evidence=True,
        version="2",
    ),
    "transcription": Operation(
        "Transcribe recordings",
        audio_ai.transcription,
        ("audio", "phone-call", "facetime"),
        additional_on_evidence=True,
        version="2",
    ),
    "entities": Operation(
        "Extract identifiers and dates", text.extract_entities, TEXT, version="2"
    ),
    "style_profiles": Operation(
        "Describe writing style",
        text.style_profile,
        ("text", "text-message"),
    ),
    "style_change": Operation(
        "Compare writing over time",
        text.style_changes,
        ("text", "text-message"),
    ),
    "copied_text": Operation("Find repeated wording", text.copied_text, TEXT),
    "text_topics": Operation(
        "Inspect keywords and topics",
        text_workflows.text_topics,
        TEXT,
        additional_on_evidence=True,
        version="2",
    ),
    "content_events": Operation(
        "Suggest claims and commitments",
        text_ai.content_events,
        TEXT,
        additional_on_evidence=True,
    ),
    "session_summaries": Operation(
        "Summarize message sessions",
        phone_ai.session_summaries,
        ("text-message",),
        additional_on_evidence=True,
        version="3",
    ),
    "sessions": Operation(
        "Read inferred message sessions",
        conversations.conversation_sessions,
        ("text-message",),
        version="2",
    ),
    "comm_patterns": Operation(
        "Inspect message timing",
        conversations.interaction_patterns,
        ("text-message",),
        version="2",
    ),
    "conversation_link": Operation(
        "Compare exchanges across channels",
        phone_workflows.cross_channel_links,
        COMMUNICATIONS,
    ),
    "imessage_import": Operation(
        "Import message CSV", imessage.prepare_import, ("document",), version="2"
    ),
    "normalize_addresses": Operation(
        "Compare recorded addresses",
        locations.normalize_addresses,
        LOCATIONS,
    ),
    "location_quality": Operation(
        "Review observation quality", locations.location_quality, LOCATIONS
    ),
    "spatial_groups": Operation(
        "Group nearby observations", locations.spatial_groups, LOCATIONS
    ),
    "geographic_context": Operation(
        "Look up geographic context",
        geographic_context.geographic_context,
        LOCATIONS,
        additional_on_evidence=True,
    ),
    "proximity": Operation(
        "Find observations in an area", locations.proximity, LOCATIONS
    ),
    "candidate_visits": Operation(
        "Inspect candidate visits and frequent places",
        locations.candidate_visits,
        LOCATIONS,
    ),
    "candidate_trips": Operation(
        "Inspect candidate journeys and drives",
        locations.candidate_trips,
        LOCATIONS,
    ),
    "movement_routines": Operation(
        "Compare recurring movement patterns",
        locations.movement_routines,
        LOCATIONS,
    ),
    "colocation": Operation(
        "Compare possible co-location", locations.colocation, LOCATIONS
    ),
    "travel_discrepancies": Operation(
        "Review unusual location transitions",
        locations.travel_discrepancies,
        LOCATIONS,
    ),
    "location_conflicts": Operation(
        "Compare reported location conflicts",
        locations.location_conflicts,
        LOCATIONS,
    ),
    "journey_claims": Operation(
        "Compare plans with locations", journey_claims.compare_plans, TEXT + LOCATIONS
    ),
    "geocode_locations": Operation(
        "Look up geographic coordinates or addresses",
        geocoding.geocode_locations,
        LOCATIONS,
        version="2",
        additional_on_evidence=True,
    ),
}


CASE_OPERATIONS = {
    name: operation
    for name, operation in OPERATIONS.items()
    if name not in {"question_synthesis", "imessage_import", "sighting_group"}
}


def get_operation(name):
    try:
        return OPERATIONS[name]
    except KeyError:
        raise ValidationError("Choose a supported analysis.") from None


OPERATION_GROUPS = {
    "Extract and inspect files": {
        "native_text": "Read document and email text with page or line references.",
        "image_metadata": (
            "Compare decoded image properties with embedded metadata claims."
        ),
        "audio_metadata": (
            "Inspect recording properties and numerical signal intervals."
        ),
        "ocr": "Compare printed text and inferred layout with the source image.",
        "plate_ocr": "Compare possible plate readings and their source regions.",
        "transcription": (
            "Read candidate speech with recording timestamps and coverage gaps."
        ),
    },
    "Read and compare text": {
        "claim_comparison": (
            "Compare one quoted claim with selected case evidence and review "
            "supporting, conflicting and unresolved passages."
        ),
        "question_synthesis": (
            "Ask a question using a shared selection of text and findings."
        ),
        "entities": "Find identifiers and dates with the exact wording they came from.",
        "style_profiles": "Inspect descriptive writing measurements and examples.",
        "style_change": "Compare two writing samples and their coverage.",
        "copied_text": "Compare matching passages and their source positions.",
        "text_topics": (
            "Review frequent words and available source-cited topic suggestions."
        ),
        "content_events": (
            "Review model suggestions for claims and commitments with source "
            "quotations."
        ),
    },
    "Inspect communications": {
        "sessions": (
            "Read inferred message sessions with reported participants and event times."
        ),
        "comm_patterns": (
            "Compare recorded message counts, intervals and selected periods."
        ),
        "conversation_link": (
            "Compare exchanges across channels using reported timing and wording."
        ),
        "session_summaries": (
            "Review source-cited summaries of selected message sessions."
        ),
    },
    "Compare locations and movement": {
        "journey_claims": (
            "Compare quoted plans or reported events with selected observations, "
            "visits, and possible trips."
        ),
        "normalize_addresses": (
            "Compare the recorded and normalized wording of addresses."
        ),
        "location_quality": (
            "Inspect missing positions, event times and differences between "
            "interpretations."
        ),
        "spatial_groups": "See candidate groups and the observations they contain.",
        "proximity": "See which observations fall near a selected point or area.",
        "candidate_visits": (
            "Compare candidate visits, observed spans and sampling gaps."
        ),
        "candidate_trips": "Inspect candidate journeys, observed endpoints and gaps.",
        "movement_routines": "Compare recurring directed journeys and sample coverage.",
        "colocation": "Compare nearby samples by distance and time difference.",
        "travel_discrepancies": "Review unusual transitions and gaps between samples.",
        "location_conflicts": (
            "Compare selected observations for the same reported event."
        ),
        "geocode_locations": (
            "Review candidate positions or addresses from the configured lookup."
        ),
        "geographic_context": "Inspect spatial relations and retained map references.",
    },
}
