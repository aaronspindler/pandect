"""Tailored public capability copy backed by the application's static operation data."""

from core.analysis.execution import GEOGRAPHIC as GEOGRAPHIC_OPERATIONS
from core.analysis.execution import requires_inference
from core.analysis.operations import OPERATIONS

INCLUDED = "Included"
OPTIONAL_AI = "Included; AI optional"
MODEL = "Private setup or approved external AI"
GEOGRAPHIC = "Configured geographic service"

LOCATION_KINDS = ("location", "person-location", "vehicle-location")
CALL_KINDS = ("phone-call", "facetime")
COMMUNICATION_KINDS = ("text-message", *CALL_KINDS)
FILE_KINDS = ("image", "audio", "document")

TEXT_MODEL = (
    "Configure a private Ollama text model, or select and approve external AI "
    "via OpenAI, Anthropic, or Cloudflare AI Gateway. Usable selected text is required."
)
TEXT_PREREQUISITE = (
    "Text analysis uses original text or an exact retained extraction or "
    "transcript. Files and metadata alone do not supply usable text."
)
LOCATION_PREREQUISITE = (
    "Spatial comparisons need reported coordinates or explicitly selected "
    "geographic estimates. Movement tracks need timed person-location or "
    "vehicle-location records linked to a person or vehicle. Unlinked locations "
    "cannot form a subject’s track."
)

EVIDENCE_TYPES = (
    {
        "kind": "document",
        "label": "Document",
        "description": "Retain a source file and review derived text separately.",
        "intake": (
            "Upload a file or add an individual document; supported message exports "
            "can be imported."
        ),
        "formats": (
            "Text extraction supports UTF-8 files and unencrypted native-text PDF, "
            "up to 16 MiB input, 100 PDF pages and 2 MiB extracted text. "
            "Printed-English OCR supports JPEG/PNG page images. "
            "Office extraction and OCR directly from scanned PDFs are unsupported."
        ),
        "prerequisites": TEXT_PREREQUISITE,
    },
    {
        "kind": "image",
        "label": "Image",
        "description": "Retain an original image with its source details and checksum.",
        "intake": "Upload images or add an individual image.",
        "formats": (
            "Analysis supports JPEG and single-frame PNG, up to 16 MiB and "
            "50 million pixels. Multi-picture JPEG analysis covers only the "
            "primary image. Native RAW and animated PNG analysis are unsupported."
        ),
        "prerequisites": TEXT_PREREQUISITE,
    },
    {
        "kind": "audio",
        "label": "Audio",
        "description": (
            "Retain a recording and inspect its properties or candidate transcript."
        ),
        "intake": "Upload recordings or add an individual audio file.",
        "formats": (
            "Analysis supports uncompressed signed 16-bit PCM WAV or AAC M4A, "
            "mono/stereo, 8–48 kHz, up to 30 decoded minutes. Original and "
            "decoded audio are each limited to 16 MiB. "
            "Transcription supports English. Other originals can be retained "
            "without supported analysis."
        ),
        "prerequisites": TEXT_PREREQUISITE,
    },
    {
        "kind": "text",
        "label": "Text",
        "description": "Capture original text with its source attribution.",
        "intake": (
            "Add a text record. Use a Document upload when exact source-file bytes "
            "matter."
        ),
        "formats": (
            "Text entered in the browser; writing analysis uses original writing "
            "with adequate samples."
        ),
        "prerequisites": (
            "Comparisons require deliberately selected sources and relevant writing "
            "context."
        ),
    },
    {
        "kind": "text-message",
        "label": "Text message",
        "description": (
            "Retain message text, reported participants and an optional message time."
        ),
        "intake": "Add one message or import a supported iMessage CSV export.",
        "formats": (
            "Individual messages and the documented iMessage CSV export profile; "
            "imported attachment references do not fetch attachment files."
        ),
        "prerequisites": (
            "Individual records link to people in the same case. Session and timing "
            "analysis uses reported participants and message times."
        ),
    },
    {
        "kind": "phone-call",
        "label": "Phone call",
        "description": (
            "Record a reported call between case participants, with optional time "
            "and duration."
        ),
        "intake": (
            "Add a call; optionally supply a transcript or audio. Later additions "
            "retain their own attribution and checksums."
        ),
        "formats": (
            "Call metadata, supplied transcripts and supported PCM WAV or AAC M4A "
            "additions; transcription supports English."
        ),
        "prerequisites": (
            "Participants belong to the same case. Text analysis needs a selected "
            "supplied transcript or retained transcription; call metadata supplies "
            "no speech."
        ),
    },
    {
        "kind": "facetime",
        "label": "FaceTime",
        "description": (
            "Record a reported FaceTime call and its participants, time or duration."
        ),
        "intake": (
            "Add a FaceTime record with optional transcript or audio; later "
            "additions retain earlier material."
        ),
        "formats": (
            "Call metadata, supplied transcripts and supported PCM WAV or AAC M4A "
            "additions. FaceTime video upload and video analysis are unavailable."
        ),
        "prerequisites": (
            "Participants belong to the same case. Text analysis needs a selected "
            "supplied transcript or retained transcription; call metadata supplies "
            "no speech."
        ),
    },
    {
        "kind": "location",
        "label": "Location",
        "description": (
            "Retain a reported address or position, with an optional observation time."
        ),
        "intake": (
            "Add a location record with an address or WGS84 latitude/longitude pair."
        ),
        "formats": (
            "Structured location records; recorded addresses and coordinates remain "
            "distinct from geographic estimates."
        ),
        "prerequisites": LOCATION_PREREQUISITE,
    },
    {
        "kind": "person-location",
        "label": "Person location",
        "description": (
            "Retain a reported location explicitly linked to a person in the case."
        ),
        "intake": "Add a person-location record and select an existing case person.",
        "formats": (
            "An address or WGS84 coordinate pair, with an optional observation time."
        ),
        "prerequisites": LOCATION_PREREQUISITE,
    },
    {
        "kind": "vehicle-location",
        "label": "Vehicle location",
        "description": (
            "Retain a reported vehicle position or address and its source context."
        ),
        "intake": (
            "Add a record linked to a case vehicle, or review and import a "
            "supported Asset Location CSV."
        ),
        "formats": (
            "Addresses or WGS84 coordinate pairs; supported CSV imports can also "
            "retain source-provided speed, heading, receipt times and source rows."
        ),
        "prerequisites": LOCATION_PREREQUISITE,
    },
)

ANALYSES = {
    "native_text": {
        "label": "Extract document text",
        "description": "Read embedded text with page or line references.",
        "detail": (
            "UTF-8 files or unencrypted native-text PDF. Scanned pages need "
            "separately retained JPEG/PNG page images for OCR."
        ),
    },
    "image_metadata": {
        "label": "Inspect image metadata",
        "description": (
            "Inspect dimensions, embedded time/GPS/device claims and differences "
            "from supplied details."
        ),
        "detail": (
            "Supported JPEG/PNG. Embedded metadata is a source claim, not "
            "verification of capture time or location."
        ),
    },
    "audio_metadata": {
        "label": "Inspect audio metadata",
        "description": (
            "Inspect recording duration, format and numerical signal intervals."
        ),
        "detail": (
            "A supported PCM WAV or AAC M4A original or retained call audio "
            "addition; no speech recognition is needed."
        ),
    },
    "ocr": {
        "label": "Read text in images",
        "description": (
            "Read candidate printed-English text with links to its image regions."
        ),
        "detail": (
            "JPEG/PNG images. Tesseract and English data are bundled in the "
            "standard Docker image; no external service or credentials. Review "
            "readings against the original."
        ),
    },
    "plate_ocr": {
        "label": "Find vehicle plates",
        "description": (
            "Find possible plate regions and candidate text readings for review."
        ),
        "detail": (
            "One JPEG/PNG image; uses bundled local detection and Tesseract. Misses "
            "and false candidates are possible. Readings establish no owner or "
            "driver."
        ),
    },
    "transcription": {
        "label": "Transcribe recordings",
        "description": (
            "Produce candidate English speech with recording timestamps and "
            "coverage gaps."
        ),
        "detail": (
            "Supported PCM WAV or AAC M4A audio. Configure a private Whisper runtime "
            "and model, or explicitly select and approve transcription through "
            "Cloudflare AI Gateway. No speaker identification."
        ),
    },
    "entities": {
        "label": "Extract identifiers and dates",
        "description": (
            "Find explicit identifiers, dates, amounts and coordinates with their "
            "original wording."
        ),
        "detail": (
            "Usable text is required. Identifier matches perform no DNS or "
            "ownership lookup and prove no identity or association."
        ),
    },
    "style_profiles": {
        "label": "Describe writing style",
        "description": (
            "Measure vocabulary, punctuation, length and other observable writing "
            "features."
        ),
        "detail": (
            "Adequate original writing and supplied language/context; short samples "
            "may be insufficient. Measurements establish no authorship or "
            "personality."
        ),
    },
    "style_change": {
        "label": "Compare writing over time",
        "description": (
            "Compare two writing samples, their differences and their coverage."
        ),
        "detail": (
            "Select two disjoint groups of original writing and their context. "
            "Reported times support period comparisons; differences establish no "
            "change of writer."
        ),
    },
    "copied_text": {
        "label": "Find repeated wording",
        "description": "Compare matching passages and follow their source positions.",
        "detail": (
            "Multiple usable text sources. Matching wording does not establish "
            "copying direction or a common author."
        ),
    },
    "text_topics": {
        "label": "Inspect keywords and topics",
        "description": (
            "Count frequent words and optionally suggest cited topics or short "
            "summaries."
        ),
        "detail": (
            "Keywords need usable text only. Optional topic summaries need a "
            "private Ollama text model or explicitly approved external AI."
        ),
    },
    "content_events": {
        "label": "Suggest claims and commitments",
        "description": (
            "Suggest attributed claims, plans, commitments, cancellations and "
            "questions with source quotations."
        ),
        "detail": TEXT_MODEL + " Model interpretations are labelled as AI suggestions.",
    },
    "sessions": {
        "label": "Read inferred message sessions",
        "description": (
            "Group selected messages into inferred sessions with inspectable "
            "boundaries."
        ),
        "detail": (
            "Reported participants and message times; gap assumptions and "
            "boundaries can be adjusted. Missing exchanges remain unknown."
        ),
    },
    "comm_patterns": {
        "label": "Inspect message timing",
        "description": "Compare message counts, intervals, turns and selected periods.",
        "detail": (
            "Reported times and participant context. Observed intervals are not "
            "verified replies or evidence of motive."
        ),
    },
    "conversation_link": {
        "label": "Compare exchanges across channels",
        "description": (
            "Compare candidate message and call exchanges using reported timing and "
            "wording."
        ),
        "detail": (
            "Timing comparisons are included. Optional semantic links need usable "
            "text plus a private Ollama model or approved external AI. Adjacency "
            "proves no shared event or causation."
        ),
    },
    "session_summaries": {
        "label": "Summarize message sessions",
        "description": (
            "Suggest source-cited summaries of selected, inferred message sessions."
        ),
        "detail": TEXT_MODEL + " Summaries disclose selection and chunk coverage.",
    },
    "normalize_addresses": {
        "label": "Compare recorded addresses",
        "description": "Compare original and normalized address wording.",
        "detail": (
            "Recorded address text. This comparison performs no geocoding or "
            "external reference lookup."
        ),
    },
    "location_quality": {
        "label": "Review observation quality",
        "description": (
            "Inspect missing positions or times, repeated rows, receipt delays and "
            "discrepancies."
        ),
        "detail": "Uses retained observation fields; missing values stay unknown.",
    },
    "spatial_groups": {
        "label": "Group nearby observations",
        "description": (
            "Group positions using a chosen distance and inspect contributing "
            "observations."
        ),
        "detail": (
            "Use reported coordinates or selected address estimates and choose "
            "the distance threshold."
        ),
    },
    "sighting_group": {
        "label": "Review sighting groups",
        "description": "Save selected direct connections and review their sources.",
        "detail": (
            "Choose an image or location observation with a retained plate "
            "match, spatial comparison, or reported person or vehicle association. "
            "Other evidence can supply supporting or contrary context."
        ),
    },
    "proximity": {
        "label": "Find observations in an area",
        "description": "Find positions within a selected radius or polygon.",
        "detail": (
            "Usable coordinates and an investigator-supplied point/radius or area."
        ),
    },
    "candidate_visits": {
        "label": "Inspect candidate visits and frequent places",
        "description": (
            "Inspect possible stops, observed spans, frequent places and sampling gaps."
        ),
        "detail": (
            "Timed positions and subject context. Candidates do not label a place "
            "as home or work."
        ),
    },
    "candidate_trips": {
        "label": "Inspect candidate journeys and drives",
        "description": (
            "Inspect candidate journeys, observed endpoints and gaps in selected "
            "positions."
        ),
        "detail": (
            "Usable timed positions. Straight connections represent observations, "
            "not road routes or complete travel histories."
        ),
    },
    "movement_routines": {
        "label": "Compare recurring movement patterns",
        "description": "Compare directed journeys, periods and observation coverage.",
        "detail": (
            "Usable timed positions and selected periods. Sampling density and "
            "missing observations limit comparisons."
        ),
    },
    "colocation": {
        "label": "Compare possible co-location",
        "description": (
            "Compare nearby samples from distinct person or vehicle associations."
        ),
        "detail": (
            "Usable positions/times and at least two distinct linked subjects. "
            "Proximity establishes no meeting or relationship."
        ),
    },
    "travel_discrepancies": {
        "label": "Review unusual location transitions",
        "description": (
            "Flag rapid or equal-time transitions, ordering issues and unsupported "
            "gaps."
        ),
        "detail": (
            "Usable timed positions. Flags identify observations for review, not "
            "verified travel or wrongdoing."
        ),
    },
    "location_conflicts": {
        "label": "Compare reported location conflicts",
        "description": "Compare selected observations for the same claimed event.",
        "detail": (
            "At least two observations and an investigator-supplied event "
            "description. Conflicting claims remain distinct."
        ),
    },
    "journey_claims": {
        "label": "Compare plans with locations",
        "description": "Compare a quoted plan or reported event with location samples.",
        "detail": (
            "Select an exact passage and one or more observations, including samples "
            "from a saved trip. Open the sources behind each comparison. Attendance, "
            "driver identity and gaps in coverage remain open."
        ),
    },
    "geocode_locations": {
        "label": "Look up geographic coordinates or addresses",
        "description": (
            "Find candidate coordinates from an address, or nearby addresses from a "
            "position."
        ),
        "detail": (
            "Configure a Nominatim-compatible private service or approve an "
            "external lookup. Select candidates before downstream "
            "spatial use."
        ),
    },
    "geographic_context": {
        "label": "Look up geographic context",
        "description": (
            "Find contextual roads, venues, buildings and neighbourhood features "
            "near observations."
        ),
        "detail": (
            "Configure an Overpass-compatible private service or approve an "
            "external lookup. Needs usable coordinates; historical context also "
            "needs matching dataset coverage. Map features remain contextual "
            "candidates."
        ),
    },
}

REPORTS = {
    "calls": {
        "label": "Call patterns",
        "description": (
            "Review reported call counts, durations, participants and timing."
        ),
        "kinds": CALL_KINDS,
        "requirement": INCLUDED,
        "detail": "Reported call metadata; recordings and transcripts are optional.",
    },
    "vehicle-trips": {
        "label": "Vehicle trips",
        "description": (
            "Review saved possible trips and their source observations across "
            "complete eligible histories."
        ),
        "kinds": ("vehicle-location",),
        "requirement": INCLUDED,
        "detail": (
            "Timed vehicle observations. Trips can be built from addresses. "
            "Choose an address estimate to plot its position. New observations "
            "and supported imports queue trip preparation."
        ),
    },
    "vehicle-coverage": {
        "label": "Vehicle observation coverage",
        "description": (
            "Inspect observation counts, sampling gaps, repeated rows and receipt "
            "delays."
        ),
        "kinds": ("vehicle-location",),
        "requirement": INCLUDED,
        "detail": (
            "Uses retained vehicle observation fields; no model or geographic "
            "service is required."
        ),
    },
    "vehicle-speed": {
        "label": "Vehicle speed and heading",
        "description": (
            "Review source-provided speed, heading and changes between observations."
        ),
        "kinds": ("vehicle-location",),
        "requirement": INCLUDED,
        "detail": (
            "Requires source-provided telemetry for those measurements; speed is "
            "not inferred from addresses."
        ),
    },
    "duplicates": {
        "label": "Exact file duplicates",
        "description": "Group identical file bytes using retained SHA-256 checksums.",
        "kinds": (*FILE_KINDS, *CALL_KINDS),
        "requirement": INCLUDED,
        "detail": (
            "Original files or retained call audio. Byte equality does not "
            "establish truth, common authorship or semantic similarity."
        ),
    },
}

IMPORTS = {
    "imessage_import": {
        "label": "Import iMessage CSV",
        "description": (
            "Review one direct conversation and publish messages with their "
            "retained source context."
        ),
        "kinds": (*OPERATIONS["imessage_import"].kinds, "text-message"),
        "requirement": INCLUDED,
        "detail": (
            "The documented 18-column UTF-8 profile, up to 100 MiB. Confirm "
            "timezone and participants. New messages receive applicable analysis "
            "using configured private and case-approved external services. "
            "Attachment references are not fetched."
        ),
    },
    "vehicle_locations": {
        "label": "Import vehicle locations",
        "description": (
            "Review Asset Location CSV rows and publish vehicle observations with "
            "source-row provenance."
        ),
        "kinds": ("vehicle-location",),
        "requirement": INCLUDED,
        "detail": (
            "The documented nine-column UTF-8 CSV profile, up to 100 MiB/100,000 "
            "rows. Confirm timezone, time interpretation and vehicle mappings. "
            "New observations queue location analysis and saved trips. "
            "Configured lookups can add geographic context."
        ),
    },
}


EVIDENCE_FAMILIES = (
    {
        "id": "",
        "label": "All analysis",
        "description": "Explore supported methods, pattern reports and imports.",
        "intake": (
            "Add original files, text, messages, calls and locations. "
            "Choose an evidence type to see its supported formats and limits."
        ),
        "kinds": tuple(evidence["kind"] for evidence in EVIDENCE_TYPES),
    },
    {
        "id": "documents",
        "label": "Documents & text",
        "description": "Extract usable text, compare writing and review what it says.",
        "intake": (
            "Upload a document or add original text. Use a document upload "
            "when exact source-file bytes matter."
        ),
        "kinds": ("document", "text"),
    },
    {
        "id": "images",
        "label": "Images",
        "description": (
            "Inspect image properties, read printed text and review possible plates."
        ),
        "intake": (
            "Upload original images with their source details. "
            "Analysis supports JPEG and single-frame PNG."
        ),
        "kinds": ("image",),
    },
    {
        "id": "audio",
        "label": "Audio",
        "description": "Inspect recordings and review candidate transcripts.",
        "intake": (
            "Upload original recordings. Analysis supports the PCM WAV and AAC M4A "
            "profiles described below; transcription supports English."
        ),
        "kinds": ("audio",),
    },
    {
        "id": "communications",
        "label": "Messages & calls",
        "description": "Read conversations, inspect timing and compare what was said.",
        "intake": (
            "Add individual messages, phone calls or FaceTime records. "
            "Import a supported iMessage CSV export."
        ),
        "kinds": COMMUNICATION_KINDS,
    },
    {
        "id": "locations",
        "label": "Locations",
        "description": (
            "Compare reported places, possible journeys and observation coverage."
        ),
        "intake": (
            "Add locations, link observations to a case person or vehicle, "
            "or import a supported vehicle-location CSV."
        ),
        "kinds": LOCATION_KINDS,
    },
)

SHORT_REQUIREMENTS = {
    INCLUDED: "Included",
    OPTIONAL_AI: "AI optional",
    MODEL: "Requires AI setup",
    GEOGRAPHIC: "Geographic setup",
}


def _analysis_requirement(name):
    if name in GEOGRAPHIC_OPERATIONS:
        return GEOGRAPHIC
    if requires_inference(name, {"include_summary": False, "semantic_links": False}):
        return MODEL
    if requires_inference(name, {"include_summary": True, "semantic_links": True}):
        return OPTIONAL_AI
    return INCLUDED


def catalogue_context(family_id=""):
    """Filter public presentation data without inspecting cases or live services."""
    family = next(
        (family for family in EVIDENCE_FAMILIES if family["id"] == family_id),
        EVIDENCE_FAMILIES[0],
    )
    profiles = [
        evidence for evidence in EVIDENCE_TYPES if evidence["kind"] in family["kinds"]
    ]
    sections = []
    for category, title, inventory in (
        ("analysis", "Analysis methods", ANALYSES),
        ("reports", "Pattern reports", REPORTS),
        ("imports", "Supported imports", IMPORTS),
    ):
        rows = []
        for identifier, row in inventory.items():
            if category == "analysis":
                row = row | {
                    "kinds": OPERATIONS[identifier].kinds,
                    "requirement": _analysis_requirement(identifier),
                }
            inputs = [
                {"kind": evidence["kind"], "label": evidence["label"]}
                for evidence in profiles
                if evidence["kind"] in row["kinds"]
            ]
            if inputs:
                rows.append(
                    {
                        "id": identifier,
                        **row,
                        "short_requirement": SHORT_REQUIREMENTS[row["requirement"]],
                        "evidence_types": inputs,
                    }
                )
        if category == "analysis" and family["id"] == "communications":
            first = (
                "sessions",
                "comm_patterns",
                "conversation_link",
                "session_summaries",
            )
            rows.sort(
                key=lambda row: (
                    first.index(row["id"]) if row["id"] in first else len(first)
                )
            )
        if rows:
            sections.append({"id": category, "title": title, "rows": rows})
    return {
        "evidence_families": [
            {**item, "selected": item["id"] == family["id"]}
            for item in EVIDENCE_FAMILIES
        ],
        "selected_family": family,
        "evidence_groups": profiles,
        "capability_sections": sections,
    }
