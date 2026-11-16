"""Public capability copy; product registries are compared only in tests.

Keep eligibility and prerequisites together when updating a capability. These
descriptions do not inspect this installation's configuration or case material.
"""

INCLUDED = "Included"
OPTIONAL_AI = "Included; AI optional"
MODEL = "Private setup or approved external AI"
GEOGRAPHIC = "Configured geographic service"

TEXT_KINDS = (
    "text",
    "text-message",
    "document",
    "image",
    "audio",
    "phone-call",
    "facetime",
)
LOCATION_KINDS = ("location", "person-location", "vehicle-location")
CALL_KINDS = ("phone-call", "facetime")
COMMUNICATION_KINDS = ("text-message", *CALL_KINDS)
FILE_KINDS = ("image", "audio", "document")

TEXT_MODEL = (
    "Configure a private Ollama text model, or select and approve external AI "
    "through Cloudflare AI Gateway. Usable selected text is required."
)
TEXT_PREREQUISITE = (
    "Text analysis uses original text or an exact retained extraction or "
    "transcript. Files and metadata alone do not supply usable text."
)
LOCATION_PREREQUISITE = (
    "Spatial comparisons need reported coordinates or explicitly accepted "
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
            "Analysis supports uncompressed signed 16-bit PCM WAV, mono/stereo, "
            "8–48 kHz, within the 16 MiB parser limit and 30 decoded minutes. "
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
        "intake": "Add one message or import a supported iMessage CSV or HTML export.",
        "formats": (
            "Individual messages and the documented iMessage export profiles; "
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
            "Call metadata, supplied transcripts and supported PCM WAV audio "
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
            "Call metadata, supplied transcripts and supported PCM WAV audio "
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
        "kinds": ("document",),
        "requirement": INCLUDED,
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
        "kinds": ("image",),
        "requirement": INCLUDED,
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
        "kinds": ("audio", *CALL_KINDS),
        "requirement": INCLUDED,
        "detail": (
            "A supported PCM WAV original or retained call audio addition; no "
            "speech recognition is needed."
        ),
    },
    "ocr": {
        "label": "Read text in images",
        "description": (
            "Read candidate printed-English text with links to its image regions."
        ),
        "kinds": ("image", "document"),
        "requirement": INCLUDED,
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
        "kinds": ("image",),
        "requirement": INCLUDED,
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
        "kinds": ("audio", *CALL_KINDS),
        "requirement": MODEL,
        "detail": (
            "Supported PCM WAV audio. Configure a private Whisper runtime and "
            "model, or explicitly select and approve transcription through "
            "Cloudflare AI Gateway. No speaker identification."
        ),
    },
    "entities": {
        "label": "Extract identifiers and dates",
        "description": (
            "Find explicit identifiers, dates, amounts and coordinates with their "
            "original wording."
        ),
        "kinds": TEXT_KINDS,
        "requirement": INCLUDED,
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
        "kinds": ("text", "text-message"),
        "requirement": INCLUDED,
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
        "kinds": ("text", "text-message"),
        "requirement": INCLUDED,
        "detail": (
            "Select two disjoint groups of original writing and their context. "
            "Reported times support period comparisons; differences establish no "
            "change of writer."
        ),
    },
    "copied_text": {
        "label": "Find repeated wording",
        "description": "Compare matching passages and follow their source positions.",
        "kinds": TEXT_KINDS,
        "requirement": INCLUDED,
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
        "kinds": TEXT_KINDS,
        "requirement": OPTIONAL_AI,
        "detail": (
            "Keywords need usable text only. Optional topic summaries need a "
            "private Ollama text model or explicitly approved external AI through "
            "Cloudflare AI Gateway."
        ),
    },
    "content_events": {
        "label": "Suggest claims and commitments",
        "description": (
            "Suggest attributed claims, plans, commitments, cancellations and "
            "questions with source quotations."
        ),
        "kinds": TEXT_KINDS,
        "requirement": MODEL,
        "detail": TEXT_MODEL + " Model interpretations require review.",
    },
    "sessions": {
        "label": "Read inferred message sessions",
        "description": (
            "Group selected messages into inferred sessions with inspectable "
            "boundaries."
        ),
        "kinds": ("text-message",),
        "requirement": INCLUDED,
        "detail": (
            "Reported participants and message times; gap assumptions and "
            "boundaries can be adjusted. Missing exchanges remain unknown."
        ),
    },
    "comm_patterns": {
        "label": "Inspect message timing",
        "description": "Compare message counts, intervals, turns and selected periods.",
        "kinds": ("text-message",),
        "requirement": INCLUDED,
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
        "kinds": COMMUNICATION_KINDS,
        "requirement": OPTIONAL_AI,
        "detail": (
            "Timing comparisons are included. Optional semantic links need usable "
            "text plus a private Ollama model or approved Gateway AI. Adjacency "
            "proves no shared event or causation."
        ),
    },
    "session_summaries": {
        "label": "Summarize message sessions",
        "description": (
            "Suggest source-cited summaries of selected, inferred message sessions."
        ),
        "kinds": ("text-message",),
        "requirement": MODEL,
        "detail": TEXT_MODEL + " Summaries disclose selection and chunk coverage.",
    },
    "normalize_addresses": {
        "label": "Compare recorded addresses",
        "description": "Compare original and normalized address wording.",
        "kinds": LOCATION_KINDS,
        "requirement": INCLUDED,
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
        "kinds": LOCATION_KINDS,
        "requirement": INCLUDED,
        "detail": "Uses retained observation fields; missing values stay unknown.",
    },
    "spatial_groups": {
        "label": "Group nearby observations",
        "description": (
            "Group positions using a chosen distance and inspect contributing "
            "observations."
        ),
        "kinds": LOCATION_KINDS,
        "requirement": INCLUDED,
        "detail": (
            "Reported coordinates or selected, explicitly accepted geographic "
            "estimates; choose the distance threshold."
        ),
    },
    "proximity": {
        "label": "Find observations in an area",
        "description": "Find positions within a selected radius or polygon.",
        "kinds": LOCATION_KINDS,
        "requirement": INCLUDED,
        "detail": (
            "Usable coordinates and an investigator-supplied point/radius or area."
        ),
    },
    "candidate_visits": {
        "label": "Inspect candidate visits and frequent places",
        "description": (
            "Inspect possible stops, observed spans, frequent places and sampling gaps."
        ),
        "kinds": LOCATION_KINDS,
        "requirement": INCLUDED,
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
        "kinds": LOCATION_KINDS,
        "requirement": INCLUDED,
        "detail": (
            "Usable timed positions. Straight connections represent observations, "
            "not road routes or complete travel histories."
        ),
    },
    "movement_routines": {
        "label": "Compare recurring movement patterns",
        "description": "Compare directed journeys, periods and observation coverage.",
        "kinds": LOCATION_KINDS,
        "requirement": INCLUDED,
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
        "kinds": LOCATION_KINDS,
        "requirement": INCLUDED,
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
        "kinds": LOCATION_KINDS,
        "requirement": INCLUDED,
        "detail": (
            "Usable timed positions. Flags identify observations for review, not "
            "verified travel or wrongdoing."
        ),
    },
    "location_conflicts": {
        "label": "Compare reported location conflicts",
        "description": "Compare selected observations for the same claimed event.",
        "kinds": LOCATION_KINDS,
        "requirement": INCLUDED,
        "detail": (
            "At least two observations and an investigator-supplied event "
            "description. Conflicting claims remain distinct."
        ),
    },
    "geocode_locations": {
        "label": "Look up geographic coordinates or addresses",
        "description": (
            "Find candidate coordinates from an address, or nearby addresses from a "
            "position."
        ),
        "kinds": LOCATION_KINDS,
        "requirement": GEOGRAPHIC,
        "detail": (
            "Configure a Nominatim-compatible private service or approve an "
            "external lookup. Review and accept candidates before downstream "
            "spatial use."
        ),
    },
    "geographic_context": {
        "label": "Look up geographic context",
        "description": (
            "Find contextual roads, venues, buildings and neighbourhood features "
            "near observations."
        ),
        "kinds": LOCATION_KINDS,
        "requirement": GEOGRAPHIC,
        "detail": (
            "Configure an Overpass-compatible private service or approve an "
            "external lookup. Needs usable coordinates; historical context also "
            "needs matching dataset coverage. Map features remain contextual "
            "candidates."
        ),
    },
    "question_synthesis": {
        "label": "Answer a question from selected material",
        "description": (
            "Suggest supported or contrary observations and unknowns with source "
            "citations."
        ),
        "kinds": (*TEXT_KINDS, *LOCATION_KINDS),
        "requirement": MODEL,
        "detail": (
            "A question and a shared selection of usable text or retained findings. "
            "Raw files and structured locations supply no independent text. "
            "Configure a private Ollama model or approve Gateway AI; review "
            "suggestions."
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
            "Timed vehicle observations. Address-only trips need no lookup; "
            "plotting estimated positions needs reviewed geocodes. New observations "
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
    "conversation_import": {
        "label": "Import an iMessage HTML export",
        "description": (
            "Publish mapped message records while retaining the source export and "
            "row provenance."
        ),
        "kinds": ("document", "text-message"),
        "source_kinds": ("document",),
        "requirement": INCLUDED,
        "detail": (
            "The documented UTF-8 HTML profile, up to 2 MiB/500 messages. Choose "
            "timezone and participant mappings; links and attachments are not "
            "fetched. Imported local checks are a separate deliberate action."
        ),
    },
    "imessage_import": {
        "label": "Import iMessage CSV",
        "description": (
            "Review one direct conversation and publish messages with their "
            "retained source context."
        ),
        "kinds": ("document", "text-message"),
        "source_kinds": ("document",),
        "requirement": INCLUDED,
        "detail": (
            "The documented 18-column UTF-8 profile, up to 100 MiB. Confirm "
            "timezone and participants. Publication queues checks for new messages; "
            "model follow-ups need private setup and fail recoverably if "
            "unavailable. Attachment references are not fetched."
        ),
    },
    "vehicle_locations": {
        "label": "Import vehicle locations",
        "description": (
            "Review Asset Location CSV rows and publish vehicle observations with "
            "source-row provenance."
        ),
        "kinds": ("vehicle-location",),
        "source_kinds": (),
        "requirement": INCLUDED,
        "detail": (
            "The documented nine-column UTF-8 CSV profile, up to 100 MiB/100,000 "
            "rows. Confirm timezone, time interpretation and vehicle mappings. "
            "Import performs no external lookup or AI request; this is not a live "
            "tracking feed."
        ),
    },
}


def evidence_groups():
    """Repeat shared capability copy for each supported evidence section."""
    return [
        {
            **evidence,
            **{
                name: [
                    {"id": identifier, **row}
                    for identifier, row in inventory.items()
                    if evidence["kind"] in row["kinds"]
                ]
                for name, inventory in (
                    ("analyses", ANALYSES),
                    ("reports", REPORTS),
                    ("imports", IMPORTS),
                )
            },
        }
        for evidence in EVIDENCE_TYPES
    ]
