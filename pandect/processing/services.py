"""Small, explicit orchestration primitives for durable processing work."""

import logging
import math
import re
import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from pandect.catalog.models import CatalogItem
from pandect.processing.extractors.audio import MAX_FFMPEG_TIMEOUT_SECONDS
from pandect.processing.extractors.audio_splits import (
    DEFAULT_HARD_CUT_OVERLAP_SECONDS,
    DEFAULT_MAX_CHUNK_SECONDS,
    DEFAULT_MIN_SILENCE_SECONDS,
    DEFAULT_SILENCE_WINDOW_SECONDS,
    MAX_PROVIDER_SPLIT_DEPTH,
    MAX_PROVIDER_SPLIT_ENCODINGS,
)
from pandect.processing.extractors.exiftool import EXIFTOOL_TIMEOUT_SECONDS
from pandect.processing.extractors.ffprobe import FFPROBE_TIMEOUT_SECONDS
from pandect.processing.extractors.hashing import (
    FileFingerprint,
    fingerprint_stream,
    sha256_bytes,
)
from pandect.processing.extractors.subprocesses import DEFAULT_OUTPUT_LIMIT_BYTES
from pandect.processing.extractors.tesseract import TESSERACT_TIMEOUT_SECONDS
from pandect.processing.model_manifest import MODEL_ARTIFACTS
from pandect.processing.models import (
    ArtifactPublication,
    DerivedArtifact,
    ProcessingResult,
    ProcessingRun,
    ProcessingStage,
    TimestampClaim,
)
from pandect.processing.remote.cloudflare import MAX_TRANSCRIPTION_AUDIO_BYTES

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 2 * 60 * 60 + 15 * 60
DEFAULT_QUEUED_TIMEOUT_SECONDS = 10 * 60
DEFAULT_RECONCILE_LIMIT = 100
PROCESSOR_VERSION = "2"
LOCAL_PROCESSING_SNAPSHOT_KEY = "local_processing_snapshot"
SOURCE_CHANGED_FAILURE_CODE = "source_changed"
SOURCE_REPLACED_FAILURE_CODE = "source_changed_replacement_recorded"
KEYFRAME_PLAN_UNIT_KEY = "plan"
KEYFRAME_AGGREGATE_UNIT_KEY = "aggregate"
_KEYFRAME_UNIT_PATTERN = re.compile(r"\Aframe:(?P<index>\d{3,})\Z")
_KEYFRAME_PRIORITY_STEP = 2


class ProcessingError(Exception):
    """Base class for safe processing-control failures."""


class UnsupportedProcessingItemError(ProcessingError):
    """The catalogue item has no local processing workflow."""


class SourceUnavailableError(ProcessingError):
    """The canonical input is missing or currently unreadable."""


class StageStateError(ProcessingError):
    """A stage was not in the state required for the requested transition."""


class StageProcessingError(ProcessingError):
    """A processor-reported failure safe to persist without evidence content."""

    def __init__(
        self,
        code: str,
        public_message: str = "",
        *,
        retryable: bool = False,
    ) -> None:
        self.code = _safe_failure_code(code)
        self.public_message = _safe_failure_detail(public_message)
        self.retryable = retryable
        super().__init__(self.public_message or self.code)


def clean_attempt_workspace(
    run_id: UUID | str, stage_id: UUID | str, attempt: int
) -> bool:
    """Remove exactly one non-live attempt workspace, rejecting symlink escapes."""
    if attempt <= 0:
        return False
    work_root = Path(settings.WORK_ROOT).resolve()
    workspace = work_root / str(run_id) / str(stage_id) / f"attempt-{attempt}"
    try:
        resolved = workspace.resolve(strict=False)
    except OSError:
        return False
    # Resolve the configured root first, then require the attempt path to have
    # no symlinked component. This prevents an old attempt symlink from
    # redirecting cleanup onto a newer/live workspace or outside work storage.
    if (
        workspace == work_root
        or work_root not in workspace.parents
        or resolved != workspace
        or workspace.is_symlink()
    ):
        return False
    try:
        shutil.rmtree(workspace)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class StageDefinition:
    kind: str
    dependencies: tuple[str, ...] = ()
    priority: int = 0
    processor_name: str = ""
    unit_key: str = "default"


@dataclass(frozen=True)
class UnitStageDefinition:
    """One explicit artifact-to-stage fan-out in a local workflow."""

    producer_kind: str
    unit_kind: str
    artifact_role: str
    aggregate_unit_key: str


@dataclass(frozen=True)
class ResultSpec:
    result_kind: str
    evidence_tier: str
    data: Any = field(default_factory=dict)
    text: str = ""
    schema_version: int = 1
    confidence: float | None = None
    uncertainty: str = ""
    processor_name: str = ""
    processor_version: str = ""
    prompt_template_version: str = ""
    input_hashes: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    remote_request_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactSpec:
    file_name: str
    role: str
    mime_type: str
    sha256: str
    byte_size: int
    stream_index: int | None = None
    frame_index: int | None = None
    source_start_seconds: Any | None = None
    source_end_seconds: Any | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Transient task-attempt state. It is deliberately not persisted as
    # artifact provenance and is used only to clean an uncommitted publication.
    publication_created: bool = field(default=False, compare=False, repr=False)


@dataclass(frozen=True)
class ConsumedArtifactSpec:
    """One persisted derivative whose exact bytes an outcome consumed."""

    artifact_id: UUID | str
    sha256: str
    byte_size: int


@dataclass(frozen=True)
class TimestampClaimSpec:
    namespace: str
    tag: str
    raw_value: str
    precision: str = TimestampClaim.Precision.UNKNOWN
    parsed_value: Any | None = None
    timezone_evidence: str = ""
    confidence: float | None = None
    conflicts: list[Any] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IdentityObservationSpec:
    modality: str
    unit_key: str
    source_location: dict[str, Any]
    source_sha256: str
    embedding: bytes
    embedding_dimension: int
    feature_space: str
    processor_name: str
    processor_version: str
    preprocessing_version: str
    model_identifier: str = ""
    model_version: str = ""
    model_sha256: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)
    quality_status: str = "unassessed"
    quality_score: float | None = None
    quality_details: dict[str, Any] = field(default_factory=dict)
    sample_duration_ms: int | None = None
    sample_word_count: int | None = None


@dataclass(frozen=True)
class IdentityCandidateSpec:
    """One suggested match against an existing local identity observation."""

    observation_id: UUID | str
    subject_id: UUID | str
    candidate_set_id: UUID | str
    rank: int
    similarity_score: float
    gallery_fingerprint: str
    reference_count: int
    calibration_revision: str


@dataclass(frozen=True)
class StageOutcome:
    results: tuple[ResultSpec, ...] = ()
    artifacts: tuple[ArtifactSpec, ...] = ()
    consumed_artifacts: tuple[ConsumedArtifactSpec, ...] = ()
    timestamp_claims: tuple[TimestampClaimSpec, ...] = ()
    identity_observations: tuple[IdentityObservationSpec, ...] = ()
    identity_candidates: tuple[IdentityCandidateSpec, ...] = ()


@dataclass(frozen=True)
class ReconcileReport:
    queued_recovered: int = 0
    running_recovered: int = 0
    running_failed: int = 0
    replacements_repaired: int = 0
    pending_examined: int = 0


def _discard_abandoned_publications(stage: ProcessingStage) -> None:
    """Clean one expired attempt before making its stable paths reclaimable."""
    from pandect.processing.tasks import _clean_attempt_publications

    _clean_attempt_publications(stage, terminal_only=False)


# This is intentionally a small code-owned map, not a database DAG. Producers
# first persist immutable artifacts; completion then creates one independently
# claimable child row per artifact and a static aggregate row coordinates the
# downstream dependency.
LOCAL_UNIT_STAGE_DEFINITIONS: tuple[UnitStageDefinition, ...] = (
    UnitStageDefinition(
        producer_kind=ProcessingStage.Kind.AUDIO_SPLIT,
        unit_kind=ProcessingStage.Kind.VOICE_ANALYSIS,
        artifact_role=DerivedArtifact.Role.AUDIO_CHUNK,
        aggregate_unit_key="aggregate",
    ),
)
_UNIT_STAGE_BY_PRODUCER = {
    definition.producer_kind: definition for definition in LOCAL_UNIT_STAGE_DEFINITIONS
}
_UNIT_STAGE_BY_KIND = {
    definition.unit_kind: definition for definition in LOCAL_UNIT_STAGE_DEFINITIONS
}
_AGGREGATE_UNIT_BY_KIND = {
    ProcessingStage.Kind.VIDEO_KEYFRAMES: KEYFRAME_AGGREGATE_UNIT_KEY,
    ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS: "aggregate",
    **{
        definition.unit_kind: definition.aggregate_unit_key
        for definition in LOCAL_UNIT_STAGE_DEFINITIONS
    },
}


LOCAL_STAGE_DEFINITIONS: dict[str, tuple[StageDefinition, ...]] = {
    "text_message": (
        StageDefinition(ProcessingStage.Kind.TEXT_EXTRACTION, priority=10),
        StageDefinition(
            ProcessingStage.Kind.WRITING_STYLE,
            (ProcessingStage.Kind.TEXT_EXTRACTION,),
            priority=20,
        ),
        StageDefinition(
            ProcessingStage.Kind.WRITING_IDENTITY,
            (ProcessingStage.Kind.WRITING_STYLE,),
            priority=30,
        ),
    ),
    "image_message": (
        StageDefinition(ProcessingStage.Kind.TEXT_EXTRACTION, priority=10),
        StageDefinition(
            ProcessingStage.Kind.WRITING_STYLE,
            (ProcessingStage.Kind.TEXT_EXTRACTION,),
            priority=20,
        ),
        StageDefinition(
            ProcessingStage.Kind.WRITING_IDENTITY,
            (ProcessingStage.Kind.WRITING_STYLE,),
            priority=30,
        ),
    ),
    "video_message": (
        StageDefinition(ProcessingStage.Kind.TEXT_EXTRACTION, priority=10),
        StageDefinition(
            ProcessingStage.Kind.WRITING_STYLE,
            (ProcessingStage.Kind.TEXT_EXTRACTION,),
            priority=20,
        ),
        StageDefinition(
            ProcessingStage.Kind.WRITING_IDENTITY,
            (ProcessingStage.Kind.WRITING_STYLE,),
            priority=30,
        ),
    ),
    "image": (
        StageDefinition(ProcessingStage.Kind.IMAGE_METADATA, priority=10),
        StageDefinition(ProcessingStage.Kind.IMAGE_OCR, priority=10),
        StageDefinition(ProcessingStage.Kind.IMAGE_QR, priority=10),
        StageDefinition(ProcessingStage.Kind.IMAGE_VISUAL, priority=10),
        StageDefinition(ProcessingStage.Kind.FACE_ANALYSIS, priority=10),
        StageDefinition(
            ProcessingStage.Kind.FACE_IDENTITY,
            (ProcessingStage.Kind.FACE_ANALYSIS,),
            priority=20,
        ),
    ),
    "video": (
        StageDefinition(ProcessingStage.Kind.VIDEO_METADATA, priority=10),
        StageDefinition(
            ProcessingStage.Kind.VIDEO_SUBTITLES,
            (ProcessingStage.Kind.VIDEO_METADATA,),
            priority=20,
        ),
        StageDefinition(
            ProcessingStage.Kind.VIDEO_KEYFRAMES,
            (ProcessingStage.Kind.VIDEO_METADATA,),
            priority=20,
            unit_key=KEYFRAME_PLAN_UNIT_KEY,
        ),
        StageDefinition(
            ProcessingStage.Kind.VIDEO_AUDIO,
            (ProcessingStage.Kind.VIDEO_METADATA,),
            priority=20,
        ),
        StageDefinition(
            ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
            (ProcessingStage.Kind.VIDEO_KEYFRAMES,),
            priority=30,
            unit_key="aggregate",
        ),
        StageDefinition(
            ProcessingStage.Kind.FACE_IDENTITY,
            (ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,),
            priority=40,
        ),
        StageDefinition(
            ProcessingStage.Kind.AUDIO_SPLIT,
            (ProcessingStage.Kind.VIDEO_AUDIO,),
            priority=30,
        ),
        StageDefinition(
            ProcessingStage.Kind.AUDIO_ACOUSTICS,
            (ProcessingStage.Kind.VIDEO_AUDIO,),
            priority=30,
        ),
        StageDefinition(
            ProcessingStage.Kind.VOICE_ANALYSIS,
            (ProcessingStage.Kind.AUDIO_SPLIT,),
            priority=40,
            unit_key="aggregate",
        ),
        StageDefinition(
            ProcessingStage.Kind.VOICE_IDENTITY,
            (ProcessingStage.Kind.VOICE_ANALYSIS,),
            priority=50,
        ),
    ),
    "audio": (
        StageDefinition(ProcessingStage.Kind.AUDIO_METADATA, priority=10),
        StageDefinition(
            ProcessingStage.Kind.AUDIO_SPLIT,
            (ProcessingStage.Kind.AUDIO_METADATA,),
            priority=20,
        ),
        StageDefinition(
            ProcessingStage.Kind.AUDIO_ACOUSTICS,
            (ProcessingStage.Kind.AUDIO_METADATA,),
            priority=20,
        ),
        StageDefinition(
            ProcessingStage.Kind.VOICE_ANALYSIS,
            (ProcessingStage.Kind.AUDIO_SPLIT,),
            priority=30,
            unit_key="aggregate",
        ),
        StageDefinition(
            ProcessingStage.Kind.VOICE_IDENTITY,
            (ProcessingStage.Kind.VOICE_ANALYSIS,),
            priority=40,
        ),
    ),
}

_MESSAGE_ITEM_TYPES = frozenset({"text_message", "image_message", "video_message"})
_FILE_RELATIONS = {"image": "image", "video": "video", "audio": "audio"}
_FAILURE_CODE_PATTERN = re.compile(r"\A[a-z0-9_]{1,64}\Z")
_IDENTITY_REMATCH_STAGE_KINDS = {
    "face": ProcessingStage.Kind.FACE_IDENTITY,
    "voice": ProcessingStage.Kind.VOICE_IDENTITY,
    "writing": ProcessingStage.Kind.WRITING_IDENTITY,
}
IDENTITY_REMATCH_OBSERVATION_LIMIT = 100

_FACE_MODEL_FILES = frozenset(
    {
        "face_detection_yunet_2023mar.onnx",
        "face_recognition_sface_2021dec.onnx",
        "facial_expression_recognition_mobilefacenet_2022july.onnx",
        "age-gender-recognition-retail-0013.xml",
        "age-gender-recognition-retail-0013.bin",
        "head-pose-estimation-adas-0001.xml",
        "head-pose-estimation-adas-0001.bin",
    }
)
_VOICE_MODEL_FILES = frozenset(
    {
        "silero_vad_v4_9e2449e1.onnx",
        "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
    }
)


def local_processing_configuration_snapshot(
    item_type: str,
    definitions: tuple[StageDefinition, ...] | None = None,
) -> dict[str, Any]:
    """Return the safe, effective configuration for one local workflow.

    The snapshot contains no credentials, local source paths, or operator
    evidence. It is persisted with the run so a later re-analysis can
    distinguish changes to stage wiring, resource policy, pinned public model
    sources, revisions, and known provenance limitations.
    """
    selected_definitions = definitions or stage_definitions_for(item_type)
    kinds = {definition.kind for definition in selected_definitions}
    selected_model_files: set[str] = set()
    if kinds & {
        ProcessingStage.Kind.FACE_ANALYSIS,
        ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
    }:
        selected_model_files.update(_FACE_MODEL_FILES)
    if ProcessingStage.Kind.VOICE_ANALYSIS in kinds:
        selected_model_files.update(_VOICE_MODEL_FILES)

    return {
        "schema_version": 4,
        "execution_policy": "isolated-media-runner-v1",
        "processor_version": PROCESSOR_VERSION,
        "item_type": item_type,
        "stages": [
            {
                "kind": definition.kind,
                "dependencies": list(definition.dependencies),
                "priority": definition.priority,
                "processor_name": definition.processor_name or definition.kind,
                "processor_version": PROCESSOR_VERSION,
                "unit_key": definition.unit_key,
            }
            for definition in selected_definitions
        ],
        "resource_limits": {
            "image_max_decoded_pixels": int(settings.PROCESSING_MAX_IMAGE_PIXELS),
            "image_max_decoded_frames": int(settings.PROCESSING_MAX_IMAGE_FRAMES),
            "video_max_local_frames": int(settings.PROCESSING_MAX_VIDEO_FRAMES),
            "subprocess_default_output_bytes": DEFAULT_OUTPUT_LIMIT_BYTES,
            "stage_lease_seconds": DEFAULT_LEASE_SECONDS,
            "queued_recovery_seconds": DEFAULT_QUEUED_TIMEOUT_SECONDS,
            "native_inference_threads": 1,
            "ffmpeg_decoder_threads": 1,
            "ffmpeg_encoder_threads": 1,
            "ffmpeg_filter_threads": 1,
            "ffmpeg_filter_complex_threads": 1,
        },
        "timeouts": {
            "ffprobe_seconds": FFPROBE_TIMEOUT_SECONDS,
            "exiftool_seconds": EXIFTOOL_TIMEOUT_SECONDS,
            "tesseract_seconds_per_frame_or_page": TESSERACT_TIMEOUT_SECONDS,
            "ffmpeg": {
                "formula": "2 * media_duration_seconds + 300",
                "maximum_seconds": MAX_FFMPEG_TIMEOUT_SECONDS,
            },
        },
        "audio_derivatives": {
            "sample_rate_hz": 16_000,
            "channels": 1,
            "codec": "flac",
            "maximum_chunk_seconds": DEFAULT_MAX_CHUNK_SECONDS,
            "preferred_silence_window_seconds": DEFAULT_SILENCE_WINDOW_SECONDS,
            "minimum_silence_seconds": DEFAULT_MIN_SILENCE_SECONDS,
            "hard_cut_overlap_seconds": DEFAULT_HARD_CUT_OVERLAP_SECONDS,
            "provider_payload_limit_bytes": MAX_TRANSCRIPTION_AUDIO_BYTES,
            "provider_split_max_depth": MAX_PROVIDER_SPLIT_DEPTH,
            "provider_split_max_encodings": MAX_PROVIDER_SPLIT_ENCODINGS,
        },
        "models": [
            {
                "name": artifact.name,
                "filename": artifact.filename,
                "revision": artifact.revision,
                "sha256": artifact.sha256,
                "license": artifact.license,
                "required": artifact.required,
                "source": artifact.source_url,
                "note": artifact.note,
                "training_corpus_limitation": (artifact.training_corpus_limitation),
                "legal_references": [
                    {
                        "name": reference.name,
                        "source": reference.source_url,
                        "archive": f"legal/{reference.document.filename}",
                        "sha256": reference.document.sha256,
                        "note": reference.note,
                    }
                    for reference in artifact.legal_references
                ],
            }
            for artifact in MODEL_ARTIFACTS
            if artifact.filename in selected_model_files
        ],
    }


def _local_run_configuration(
    item_type: str,
    definitions: tuple[StageDefinition, ...],
    requested: dict[str, Any] | None,
) -> dict[str, Any]:
    configuration = dict(requested or {})
    if LOCAL_PROCESSING_SNAPSHOT_KEY in configuration:
        raise ValueError(
            f"{LOCAL_PROCESSING_SNAPSHOT_KEY!r} is reserved for effective run state"
        )
    configuration[LOCAL_PROCESSING_SNAPSHOT_KEY] = (
        local_processing_configuration_snapshot(item_type, definitions)
    )
    return configuration


def _safe_failure_code(value: str) -> str:
    if _FAILURE_CODE_PATTERN.fullmatch(value or ""):
        return value
    return "processing_error"


def _safe_failure_detail(value: str) -> str:
    visible = "".join(character for character in value if character.isprintable())
    return visible[:512]


def _catalog_item(item_or_id: CatalogItem | UUID | str) -> CatalogItem:
    if isinstance(item_or_id, CatalogItem):
        return item_or_id
    return CatalogItem.objects.get(pk=item_or_id)


def source_fingerprint(item_or_id: CatalogItem | UUID | str) -> FileFingerprint:
    """Hash the canonical file or exact message body without changing it."""
    item = _catalog_item(item_or_id)
    if item.item_type in _MESSAGE_ITEM_TYPES:
        try:
            body = item.message.body
        except ObjectDoesNotExist as error:
            raise SourceUnavailableError("Message source is unavailable.") from error
        if not body.strip():
            raise SourceUnavailableError("Message has no body to process.")
        encoded = body.encode("utf-8")
        return FileFingerprint(sha256=sha256_bytes(encoded), size_bytes=len(encoded))

    relation_name = _FILE_RELATIONS.get(item.item_type)
    if relation_name is None:
        raise UnsupportedProcessingItemError(
            f"Item type {item.item_type!r} has no local processing workflow."
        )
    try:
        extension = getattr(item, relation_name)
        evidence_file = extension.file
    except ObjectDoesNotExist as error:
        raise SourceUnavailableError("Media source is unavailable.") from error
    if not evidence_file or not evidence_file.name:
        raise SourceUnavailableError("Media source is unavailable.")
    try:
        with evidence_file.open("rb") as stream:
            return fingerprint_stream(stream, preserve_position=False)
    except (FileNotFoundError, OSError) as error:
        raise SourceUnavailableError("Media source is unreadable.") from error


def stage_definitions_for(item_type: str) -> tuple[StageDefinition, ...]:
    try:
        return LOCAL_STAGE_DEFINITIONS[item_type]
    except KeyError as error:
        raise UnsupportedProcessingItemError(
            f"Item type {item_type!r} has no local processing workflow."
        ) from error


def _definition_for(item_type: str, kind: str) -> StageDefinition | None:
    return next(
        (
            definition
            for definition in LOCAL_STAGE_DEFINITIONS.get(item_type, ())
            if definition.kind == kind
        ),
        None,
    )


def _unit_key_for_artifact(
    definition: UnitStageDefinition,
    artifact: DerivedArtifact,
) -> str:
    """Return the bounded deterministic key for one supported unit artifact."""
    if definition.unit_kind == ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS:
        if artifact.frame_index is None:
            raise StageStateError("A keyframe is missing its frame index.")
        return f"frame:{artifact.frame_index:03d}"
    if definition.unit_kind == ProcessingStage.Kind.VOICE_ANALYSIS:
        chunk_index = artifact.metadata.get("chunk_index")
        if (
            artifact.stream_index is None
            or not isinstance(chunk_index, int)
            or isinstance(chunk_index, bool)
            or chunk_index < 0
        ):
            raise StageStateError("An audio chunk is missing its unit coordinates.")
        return f"stream:{artifact.stream_index}/chunk:{chunk_index:03d}"
    raise StageStateError("The local unit stage map is invalid.")


def _keyframe_plan_frames(producer: ProcessingStage) -> list[dict[str, Any]]:
    """Validate the bounded deterministic plan before creating child rows."""
    result = (
        ProcessingResult.objects.filter(
            stage=producer,
            result_kind=ProcessingResult.Kind.KEYFRAMES,
        )
        .order_by("created_at", "pk")
        .first()
    )
    data = result.data if result is not None and isinstance(result.data, dict) else {}
    frames = data.get("frames")
    if (
        data.get("phase") != "plan"
        or not isinstance(frames, list)
        or not frames
        or len(frames) > 120
    ):
        raise StageStateError("The keyframe plan is unavailable or invalid.")
    validated = []
    seen_units = set()
    for expected_index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise StageStateError("The keyframe plan contains an invalid unit.")
        index = frame.get("selection_index")
        unit_key = frame.get("unit_key")
        stream_index = frame.get("source_stream_index")
        timestamp = frame.get("timestamp_seconds")
        reason = frame.get("selection_reason")
        scene_score = frame.get("scene_change_score")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index != expected_index
            or unit_key != f"frame:{index:03d}"
            or unit_key in seen_units
            or not isinstance(stream_index, int)
            or isinstance(stream_index, bool)
            or stream_index < 0
            or not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not math.isfinite(float(timestamp))
            or float(timestamp) < 0
            or reason
            not in {"first_frame", "last_frame", "scene_change", "even_coverage"}
            or (
                scene_score is not None
                and (
                    not isinstance(scene_score, (int, float))
                    or isinstance(scene_score, bool)
                    or not math.isfinite(float(scene_score))
                    or not 0 <= float(scene_score) <= 1
                )
            )
            or (reason == "scene_change") != (scene_score is not None)
        ):
            raise StageStateError("The keyframe plan contains an invalid unit.")
        seen_units.add(unit_key)
        validated.append(frame)
    return validated


def _materialize_keyframe_extraction_stages(producer: ProcessingStage) -> int:
    """Create one durable extraction row per persisted plan coordinate."""
    frames = _keyframe_plan_frames(producer)
    definitions = [
        ProcessingStage(
            run=producer.run,
            kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
            unit_key=frame["unit_key"],
            priority=producer.priority + index * _KEYFRAME_PRIORITY_STEP,
            processor_name="pandect.video-keyframe-extraction",
            processor_version=producer.processor_version,
            schema_version=producer.schema_version,
            max_attempts=producer.max_attempts,
        )
        for index, frame in enumerate(frames)
    ]
    definitions.append(
        ProcessingStage(
            run=producer.run,
            kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
            unit_key=KEYFRAME_AGGREGATE_UNIT_KEY,
            priority=(producer.priority + len(frames) * _KEYFRAME_PRIORITY_STEP + 1),
            processor_name="pandect.video-keyframe-aggregate",
            processor_version=producer.processor_version,
            schema_version=producer.schema_version,
            max_attempts=producer.max_attempts,
        )
    )
    unit_keys = [stage.unit_key for stage in definitions]
    existing = set(
        ProcessingStage.objects.filter(
            run=producer.run,
            kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
            unit_key__in=unit_keys,
        ).values_list("unit_key", flat=True)
    )
    missing = [stage for stage in definitions if stage.unit_key not in existing]
    ProcessingStage.objects.bulk_create(missing, ignore_conflicts=True)
    return len(missing)


def _materialize_frame_analysis_stage(producer: ProcessingStage) -> int:
    """Fan out analysis only after one keyframe artifact commits successfully."""
    match = _KEYFRAME_UNIT_PATTERN.fullmatch(producer.unit_key)
    if match is None:
        return 0
    frame_index = int(match["index"])
    artifacts = list(
        DerivedArtifact.objects.filter(
            stage=producer,
            role=DerivedArtifact.Role.KEYFRAME,
        ).order_by("created_at", "pk")[:2]
    )
    if len(artifacts) != 1 or artifacts[0].frame_index != frame_index:
        raise StageStateError("A keyframe extraction unit has invalid artifacts.")
    aggregate = ProcessingStage.objects.filter(
        run=producer.run,
        kind=ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
        unit_key="aggregate",
    ).first()
    if aggregate is None:
        raise StageStateError("The video frame aggregate is unavailable.")
    _, created = ProcessingStage.objects.get_or_create(
        run=producer.run,
        kind=ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
        unit_key=producer.unit_key,
        defaults={
            "priority": producer.priority + 1,
            "processor_name": aggregate.processor_name,
            "processor_version": aggregate.processor_version,
            "schema_version": aggregate.schema_version,
            "max_attempts": aggregate.max_attempts,
        },
    )
    return int(created)


def materialize_unit_stages(producer: ProcessingStage) -> int:
    """Create the explicit per-artifact work rows for one completed producer.

    The caller owns the surrounding completion transaction, so artifacts and
    their child stage rows become visible atomically. The unique stage-unit
    constraint makes a repeated call harmless.
    """
    if producer.kind == ProcessingStage.Kind.VIDEO_KEYFRAMES:
        if producer.unit_key in (KEYFRAME_PLAN_UNIT_KEY, "default"):
            return _materialize_keyframe_extraction_stages(producer)
        return _materialize_frame_analysis_stage(producer)

    definition = _UNIT_STAGE_BY_PRODUCER.get(producer.kind)
    if definition is None:
        return 0
    aggregate = (
        ProcessingStage.objects.filter(
            run=producer.run,
            kind=definition.unit_kind,
            unit_key__in=(definition.aggregate_unit_key, "default"),
        )
        .order_by("unit_key")
        .first()
    )
    if aggregate is None:
        raise StageStateError("The aggregate unit stage is unavailable.")
    artifacts = list(
        DerivedArtifact.objects.filter(
            stage=producer,
            role=definition.artifact_role,
        ).order_by("stream_index", "frame_index", "source_start_seconds", "pk")
    )
    unit_keys = [_unit_key_for_artifact(definition, artifact) for artifact in artifacts]
    if len(unit_keys) != len(set(unit_keys)):
        raise StageStateError("Unit artifact coordinates are not unique.")
    existing = set(
        ProcessingStage.objects.filter(
            run=producer.run,
            kind=definition.unit_kind,
            unit_key__in=unit_keys,
        ).values_list("unit_key", flat=True)
    )
    missing = [unit_key for unit_key in unit_keys if unit_key not in existing]
    ProcessingStage.objects.bulk_create(
        [
            ProcessingStage(
                run=producer.run,
                kind=definition.unit_kind,
                unit_key=unit_key,
                priority=aggregate.priority,
                processor_name=aggregate.processor_name,
                processor_version=aggregate.processor_version,
                schema_version=aggregate.schema_version,
                max_attempts=aggregate.max_attempts,
            )
            for unit_key in missing
        ],
        ignore_conflicts=True,
    )
    return len(missing)


@transaction.atomic
def supersede_active_processing_runs(
    item_or_id: CatalogItem | UUID | str,
    *,
    current_fingerprint: FileFingerprint | None = None,
    replacement_recorded: bool = False,
) -> int:
    """Fence unfinished local and remote work that no longer has this source.

    ``current_fingerprint=None`` is the explicit unavailable-source case used
    when an optional media-message caption becomes blank. Terminal runs and
    their evidence remain immutable history; only work that could still append
    stale output is superseded.
    """
    item = _catalog_item(item_or_id)
    active_statuses = (
        ProcessingRun.Status.PENDING,
        ProcessingRun.Status.RUNNING,
    )
    now = timezone.now()
    changed_runs = ProcessingRun.objects.select_for_update().filter(
        catalog_item=item,
        status__in=active_statuses,
    )
    if current_fingerprint is not None:
        changed_runs = changed_runs.exclude(
            source_fingerprint=current_fingerprint.sha256,
            source_size=current_fingerprint.size_bytes,
        )
    run_ids = list(changed_runs.values_list("pk", flat=True))
    if not run_ids:
        return 0
    if current_fingerprint is None:
        failure_code = "source_unavailable"
    elif replacement_recorded:
        failure_code = SOURCE_REPLACED_FAILURE_CODE
    else:
        failure_code = SOURCE_CHANGED_FAILURE_CODE
    ProcessingStage.objects.filter(run_id__in=run_ids).exclude(
        status__in=(
            ProcessingStage.Status.SUCCEEDED,
            ProcessingStage.Status.FAILED,
            ProcessingStage.Status.SKIPPED,
            ProcessingStage.Status.SUPERSEDED,
        )
    ).update(
        status=ProcessingStage.Status.SUPERSEDED,
        failure_code=failure_code,
        failure_detail="",
        lease_expires_at=None,
        finished_at=now,
        updated_at=now,
    )
    changed_runs.update(
        status=ProcessingRun.Status.SUPERSEDED,
        finished_at=now,
        updated_at=now,
    )
    return len(run_ids)


def create_local_run(
    item_or_id: CatalogItem | UUID | str,
    *,
    trigger: str = ProcessingRun.Trigger.AUTOMATIC,
    configuration: dict[str, Any] | None = None,
    enqueue: bool = True,
) -> ProcessingRun:
    """Create a versioned local workflow and enqueue it only after commit."""
    item = _catalog_item(item_or_id)
    fingerprint = source_fingerprint(item)
    definitions = stage_definitions_for(item.item_type)
    effective_configuration = _local_run_configuration(
        item.item_type,
        definitions,
        configuration,
    )

    with transaction.atomic():
        # Item-first locking keeps run creation and source-change replacement
        # on one ordering when a manual action and reconciler overlap.
        item = CatalogItem.objects.select_for_update().get(pk=item.pk)
        supersede_active_processing_runs(
            item,
            current_fingerprint=fingerprint,
            replacement_recorded=True,
        )
        run = ProcessingRun.objects.create(
            catalog_item=item,
            trigger=trigger,
            scope=ProcessingRun.Scope.LOCAL,
            source_fingerprint=fingerprint.sha256,
            source_size=fingerprint.size_bytes,
            processor_version=PROCESSOR_VERSION,
            configuration=effective_configuration,
        )
        ProcessingStage.objects.bulk_create(
            [
                ProcessingStage(
                    run=run,
                    kind=definition.kind,
                    unit_key=definition.unit_key,
                    priority=definition.priority,
                    processor_name=definition.processor_name,
                    processor_version=PROCESSOR_VERSION,
                )
                for definition in definitions
            ]
        )
        if enqueue:
            enqueue_run_on_commit(run)
    return run


def create_identity_rematch_run(
    item_or_id: CatalogItem | UUID | str,
    modality: str,
    *,
    gallery_change: dict[str, Any] | None = None,
    enqueue: bool = True,
) -> ProcessingRun:
    """Create a bounded, identity-only run for one item's current source.

    The run reuses stored opaque features and appends a new candidate/result
    version. It never regenerates media derivatives or changes canonical item
    fields. A stale observation is not rematched against a replaced source.
    """
    try:
        stage_kind = _IDENTITY_REMATCH_STAGE_KINDS[modality]
    except KeyError as error:
        raise ValueError("Select a supported identity modality.") from error

    item = _catalog_item(item_or_id)
    fingerprint = source_fingerprint(item)
    change_snapshot = {
        key: str(value) if isinstance(value, UUID) else value
        for key, value in (gallery_change or {}).items()
    }
    definition = StageDefinition(
        stage_kind,
        priority=10,
        processor_name="pandect.closed-gallery",
    )
    configuration = _local_run_configuration(
        item.item_type,
        (definition,),
        {
            "workflow": "identity_gallery_rematch",
            "modality": modality,
            "observation_limit": IDENTITY_REMATCH_OBSERVATION_LIMIT,
            "gallery_change": change_snapshot,
        },
    )
    with transaction.atomic():
        run = ProcessingRun.objects.create(
            catalog_item=item,
            trigger=ProcessingRun.Trigger.MANUAL,
            scope=ProcessingRun.Scope.LOCAL,
            source_fingerprint=fingerprint.sha256,
            source_size=fingerprint.size_bytes,
            processor_version=PROCESSOR_VERSION,
            configuration=configuration,
        )
        ProcessingStage.objects.create(
            run=run,
            kind=stage_kind,
            priority=10,
            processor_name="pandect.closed-gallery",
            processor_version=PROCESSOR_VERSION,
        )
        if enqueue:
            enqueue_run_on_commit(run)
    return run


def enqueue_run_on_commit(run_or_id: ProcessingRun | UUID | str) -> None:
    """Register a UUID-only enqueue callback at the current commit boundary."""
    run_id = run_or_id.pk if isinstance(run_or_id, ProcessingRun) else run_or_id
    serialized_id = str(run_id)
    transaction.on_commit(lambda: _enqueue_ready_stages_safely(serialized_id))


def _enqueue_ready_stages_safely(run_id: UUID | str) -> None:
    """Keep a committed run recoverable when its immediate dispatch fails."""
    try:
        enqueue_ready_stages(run_id)
    except Exception as error:  # noqa: BLE001 - reconciliation owns recovery.
        logger.error(
            "Processing enqueue callback failed: run_id=%s exception_type=%s",
            run_id,
            type(error).__name__,
        )


def _dispatch_stage_task(stage_id: UUID | str):
    stage_state = (
        ProcessingStage.objects.filter(pk=stage_id)
        .values_list("run__scope", "priority")
        .first()
    )
    if stage_state is None:
        return None
    scope, stage_priority = stage_state
    # ProcessingStage uses lower values for earlier work. Huey's SQLite queue
    # dequeues higher priorities first, so invert the small, persisted value.
    huey_priority = -int(stage_priority)
    if scope == ProcessingRun.Scope.REMOTE:
        from pandect.processing.remote.tasks import run_remote_processing_stage

        return run_remote_processing_stage(str(stage_id), priority=huey_priority)

    from pandect.processing.tasks import run_processing_stage

    return run_processing_stage(str(stage_id), priority=huey_priority)


def enqueue_stage(stage_or_id: ProcessingStage | UUID | str) -> bool:
    """Move one pending stage to queued before handing it to Huey."""
    stage_id = (
        stage_or_id.pk if isinstance(stage_or_id, ProcessingStage) else stage_or_id
    )
    now = timezone.now()
    changed = ProcessingStage.objects.filter(
        pk=stage_id,
        status=ProcessingStage.Status.PENDING,
        attempts__lt=F("max_attempts"),
        run__status__in=(ProcessingRun.Status.PENDING, ProcessingRun.Status.RUNNING),
    ).update(
        status=ProcessingStage.Status.QUEUED,
        queued_at=now,
        task_id="",
        failure_code="",
        failure_detail="",
        finished_at=None,
        updated_at=now,
    )
    if not changed:
        return False

    result = _dispatch_stage_task(stage_id)
    task_id = str(getattr(result, "id", ""))[:64]
    if task_id:
        ProcessingStage.objects.filter(pk=stage_id).update(
            task_id=task_id,
            updated_at=timezone.now(),
        )
    return True


def enqueue_ready_stages(run_or_id: ProcessingRun | UUID | str) -> int:
    """Queue dependency-ready stages and skip permanently blocked stages."""
    run_id = run_or_id.pk if isinstance(run_or_id, ProcessingRun) else run_or_id
    try:
        run = ProcessingRun.objects.select_related("catalog_item").get(pk=run_id)
    except ProcessingRun.DoesNotExist:
        return 0
    if run.status not in (ProcessingRun.Status.PENDING, ProcessingRun.Status.RUNNING):
        return 0
    if run.scope == ProcessingRun.Scope.REMOTE:
        from pandect.processing.remote.workflows import enqueue_remote_run

        return enqueue_remote_run(run.pk)

    stages = list(run.stages.all())

    def dependency_stage(stage: ProcessingStage, kind: str) -> ProcessingStage | None:
        if (
            stage.kind == ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS
            and _KEYFRAME_UNIT_PATTERN.fullmatch(stage.unit_key)
            and kind == ProcessingStage.Kind.VIDEO_KEYFRAMES
        ):
            preferred_unit = stage.unit_key
        else:
            preferred_unit = _AGGREGATE_UNIT_BY_KIND.get(kind, "default")
        fallback_units = (
            (KEYFRAME_PLAN_UNIT_KEY, "default")
            if kind == ProcessingStage.Kind.VIDEO_KEYFRAMES
            else ("default",)
        )
        for unit_key in (preferred_unit, *fallback_units):
            dependency = next(
                (
                    candidate
                    for candidate in stages
                    if candidate.kind == kind and candidate.unit_key == unit_key
                ),
                None,
            )
            if dependency is not None:
                return dependency
        return None

    queued = 0
    blocked_statuses = {
        ProcessingStage.Status.FAILED: "dependency_failed",
        ProcessingStage.Status.SKIPPED: "dependency_skipped",
        ProcessingStage.Status.SUPERSEDED: "dependency_superseded",
    }
    for stage in stages:
        if stage.status != ProcessingStage.Status.PENDING:
            continue
        definition = _definition_for(run.catalog_item.item_type, stage.kind)
        is_identity_rematch = (
            run.configuration.get("workflow") == "identity_gallery_rematch"
            and _IDENTITY_REMATCH_STAGE_KINDS.get(run.configuration.get("modality", ""))
            == stage.kind
        )
        dependencies = (
            () if is_identity_rematch else definition.dependencies if definition else ()
        )
        dependency_stages = [dependency_stage(stage, kind) for kind in dependencies]
        dependency_statuses = [
            dependency.status if dependency is not None else None
            for dependency in dependency_stages
        ]
        blocker = next(
            (
                blocked_statuses[status]
                for status in dependency_statuses
                if status in blocked_statuses
            ),
            None,
        )
        if blocker:
            if skip_stage(stage.pk, blocker):
                stage.status = ProcessingStage.Status.SKIPPED
            continue
        if not all(
            status == ProcessingStage.Status.SUCCEEDED for status in dependency_statuses
        ):
            continue

        aggregate_unit_key = _AGGREGATE_UNIT_BY_KIND.get(stage.kind)
        is_aggregate = bool(
            aggregate_unit_key and stage.unit_key in (aggregate_unit_key, "default")
        )
        if is_aggregate:
            siblings = [
                candidate
                for candidate in stages
                if candidate.kind == stage.kind and candidate.pk != stage.pk
            ]
            if any(
                sibling.status
                in (
                    ProcessingStage.Status.PENDING,
                    ProcessingStage.Status.QUEUED,
                    ProcessingStage.Status.RUNNING,
                )
                for sibling in siblings
            ):
                continue
        if enqueue_stage(stage.pk):
            stage.status = ProcessingStage.Status.QUEUED
            queued += 1
    recompute_run_status(run.pk)
    return queued


def claim_stage(
    stage_id: UUID | str,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> ProcessingStage | None:
    """Atomically claim a queued stage; duplicate deliveries return ``None``."""
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    now = timezone.now()
    changed = ProcessingStage.objects.filter(
        pk=stage_id,
        status=ProcessingStage.Status.QUEUED,
        attempts__lt=F("max_attempts"),
        run__status__in=(ProcessingRun.Status.PENDING, ProcessingRun.Status.RUNNING),
    ).update(
        status=ProcessingStage.Status.RUNNING,
        attempts=F("attempts") + 1,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
        finished_at=None,
        updated_at=now,
    )
    if not changed:
        return None
    stage = ProcessingStage.objects.select_related("run__catalog_item").get(pk=stage_id)
    ProcessingRun.objects.filter(
        pk=stage.run_id,
        status=ProcessingRun.Status.PENDING,
    ).update(
        status=ProcessingRun.Status.RUNNING,
        started_at=now,
        updated_at=now,
    )
    return stage


def heartbeat_stage(
    stage_id: UUID | str,
    *,
    expected_attempt: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Extend only the lease owned by one specific claimed attempt."""
    if expected_attempt <= 0:
        raise ValueError("expected_attempt must be positive")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    now = timezone.now()
    return bool(
        ProcessingStage.objects.filter(
            pk=stage_id,
            status=ProcessingStage.Status.RUNNING,
            attempts=expected_attempt,
        ).update(
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        )
    )


def _mark_run_superseded_locked(run: ProcessingRun, *, now) -> None:
    """Supersede one row already fenced by its surrounding transaction."""
    terminal_at = run.finished_at or now
    ProcessingStage.objects.select_for_update().filter(run_id=run.pk).exclude(
        status__in=(
            ProcessingStage.Status.SUCCEEDED,
            ProcessingStage.Status.FAILED,
            ProcessingStage.Status.SKIPPED,
            ProcessingStage.Status.SUPERSEDED,
        )
    ).update(
        status=ProcessingStage.Status.SUPERSEDED,
        failure_code=SOURCE_CHANGED_FAILURE_CODE,
        failure_detail="",
        lease_expires_at=None,
        finished_at=terminal_at,
        updated_at=now,
    )
    if run.status == ProcessingRun.Status.SUPERSEDED and run.finished_at is not None:
        return
    ProcessingRun.objects.filter(
        pk=run.pk,
    ).update(
        status=ProcessingRun.Status.SUPERSEDED,
        finished_at=terminal_at,
        updated_at=now,
    )
    run.status = ProcessingRun.Status.SUPERSEDED
    run.finished_at = terminal_at
    run.updated_at = now


@transaction.atomic
def mark_run_superseded(run_or_id: ProcessingRun | UUID | str) -> None:
    """Terminally stop unfinished work after its canonical input changes."""
    run_id = run_or_id.pk if isinstance(run_or_id, ProcessingRun) else run_or_id
    try:
        run = ProcessingRun.objects.select_for_update().get(pk=run_id)
    except ProcessingRun.DoesNotExist:
        return
    _mark_run_superseded_locked(run, now=timezone.now())


def _current_full_local_run(
    item: CatalogItem,
    fingerprint: FileFingerprint,
    *,
    exclude_run_id: UUID | str,
) -> ProcessingRun | None:
    """Return a durable full local run already bound to the current source."""
    candidates = (
        ProcessingRun.objects.filter(
            catalog_item=item,
            scope=ProcessingRun.Scope.LOCAL,
            source_fingerprint=fingerprint.sha256,
            source_size=fingerprint.size_bytes,
            processor_version=PROCESSOR_VERSION,
        )
        .exclude(
            pk=exclude_run_id,
        )
        .exclude(
            status=ProcessingRun.Status.SUPERSEDED,
        )
        .order_by("-created_at", "id")
    )
    for candidate in candidates:
        configuration = (
            candidate.configuration if isinstance(candidate.configuration, dict) else {}
        )
        if configuration.get("workflow") != "identity_gallery_rematch":
            return candidate
    return None


def _record_source_replacement(run_id: UUID | str, *, now=None) -> None:
    """Mark superseded stage state once a current-source run is durable."""
    ProcessingStage.objects.filter(
        run_id=run_id,
        status=ProcessingStage.Status.SUPERSEDED,
        failure_code=SOURCE_CHANGED_FAILURE_CODE,
    ).update(
        failure_code=SOURCE_REPLACED_FAILURE_CODE,
        failure_detail="",
        updated_at=now or timezone.now(),
    )


@transaction.atomic
def supersede_and_replace_local_run(
    run_or_id: ProcessingRun | UUID | str,
) -> ProcessingRun | None:
    """Supersede changed local work and create one run for the current source.

    The replacement is intentionally limited to local runs. Remote work needs a
    new item-specific consent and must never be recreated automatically.
    """
    run_id = run_or_id.pk if isinstance(run_or_id, ProcessingRun) else run_or_id
    try:
        catalog_item_id = ProcessingRun.objects.values_list(
            "catalog_item_id", flat=True
        ).get(pk=run_id)
        item = CatalogItem.objects.select_for_update().get(pk=catalog_item_id)
        run = (
            ProcessingRun.objects.select_for_update()
            .select_related("catalog_item")
            .get(pk=run_id)
        )
    except (CatalogItem.DoesNotExist, ProcessingRun.DoesNotExist):
        return None
    # The catalogue-item fence serializes competing replacement attempts on
    # databases with row locks. The surrounding write transaction provides the
    # equivalent single-writer fence in the supported SQLite deployment.
    _mark_run_superseded_locked(run, now=timezone.now())
    if run.scope != ProcessingRun.Scope.LOCAL:
        return None

    try:
        current = source_fingerprint(item)
    except ProcessingError:
        return None
    # Fence every other still-active run bound to the old source in the same
    # commit. Remote runs are stopped but never recreated without new consent.
    supersede_active_processing_runs(
        item,
        current_fingerprint=current,
        replacement_recorded=True,
    )
    existing = _current_full_local_run(
        item,
        current,
        exclude_run_id=run.pk,
    )
    if existing is not None:
        _record_source_replacement(run.pk)
        return existing
    definitions = stage_definitions_for(item.item_type)
    configuration = _local_run_configuration(
        item.item_type,
        definitions,
        {
            "replacement_for_run": str(run.pk),
            "reason": "source_changed_during_processing",
        },
    )
    replacement = ProcessingRun.objects.create(
        catalog_item=item,
        trigger=ProcessingRun.Trigger.AUTOMATIC,
        scope=ProcessingRun.Scope.LOCAL,
        source_fingerprint=current.sha256,
        source_size=current.size_bytes,
        processor_version=PROCESSOR_VERSION,
        configuration=configuration,
    )
    ProcessingStage.objects.bulk_create(
        [
            ProcessingStage(
                run=replacement,
                kind=definition.kind,
                unit_key=definition.unit_key,
                priority=definition.priority,
                processor_name=definition.processor_name,
                processor_version=PROCESSOR_VERSION,
            )
            for definition in definitions
        ]
    )
    _record_source_replacement(run.pk)
    enqueue_run_on_commit(replacement)
    return replacement


def _result_from_spec(stage: ProcessingStage, spec: ResultSpec) -> ProcessingResult:
    result = ProcessingResult(
        stage=stage,
        result_kind=spec.result_kind,
        evidence_tier=spec.evidence_tier,
        schema_version=spec.schema_version,
        text=spec.text,
        data=spec.data,
        confidence=spec.confidence,
        uncertainty=spec.uncertainty,
        processor_name=spec.processor_name or stage.processor_name or stage.kind,
        processor_version=spec.processor_version or stage.processor_version,
        prompt_template_version=spec.prompt_template_version,
        input_hashes=spec.input_hashes,
        provenance=spec.provenance,
        remote_request_metadata=spec.remote_request_metadata,
    )
    result.full_clean()
    return result


def _artifact_from_spec(stage: ProcessingStage, spec: ArtifactSpec) -> DerivedArtifact:
    normalized_name = str(spec.file_name).replace("\\", "/")
    if (
        normalized_name.startswith("/")
        or ".." in normalized_name.split("/")
        or not normalized_name.startswith(
            f"derived/{stage.run.catalog_item_id}/{stage.run_id}/"
        )
    ):
        raise StageStateError("Derived artifact path is outside this run.")
    try:
        with default_storage.open(normalized_name, "rb") as stream:
            observed = fingerprint_stream(stream, preserve_position=False)
    except (FileNotFoundError, OSError, ValueError) as error:
        raise StageStateError("Derived artifact file is unavailable.") from error
    if observed.sha256 != spec.sha256 or observed.size_bytes != spec.byte_size:
        raise StageStateError("Derived artifact does not match its record.")
    artifact = DerivedArtifact(
        stage=stage,
        catalog_item=stage.run.catalog_item,
        file=normalized_name,
        role=spec.role,
        mime_type=spec.mime_type,
        sha256=spec.sha256,
        byte_size=spec.byte_size,
        stream_index=spec.stream_index,
        frame_index=spec.frame_index,
        source_start_seconds=spec.source_start_seconds,
        source_end_seconds=spec.source_end_seconds,
        metadata=spec.metadata,
    )
    artifact.full_clean()
    return artifact


def _timestamp_from_spec(
    stage: ProcessingStage,
    spec: TimestampClaimSpec,
) -> TimestampClaim:
    claim = TimestampClaim(
        stage=stage,
        catalog_item=stage.run.catalog_item,
        namespace=spec.namespace,
        tag=spec.tag,
        raw_value=spec.raw_value,
        precision=spec.precision,
        parsed_value=spec.parsed_value,
        timezone_evidence=spec.timezone_evidence,
        confidence=spec.confidence,
        conflicts=spec.conflicts,
        provenance=spec.provenance or {"source": "processing_stage"},
    )
    claim.full_clean()
    return claim


def _identity_observation_from_spec(stage, spec: IdentityObservationSpec):
    """Build a validated local observation without exposing embeddings to logs."""
    from pandect.identities.models import IdentityObservation

    provenance = dict(spec.provenance)
    provenance.update(
        {
            "processing_run_id": str(stage.run_id),
            "processing_stage_id": str(stage.pk),
        }
    )
    observation = IdentityObservation(
        catalog_item=stage.run.catalog_item,
        modality=spec.modality,
        unit_key=spec.unit_key,
        source_location=spec.source_location,
        source_sha256=spec.source_sha256,
        embedding=spec.embedding,
        embedding_dimension=spec.embedding_dimension,
        feature_space=spec.feature_space,
        processor_name=spec.processor_name,
        processor_version=spec.processor_version,
        model_identifier=spec.model_identifier,
        model_version=spec.model_version,
        model_sha256=spec.model_sha256,
        preprocessing_version=spec.preprocessing_version,
        provenance=provenance,
        quality_status=spec.quality_status,
        quality_score=spec.quality_score,
        quality_details=spec.quality_details or {"status": "not_assessed"},
        sample_duration_ms=spec.sample_duration_ms,
        sample_word_count=spec.sample_word_count,
    )
    observation.full_clean()
    return observation


def _identity_candidate_from_spec(stage, spec: IdentityCandidateSpec):
    """Build a validated suggestion using its observation's fixed quality data."""
    from django.core.exceptions import ValidationError

    from pandect.identities.models import (
        IdentityCandidate,
        IdentityObservation,
        Subject,
    )

    try:
        observation = IdentityObservation.objects.get(pk=spec.observation_id)
    except IdentityObservation.DoesNotExist as error:
        raise ValidationError(
            {"identity_candidates": "Candidate observation does not exist."}
        ) from error
    if observation.catalog_item_id != stage.run.catalog_item_id:
        raise ValidationError(
            {
                "identity_candidates": (
                    "Candidate observation must belong to the processing run item."
                )
            }
        )
    try:
        subject = Subject.objects.get(pk=spec.subject_id)
    except Subject.DoesNotExist as error:
        raise ValidationError(
            {"identity_candidates": "Candidate subject does not exist."}
        ) from error

    candidate = IdentityCandidate(
        candidate_set_id=spec.candidate_set_id,
        producing_run=stage.run,
        producing_stage=stage,
        observation=observation,
        subject=subject,
        modality=observation.modality,
        feature_space=observation.feature_space,
        gallery_fingerprint=spec.gallery_fingerprint,
        rank=spec.rank,
        similarity_score=spec.similarity_score,
        reference_count=spec.reference_count,
        calibration_revision=spec.calibration_revision,
    )
    candidate.full_clean()
    return candidate


def _verify_consumed_artifacts(
    stage: ProcessingStage,
    specs: tuple[ConsumedArtifactSpec, ...],
) -> None:
    """Fence persistence on the exact local derivatives analyzed by a stage."""
    if not specs:
        return
    artifact_ids = [str(spec.artifact_id) for spec in specs]
    if len(artifact_ids) != len(set(artifact_ids)):
        raise StageProcessingError("derived_artifact_integrity")
    artifacts = {
        str(artifact.pk): artifact
        for artifact in DerivedArtifact.objects.filter(
            pk__in=artifact_ids,
            catalog_item_id=stage.run.catalog_item_id,
            stage__run_id=stage.run_id,
        )
    }
    if len(artifacts) != len(specs):
        raise StageProcessingError(
            "derived_artifact_unavailable",
            "A required derived artifact is unavailable.",
        )
    for spec in specs:
        artifact = artifacts[str(spec.artifact_id)]
        if artifact.sha256 != spec.sha256 or artifact.byte_size != spec.byte_size:
            raise StageProcessingError(
                "derived_artifact_integrity",
                "A required derived artifact no longer matches its recorded "
                "fingerprint.",
            )
        try:
            with artifact.file.open("rb") as stream:
                fingerprint = fingerprint_stream(stream, preserve_position=False)
        except (FileNotFoundError, OSError, ValueError) as error:
            raise StageProcessingError(
                "derived_artifact_unavailable",
                "A required derived artifact is unavailable.",
            ) from error
        if (
            fingerprint.sha256 != spec.sha256
            or fingerprint.size_bytes != spec.byte_size
        ):
            raise StageProcessingError(
                "derived_artifact_integrity",
                "A required derived artifact no longer matches its recorded "
                "fingerprint.",
            )


def complete_stage(
    stage_id: UUID | str,
    outcome: StageOutcome | None = None,
    *,
    expected_attempt: int,
    observed_source_fingerprint: str | None = None,
) -> bool:
    """Append outputs and terminally complete exactly one claimed stage."""
    if expected_attempt <= 0:
        raise ValueError("expected_attempt must be positive")
    outcome = outcome or StageOutcome()
    with transaction.atomic():
        try:
            stage = ProcessingStage.objects.select_related("run__catalog_item").get(
                pk=stage_id
            )
        except ProcessingStage.DoesNotExist:
            return False
        if (
            stage.status != ProcessingStage.Status.RUNNING
            or stage.attempts != expected_attempt
        ):
            return False
        if (
            observed_source_fingerprint is not None
            and observed_source_fingerprint != stage.run.source_fingerprint
        ):
            # Supersession, replacement rows, and their stages commit together;
            # only the UUID-only dispatch is deferred until the outer commit.
            supersede_and_replace_local_run(stage.run_id)
            return False

        # Unit stages consume one bounded keyframe or audio chunk. Rehash it
        # after the running-attempt fence and immediately before appending the
        # outcome so a mutation between execution and persistence fails closed.
        _verify_consumed_artifacts(stage, outcome.consumed_artifacts)
        ProcessingResult.objects.bulk_create(
            [_result_from_spec(stage, spec) for spec in outcome.results]
        )
        DerivedArtifact.objects.bulk_create(
            [_artifact_from_spec(stage, spec) for spec in outcome.artifacts]
        )
        materialize_unit_stages(stage)
        publications = ArtifactPublication.objects.filter(
            stage=stage,
            file_name__in=[
                str(spec.file_name).replace("\\", "/") for spec in outcome.artifacts
            ],
        )
        publication_attempts = list(
            publications.values_list("attempt", flat=True).distinct()
        )
        publications.delete()
        TimestampClaim.objects.bulk_create(
            [_timestamp_from_spec(stage, spec) for spec in outcome.timestamp_claims]
        )
        if outcome.identity_observations:
            from pandect.identities.models import IdentityObservation

            IdentityObservation.objects.bulk_create(
                [
                    _identity_observation_from_spec(stage, spec)
                    for spec in outcome.identity_observations
                ]
            )
        if outcome.identity_candidates:
            from pandect.identities.models import IdentityCandidate

            IdentityCandidate.objects.bulk_create(
                [
                    _identity_candidate_from_spec(stage, spec)
                    for spec in outcome.identity_candidates
                ]
            )
        now = timezone.now()
        changed = ProcessingStage.objects.filter(
            pk=stage.pk,
            status=ProcessingStage.Status.RUNNING,
            attempts=expected_attempt,
        ).update(
            status=ProcessingStage.Status.SUCCEEDED,
            failure_code="",
            failure_detail="",
            heartbeat_at=now,
            lease_expires_at=None,
            finished_at=now,
            updated_at=now,
        )
        if not changed:
            raise StageStateError("Stage was no longer running.")
        for publication_attempt in publication_attempts:
            transaction.on_commit(
                lambda attempt=publication_attempt: clean_attempt_workspace(
                    stage.run_id,
                    stage.pk,
                    attempt,
                )
            )
        transaction.on_commit(lambda: _enqueue_ready_stages_safely(str(stage.run_id)))
    recompute_run_status(stage.run_id)
    return True


def skip_stage(
    stage_id: UUID | str,
    code: str,
    *,
    expected_attempt: int | None = None,
) -> bool:
    """Terminally skip pending work or one exactly fenced running attempt.

    Dependency propagation skips stages before they are claimed. A processor
    can also discover a supported item whose pixels this local runtime cannot
    decode; that transition must be fenced by the claimed attempt just like a
    completion or failure so a stale worker cannot terminate newer work.
    """
    if expected_attempt is not None and expected_attempt <= 0:
        raise ValueError("expected_attempt must be positive")
    now = timezone.now()
    with transaction.atomic():
        stages = ProcessingStage.objects.filter(pk=stage_id)
        if expected_attempt is None:
            stages = stages.filter(status=ProcessingStage.Status.PENDING)
        else:
            stages = stages.filter(
                status=ProcessingStage.Status.RUNNING,
                attempts=expected_attempt,
            )
        changed = stages.update(
            status=ProcessingStage.Status.SKIPPED,
            failure_code=_safe_failure_code(code),
            failure_detail="",
            heartbeat_at=now if expected_attempt is not None else None,
            lease_expires_at=None,
            finished_at=now,
            updated_at=now,
        )
        if not changed:
            return False
        run_id = ProcessingStage.objects.values_list("run_id", flat=True).get(
            pk=stage_id
        )
        if expected_attempt is not None:
            transaction.on_commit(lambda: _enqueue_ready_stages_safely(str(run_id)))
    recompute_run_status(run_id)
    return True


def fail_stage(
    stage_id: UUID | str,
    code: str,
    public_message: str = "",
    *,
    expected_attempt: int,
    retryable: bool = False,
    lease_expired_before=None,
) -> bool:
    """Persist a safe failure, retrying only a known transient local failure."""
    if expected_attempt <= 0:
        raise ValueError("expected_attempt must be positive")
    with transaction.atomic():
        try:
            stage = ProcessingStage.objects.get(pk=stage_id)
        except ProcessingStage.DoesNotExist:
            return False
        if (
            stage.status != ProcessingStage.Status.RUNNING
            or stage.attempts != expected_attempt
        ):
            return False
        now = timezone.now()
        should_retry = retryable and stage.attempts < stage.max_attempts
        new_status = (
            ProcessingStage.Status.PENDING
            if should_retry
            else ProcessingStage.Status.FAILED
        )
        active_attempt = ProcessingStage.objects.filter(
            pk=stage.pk,
            status=ProcessingStage.Status.RUNNING,
            attempts=expected_attempt,
        )
        if lease_expired_before is not None:
            active_attempt = active_attempt.filter(
                lease_expires_at__lt=lease_expired_before
            )
        changed = active_attempt.update(
            status=new_status,
            failure_code=_safe_failure_code(code),
            failure_detail=_safe_failure_detail(public_message),
            lease_expires_at=None,
            finished_at=None if should_retry else now,
            updated_at=now,
        )
        if not changed:
            return False
        if should_retry:
            # Keep the pending row invisible to another dispatcher until this
            # attempt's journaled hard links and private workspace are either
            # safely removed or the retry is terminally refused. The row write
            # above owns the attempt fence for this transaction.
            _discard_abandoned_publications(stage)
            publications_remain = ArtifactPublication.objects.filter(
                stage=stage.pk,
                attempt=stage.attempts,
            ).exists()
            if publications_remain:
                ProcessingStage.objects.filter(
                    pk=stage.pk,
                    status=ProcessingStage.Status.PENDING,
                    attempts=stage.attempts,
                ).update(
                    status=ProcessingStage.Status.FAILED,
                    failure_code="artifact_publication_recovery_failed",
                    failure_detail="",
                    finished_at=now,
                    updated_at=now,
                )
            else:
                clean_attempt_workspace(stage.run_id, stage.pk, stage.attempts)
        transaction.on_commit(lambda: _enqueue_ready_stages_safely(str(stage.run_id)))
    recompute_run_status(stage.run_id)
    return True


@transaction.atomic
def recompute_run_status(run_id: UUID | str) -> str | None:
    """Derive and persist run state behind a run/stage transaction fence."""
    try:
        run = ProcessingRun.objects.select_for_update().get(pk=run_id)
    except ProcessingRun.DoesNotExist:
        return None
    if run.status == ProcessingRun.Status.SUPERSEDED:
        return run.status

    # ``select_for_update`` is a real row lock on databases that support it.
    # SQLite deliberately ignores that clause, so acquire its single-writer
    # reservation with a semantic no-op before taking the stage snapshot. A
    # competing transition then completes first or waits; it cannot change a
    # stage after this snapshot and have stale derived state overwrite it.
    fenced = ProcessingRun.objects.filter(
        pk=run.pk,
        status=run.status,
        updated_at=run.updated_at,
    ).update(updated_at=F("updated_at"))
    if not fenced:
        return (
            ProcessingRun.objects.filter(pk=run.pk)
            .values_list("status", flat=True)
            .first()
        )

    statuses = list(
        ProcessingStage.objects.select_for_update()
        .filter(run_id=run.pk)
        .values_list("status", flat=True)
    )
    if not statuses:
        new_status = ProcessingRun.Status.FAILED
    elif any(status == ProcessingStage.Status.SUPERSEDED for status in statuses):
        new_status = ProcessingRun.Status.SUPERSEDED
    elif any(
        status
        in (
            ProcessingStage.Status.PENDING,
            ProcessingStage.Status.QUEUED,
            ProcessingStage.Status.RUNNING,
        )
        for status in statuses
    ):
        new_status = (
            ProcessingRun.Status.RUNNING
            if any(
                status
                in (ProcessingStage.Status.QUEUED, ProcessingStage.Status.RUNNING)
                for status in statuses
            )
            else ProcessingRun.Status.PENDING
        )
    elif any(status == ProcessingStage.Status.FAILED for status in statuses):
        new_status = (
            ProcessingRun.Status.PARTIAL
            if any(status == ProcessingStage.Status.SUCCEEDED for status in statuses)
            else ProcessingRun.Status.FAILED
        )
    else:
        new_status = ProcessingRun.Status.SUCCEEDED

    now = timezone.now()
    updates: dict[str, Any] = {"status": new_status, "updated_at": now}
    if new_status == ProcessingRun.Status.RUNNING and run.started_at is None:
        updates["started_at"] = now
    terminal_statuses = (
        ProcessingRun.Status.SUCCEEDED,
        ProcessingRun.Status.PARTIAL,
        ProcessingRun.Status.FAILED,
        ProcessingRun.Status.SUPERSEDED,
    )
    if new_status in terminal_statuses:
        if run.status not in terminal_statuses or run.finished_at is None:
            updates["finished_at"] = now
    else:
        updates["finished_at"] = None
    changed = ProcessingRun.objects.filter(
        pk=run.pk,
        status=run.status,
        updated_at=run.updated_at,
    ).update(**updates)
    if changed:
        return new_status
    return (
        ProcessingRun.objects.filter(pk=run.pk).values_list("status", flat=True).first()
    )


def _repair_missing_source_change_replacements(*, limit: int) -> tuple[int, int]:
    """Repair bounded legacy gaps left after local source supersession.

    New replacement transitions are atomic. This scan exists for rows written
    by older code or a process that stopped before that invariant was added.
    Remote runs are deliberately excluded because consent cannot be inherited.
    """
    if limit <= 0:
        return 0, 0
    run_ids = list(
        ProcessingRun.objects.filter(
            scope=ProcessingRun.Scope.LOCAL,
            status=ProcessingRun.Status.SUPERSEDED,
            stages__failure_code=SOURCE_CHANGED_FAILURE_CODE,
        )
        .order_by("-updated_at", "pk")
        .values_list("pk", flat=True)
        .distinct()[:limit]
    )
    repaired = 0
    for run_id in run_ids:
        if ProcessingRun.objects.filter(
            configuration__replacement_for_run=str(run_id)
        ).exists():
            _record_source_replacement(run_id)
            continue
        try:
            run = ProcessingRun.objects.select_related("catalog_item").get(pk=run_id)
            current = source_fingerprint(run.catalog_item)
        except (ProcessingRun.DoesNotExist, ProcessingError):
            continue
        if (
            _current_full_local_run(
                run.catalog_item,
                current,
                exclude_run_id=run.pk,
            )
            is not None
        ):
            _record_source_replacement(run.pk)
            continue
        replacement = supersede_and_replace_local_run(run.pk)
        configuration = (
            replacement.configuration
            if replacement is not None and isinstance(replacement.configuration, dict)
            else {}
        )
        if configuration.get("replacement_for_run") == str(run.pk):
            repaired += 1
    return repaired, len(run_ids)


def reconcile_stages(
    *,
    queued_timeout_seconds: int = DEFAULT_QUEUED_TIMEOUT_SECONDS,
    limit: int = DEFAULT_RECONCILE_LIMIT,
    now=None,
) -> ReconcileReport:
    """Recover queue/crash gaps in a bounded, idempotent maintenance pass."""
    if queued_timeout_seconds <= 0:
        raise ValueError("queued_timeout_seconds must be positive")
    if limit <= 0:
        raise ValueError("limit must be positive")
    now = now or timezone.now()
    queued_cutoff = now - timedelta(seconds=queued_timeout_seconds)

    queued_ids = list(
        ProcessingStage.objects.filter(
            status=ProcessingStage.Status.QUEUED,
            queued_at__lt=queued_cutoff,
        )
        .order_by("queued_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    queued_recovered = 0
    for stage_id in queued_ids:
        changed = ProcessingStage.objects.filter(
            pk=stage_id,
            status=ProcessingStage.Status.QUEUED,
            queued_at__lt=queued_cutoff,
        ).update(
            status=ProcessingStage.Status.PENDING,
            task_id="",
            failure_code="queue_delivery_lost",
            failure_detail="",
            updated_at=now,
        )
        if changed and enqueue_stage(stage_id):
            queued_recovered += 1

    remaining = max(0, limit - len(queued_ids))
    running_ids = list(
        ProcessingStage.objects.filter(
            status=ProcessingStage.Status.RUNNING,
            lease_expires_at__lt=now,
        )
        .order_by("lease_expires_at", "pk")
        .values_list("pk", flat=True)[:remaining]
    )
    running_recovered = 0
    running_failed = 0
    for stage_id in running_ids:
        try:
            stage = ProcessingStage.objects.get(
                pk=stage_id,
                status=ProcessingStage.Status.RUNNING,
            )
        except ProcessingStage.DoesNotExist:
            continue
        if stage.attempts < stage.max_attempts:
            recovered = False
            recovery_failed = False
            with transaction.atomic():
                changed = ProcessingStage.objects.filter(
                    pk=stage.pk,
                    status=ProcessingStage.Status.RUNNING,
                    attempts=stage.attempts,
                    lease_expires_at__lt=now,
                ).update(
                    status=ProcessingStage.Status.PENDING,
                    task_id="",
                    failure_code="worker_lease_expired",
                    failure_detail="",
                    lease_expires_at=None,
                    finished_at=None,
                    updated_at=now,
                )
                if changed:
                    # Do not commit/expose PENDING until the expired executor's
                    # exact journal is resolved. Publication and claim writes
                    # serialize behind this attempt fence.
                    _discard_abandoned_publications(stage)
                    publications_remain = ArtifactPublication.objects.filter(
                        stage=stage.pk,
                        attempt=stage.attempts,
                    ).exists()
                    if publications_remain:
                        ProcessingStage.objects.filter(
                            pk=stage.pk,
                            status=ProcessingStage.Status.PENDING,
                            attempts=stage.attempts,
                        ).update(
                            status=ProcessingStage.Status.FAILED,
                            failure_code="artifact_publication_recovery_failed",
                            failure_detail="",
                            finished_at=now,
                            updated_at=now,
                        )
                        recovery_failed = True
                    else:
                        clean_attempt_workspace(stage.run_id, stage.pk, stage.attempts)
                        recovered = True
            if recovery_failed:
                recompute_run_status(stage.run_id)
                running_failed += 1
                continue
            if recovered and enqueue_stage(stage.pk):
                running_recovered += 1
        else:
            failed = fail_stage(
                stage.pk,
                "worker_lease_expired",
                expected_attempt=stage.attempts,
                retryable=False,
                lease_expired_before=now,
            )
            if failed:
                _discard_abandoned_publications(stage)
                publications_remain = ArtifactPublication.objects.filter(
                    stage=stage.pk,
                    attempt=stage.attempts,
                ).exists()
                if publications_remain:
                    ProcessingStage.objects.filter(pk=stage.pk).update(
                        failure_code="artifact_publication_recovery_failed",
                        updated_at=now,
                    )
                else:
                    clean_attempt_workspace(stage.run_id, stage.pk, stage.attempts)
                running_failed += 1

    remaining = max(0, remaining - len(running_ids))
    replacements_repaired, replacements_examined = (
        _repair_missing_source_change_replacements(limit=remaining)
    )
    remaining = max(0, remaining - replacements_examined)
    pending_run_ids = list(
        ProcessingStage.objects.filter(status=ProcessingStage.Status.PENDING)
        .filter(
            run__status__in=(
                ProcessingRun.Status.PENDING,
                ProcessingRun.Status.RUNNING,
            )
        )
        .order_by("created_at", "pk")
        .values_list("run_id", flat=True)
        .distinct()[: max(0, remaining)]
    )
    for run_id in pending_run_ids:
        enqueue_ready_stages(run_id)

    return ReconcileReport(
        queued_recovered=queued_recovered,
        running_recovered=running_recovered,
        running_failed=running_failed,
        replacements_repaired=replacements_repaired,
        pending_examined=len(pending_run_ids),
    )
