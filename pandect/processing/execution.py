"""Concrete local processors wired to explicit durable stage kinds."""

import math
import os
import re
import struct
import wave
from array import array
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from functools import cache
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.db import IntegrityError, transaction

from pandect.catalog.models import CatalogItem
from pandect.identities.models import IdentityObservation, SubjectReference
from pandect.identities.services import (
    VIDEO_FACE_AGGREGATE_ROLE,
    VIDEO_FACE_FRAME_ROLE,
    cosine_similarity,
    current_reference_vectors,
    encode_embedding,
    observation_is_gallery_eligible,
    observation_vector,
    rank_closed_gallery,
)
from pandect.processing.extractors.audio import (
    MAX_FFMPEG_TIMEOUT_SECONDS,
    analyze_acoustics,
    detect_silence,
)
from pandect.processing.extractors.audio_splits import (
    DEFAULT_HARD_CUT_OVERLAP_SECONDS,
    DEFAULT_MAX_CHUNK_SECONDS,
    DEFAULT_MIN_SILENCE_SECONDS,
    DEFAULT_SILENCE_WINDOW_SECONDS,
    MAX_PROVIDER_SPLIT_DEPTH,
    MAX_PROVIDER_SPLIT_ENCODINGS,
    plan_audio_splits,
    split_audio_chunk_for_payload_limit,
)
from pandect.processing.extractors.derived_media import (
    DEFAULT_JPEG_QUALITY,
    DEFAULT_SCENE_CHANGE_THRESHOLD,
    FAITHFUL_AUDIO_STREAM_COPY_CODECS,
    attached_picture_pixel_preflight,
    detect_scene_changes,
    embedded_artwork_streams_from_probe,
    extract_embedded_artwork,
    extract_faithful_audio_stream,
    extract_normalized_audio_chunk,
    extract_normalized_audio_stream,
    extract_subtitle_stream,
    extract_video_frame,
    faithful_audio_output_for_codec,
    representative_frame_selection,
    subtitle_streams_from_probe,
)
from pandect.processing.extractors.errors import (
    ExtractorError,
    ResourceLimitError,
    UnsupportedPixelFormatError,
)
from pandect.processing.extractors.exiftool import (
    EXIFTOOL_TIMEOUT_SECONDS,
    extract_metadata,
)
from pandect.processing.extractors.faces import FaceModelPaths, analyze_faces
from pandect.processing.extractors.ffprobe import FFPROBE_TIMEOUT_SECONDS, probe_media
from pandect.processing.extractors.hashing import fingerprint_file
from pandect.processing.extractors.images import analyze_image, render_image_frame
from pandect.processing.extractors.subprocesses import (
    DEFAULT_OUTPUT_LIMIT_BYTES,
    run_command,
)
from pandect.processing.extractors.tesseract import (
    TESSERACT_TIMEOUT_SECONDS,
    extract_ocr,
)
from pandect.processing.extractors.text import (
    build_writing_identity_vector,
    extract_message_text,
)
from pandect.processing.extractors.timestamps import (
    timestamp_claims_from_ffprobe,
    timestamp_claims_from_tags,
)
from pandect.processing.extractors.voice import VoiceModelPaths, analyze_voice
from pandect.processing.models import (
    ArtifactPublication,
    DerivedArtifact,
    ProcessingResult,
    ProcessingRun,
    ProcessingStage,
    TimestampClaim,
)
from pandect.processing.remote.cloudflare import MAX_TRANSCRIPTION_AUDIO_BYTES
from pandect.processing.services import (
    IDENTITY_REMATCH_OBSERVATION_LIMIT,
    ArtifactSpec,
    ConsumedArtifactSpec,
    IdentityCandidateSpec,
    IdentityObservationSpec,
    ResultSpec,
    StageOutcome,
    StageProcessingError,
    TimestampClaimSpec,
    heartbeat_stage,
)

PROCESSOR_VERSION = "1"

_BINARY_VERSION_ARGUMENTS = {
    "ffmpeg": ("ffmpeg", "-version"),
    "ffprobe": ("ffprobe", "-version"),
    "exiftool": ("exiftool", "-ver"),
    "tesseract": ("tesseract", "--version"),
}
_LIBRARY_DISTRIBUTIONS = {
    "opencv": "opencv-python-headless",
    "openvino": "openvino",
    "pillow": "Pillow",
    "sherpa_onnx": "sherpa-onnx",
    "sherpa_onnx_core": "sherpa-onnx-core",
}


def _safe_version_line(value: bytes) -> str:
    decoded = value.decode("utf-8", errors="replace")
    first_line = next(
        (line.strip() for line in decoded.splitlines() if line.strip()), ""
    )
    visible = "".join(character for character in first_line if character.isprintable())
    return visible[:160] or "unknown"


@cache
def _binary_version_snapshot(name: str) -> dict[str, Any]:
    """Read one fixed binary's revision without leaking command output or paths."""
    try:
        arguments = _BINARY_VERSION_ARGUMENTS[name]
        value = run_command(
            arguments,
            timeout_seconds=3,
            output_limit_bytes=64 * 1024,
        )
    except Exception:  # A missing version is provenance, not stage failure.
        return {"available": False, "version": "unavailable"}
    output = value.stdout or value.stderr
    return {"available": True, "version": _safe_version_line(output)}


@cache
def _library_version_snapshot(name: str) -> dict[str, Any]:
    """Return installed distribution metadata without importing an ML runtime."""
    try:
        distribution = _LIBRARY_DISTRIBUTIONS[name]
        version = importlib_metadata.version(distribution)
    except (KeyError, importlib_metadata.PackageNotFoundError):
        return {"available": False, "version": "unavailable"}
    except Exception:  # Broken metadata must not invalidate extracted evidence.
        return {"available": False, "version": "unavailable"}
    return {"available": True, "version": str(version)[:160]}


def _runtime_component_names(stage: ProcessingStage, processor: str) -> set[str]:
    value = processor.casefold()
    components = {
        name
        for name in (*_BINARY_VERSION_ARGUMENTS, *_LIBRARY_DISTRIBUTIONS)
        if name.replace("_", "-") in value or name in value
    }
    if "tesseract" in value:
        components.add("pillow")
    if "face" in value:
        components.update(("pillow", "opencv", "openvino"))
    if "voice" in value:
        components.update(("ffmpeg", "sherpa_onnx"))

    kind = getattr(stage, "kind", "")
    by_stage = {
        ProcessingStage.Kind.IMAGE_METADATA: {"pillow", "exiftool"},
        ProcessingStage.Kind.IMAGE_OCR: {"pillow", "tesseract"},
        ProcessingStage.Kind.IMAGE_QR: {"pillow", "opencv"},
        ProcessingStage.Kind.FACE_ANALYSIS: {"pillow", "opencv", "openvino"},
        ProcessingStage.Kind.VIDEO_METADATA: {"ffprobe"},
        ProcessingStage.Kind.VIDEO_SUBTITLES: {"ffprobe", "ffmpeg"},
        ProcessingStage.Kind.VIDEO_KEYFRAMES: {"ffprobe", "ffmpeg"},
        ProcessingStage.Kind.VIDEO_AUDIO: {"ffprobe", "ffmpeg"},
        ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS: {
            "pillow",
            "tesseract",
            "opencv",
            "openvino",
        },
        ProcessingStage.Kind.AUDIO_METADATA: {"ffprobe"},
        ProcessingStage.Kind.AUDIO_SPLIT: {"ffprobe", "ffmpeg"},
        ProcessingStage.Kind.AUDIO_ACOUSTICS: {"ffprobe", "ffmpeg"},
        ProcessingStage.Kind.VOICE_ANALYSIS: {"ffmpeg", "sherpa_onnx"},
    }
    components.update(by_stage.get(kind, set()))
    if "sherpa_onnx" in components:
        components.add("sherpa_onnx_core")
    return components


def _runtime_versions(components: set[str]) -> dict[str, Any]:
    binaries = {
        name: dict(_binary_version_snapshot(name))
        for name in sorted(components & _BINARY_VERSION_ARGUMENTS.keys())
    }
    libraries = {
        name: dict(_library_version_snapshot(name))
        for name in sorted(components & _LIBRARY_DISTRIBUTIONS.keys())
    }
    return {"binaries": binaries, "libraries": libraries}


def _effective_parameters(
    stage: ProcessingStage,
    components: set[str],
) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    if components & _BINARY_VERSION_ARGUMENTS.keys():
        parameters["subprocess"] = {
            "shell": False,
            "default_output_limit_bytes": DEFAULT_OUTPUT_LIMIT_BYTES,
            "paths_and_content_omitted_from_provenance": True,
        }
    if "ffprobe" in components:
        parameters["ffprobe"] = {
            "timeout_seconds": FFPROBE_TIMEOUT_SECONDS,
            "arguments": [
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-show_chapters",
                "-print_format",
                "json",
            ],
        }
    if "exiftool" in components:
        parameters["exiftool"] = {
            "timeout_seconds": EXIFTOOL_TIMEOUT_SECONDS,
            "arguments": [
                "-json",
                "-G0:1",
                "-a",
                "-u",
                "-struct",
                "-api",
                "RequestAll=3",
            ],
        }
    if "tesseract" in components:
        parameters["tesseract"] = {
            "timeout_seconds_per_frame_or_page": TESSERACT_TIMEOUT_SECONDS,
            "language": "eng",
            "page_segmentation_mode": 3,
            "orientation_language": "osd",
            "orientation_page_segmentation_mode": 0,
            "output": "tsv",
        }
    if "ffmpeg" in components:
        parameters["ffmpeg"] = {
            "timeout_formula": "2 * media_duration_seconds + 300",
            "maximum_timeout_seconds": MAX_FFMPEG_TIMEOUT_SECONDS,
            "common_arguments": [
                "-nostdin",
                "-hide_banner",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-filter_complex_threads",
                "1",
            ],
            "decoder_threads": 1,
            "encoder_threads": 1,
            "filter_threads": 1,
            "filter_complex_threads": 1,
            "derived_media_arguments": [
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-n",
            ],
        }
    if "pillow" in components:
        parameters["pillow"] = {
            "maximum_decoded_pixels": int(settings.PROCESSING_MAX_IMAGE_PIXELS),
            "maximum_decoded_frames": int(settings.PROCESSING_MAX_IMAGE_FRAMES),
        }
    if "opencv" in components:
        parameters["opencv"] = {"native_threads": 1}
    if "openvino" in components:
        parameters["openvino"] = {
            "device": "CPU",
            "inference_threads": 1,
            "performance_hint": "LATENCY",
        }
    if "sherpa_onnx" in components:
        parameters["sherpa_onnx"] = {
            "provider": "cpu",
            "inference_threads": 1,
            "sample_rate_hz": 16_000,
            "vad_threshold": 0.5,
        }

    kind = getattr(stage, "kind", "")
    if kind == ProcessingStage.Kind.VIDEO_KEYFRAMES:
        parameters["representative_frames"] = {
            "maximum_frames": int(settings.PROCESSING_MAX_VIDEO_FRAMES),
            "scene_change_threshold": DEFAULT_SCENE_CHANGE_THRESHOLD,
            "scene_score_field": "lavfi.scene_score",
            "scene_candidate_order": ("score_descending_timestamp_ascending"),
            "jpeg_quality": DEFAULT_JPEG_QUALITY,
            "metadata_stripped": True,
            "artifact_stream_index": "absolute_ffprobe_stream_index",
            "artifact_frame_index": "representative_selection_ordinal",
            "decoded_source_coordinates": "not_available",
        }
    if kind in {
        ProcessingStage.Kind.VIDEO_AUDIO,
        ProcessingStage.Kind.AUDIO_SPLIT,
        ProcessingStage.Kind.VOICE_ANALYSIS,
    }:
        parameters["normalized_audio"] = {
            "sample_rate_hz": 16_000,
            "channels": 1,
            "codec": "flac",
            "metadata_stripped": True,
        }
    if kind == ProcessingStage.Kind.VIDEO_AUDIO:
        parameters["source_faithful_audio"] = {
            "stream_copy_codecs": list(FAITHFUL_AUDIO_STREAM_COPY_CODECS),
            "dynamic_stream_copy_codec_families": ["pcm_*"],
            "fallback_codec": "wavpack",
            "fallback_container": "wv",
            "preserve_source_sample_rate": True,
            "preserve_source_channel_count": True,
            "remote_transfer_eligible": False,
        }
    if kind == ProcessingStage.Kind.AUDIO_SPLIT:
        parameters["audio_split"] = {
            "maximum_chunk_seconds": DEFAULT_MAX_CHUNK_SECONDS,
            "preferred_silence_window_seconds": DEFAULT_SILENCE_WINDOW_SECONDS,
            "minimum_silence_seconds": DEFAULT_MIN_SILENCE_SECONDS,
            "hard_cut_overlap_seconds": DEFAULT_HARD_CUT_OVERLAP_SECONDS,
            "provider_payload_limit_bytes": MAX_TRANSCRIPTION_AUDIO_BYTES,
            "provider_split_max_depth": MAX_PROVIDER_SPLIT_DEPTH,
            "provider_split_max_encodings": MAX_PROVIDER_SPLIT_ENCODINGS,
        }
    return parameters


def _heartbeat(stage: ProcessingStage) -> None:
    if not heartbeat_stage(stage.pk, expected_attempt=stage.attempts):
        raise StageProcessingError("stage_lease_lost")


def _source_path(stage: ProcessingStage) -> Path:
    item = stage.run.catalog_item
    try:
        extension = getattr(item, item.item_type)
        value = extension.file
        return Path(value.path)
    except (AttributeError, ValueError) as error:
        raise StageProcessingError(
            "source_unavailable",
            "The canonical source is unavailable.",
        ) from error


def _provenance(stage: ProcessingStage, processor: str) -> dict[str, Any]:
    components = _runtime_component_names(stage, processor)
    return {
        "processor": processor,
        "processor_version": PROCESSOR_VERSION,
        "source_sha256": stage.run.source_fingerprint,
        "source_size": stage.run.source_size,
        "runtime_versions": _runtime_versions(components),
        "effective_parameters": _effective_parameters(stage, components),
    }


def _result(
    stage: ProcessingStage,
    kind: str,
    data: Any,
    *,
    processor: str,
    text: str = "",
    confidence: float | None = None,
) -> ResultSpec:
    return ResultSpec(
        result_kind=kind,
        evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
        data=data,
        text=text,
        confidence=confidence,
        processor_name=processor,
        processor_version=PROCESSOR_VERSION,
        input_hashes={"canonical": stage.run.source_fingerprint},
        provenance=_provenance(stage, processor),
    )


def _model_result(
    stage: ProcessingStage,
    kind: str,
    data: Any,
    *,
    processor: str,
    text: str = "",
    confidence: float | None = None,
) -> ResultSpec:
    return ResultSpec(
        result_kind=kind,
        evidence_tier=ProcessingResult.EvidenceTier.MODEL_GENERATED,
        data=data,
        text=text,
        confidence=confidence,
        processor_name=processor,
        processor_version=PROCESSOR_VERSION,
        input_hashes={"canonical": stage.run.source_fingerprint},
        provenance=_provenance(stage, processor),
    )


def _artifact_bound_result(
    spec: ResultSpec,
    stage: ProcessingStage,
    artifact: DerivedArtifact,
) -> ResultSpec:
    """Bind one unit result to the exact immutable derivative it analyzed."""
    provenance = dict(spec.provenance)
    provenance["unit"] = {
        "stage_unit_key": getattr(stage, "unit_key", "default"),
        "artifact_id": str(artifact.pk),
        "artifact_sha256": artifact.sha256,
    }
    return replace(
        spec,
        input_hashes={
            "canonical": stage.run.source_fingerprint,
            "artifact": artifact.sha256,
        },
        provenance=provenance,
    )


def _verified_derived_artifact_path(artifact: DerivedArtifact) -> Path:
    """Return an artifact path only while its durable fingerprint still matches."""
    try:
        path = Path(artifact.file.path)
        fingerprint = fingerprint_file(path)
    except (AttributeError, FileNotFoundError, OSError, ValueError) as error:
        raise StageProcessingError(
            "derived_artifact_unavailable",
            "A required derived artifact is unavailable.",
        ) from error
    if (
        fingerprint.sha256 != artifact.sha256
        or fingerprint.size_bytes != artifact.byte_size
    ):
        raise StageProcessingError(
            "derived_artifact_integrity",
            "A required derived artifact no longer matches its recorded fingerprint.",
        )
    return path


def _attempt_work_directory(stage: ProcessingStage) -> Path:
    """Return the private workspace owned by this exact stage attempt."""
    work_root = Path(settings.WORK_ROOT).resolve()
    candidate = (
        work_root / str(stage.run_id) / str(stage.pk) / f"attempt-{stage.attempts}"
    )
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as error:
        raise StageProcessingError("work_path_invalid") from error
    if (
        candidate == work_root
        or work_root not in candidate.parents
        or resolved != candidate
        or candidate.is_symlink()
    ):
        raise StageProcessingError("work_path_invalid")
    candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Recheck after creation to reject a component exchanged for a symlink
    # between validation and mkdir.
    try:
        redirected = (
            candidate.is_symlink() or candidate.resolve(strict=True) != candidate
        )
    except OSError as error:
        raise StageProcessingError("work_path_invalid") from error
    if redirected:
        raise StageProcessingError("work_path_invalid")
    return candidate


def _scratch_path(stage: ProcessingStage, suffix: str) -> Path:
    """Allocate an opaque scratch filename inside this attempt's workspace."""
    return _attempt_work_directory(stage) / f"{uuid4().hex}{suffix}"


def _storage_paths(
    stage: ProcessingStage,
    basename: str,
) -> tuple[Path, Path, str]:
    """Return private work, final absolute, and storage-relative paths."""
    safe_name = "".join(
        character if character.isalnum() or character in ".-_" else "_"
        for character in basename
    )[:180]
    if not safe_name:
        safe_name = "artifact"
    relative = (
        Path("derived")
        / str(stage.run.catalog_item_id)
        / str(stage.run_id)
        / f"{stage.pk}-{safe_name}"
    )
    final_path = (Path(settings.MEDIA_ROOT) / relative).resolve()
    media_root = Path(settings.MEDIA_ROOT).resolve()
    if final_path != media_root and media_root not in final_path.parents:
        raise StageProcessingError("artifact_path_invalid")
    final_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # FFmpeg chooses some output muxers from the destination suffix. Keep the
    # derivative's extension in private work while retaining a random basename
    # that cannot disclose the source filename.
    suffix = Path(safe_name).suffix or ".bin"
    return _scratch_path(stage, suffix), final_path, relative.as_posix()


def _publish_artifact(
    temporary: Path,
    final: Path,
    *,
    stage: ProcessingStage,
    expected_sha256: str,
    expected_size_bytes: int,
) -> bool:
    """Publish once, or recover an identical file from an interrupted attempt.

    A worker can stop after the atomic rename but before ``complete_stage``
    commits the matching ``DerivedArtifact`` row.  The final path is stable for
    the stage and artifact unit, so a retry may adopt that file only after its
    complete byte fingerprint matches the newly reproduced derivative.  A
    different or unsafe destination is retained for investigation and never
    overwritten.
    """
    stage_id = stage.pk
    attempt = stage.attempts
    media_root = Path(settings.MEDIA_ROOT).resolve()
    try:
        final_relative = final.relative_to(media_root).as_posix()
        temporary_relative = temporary.relative_to(
            Path(settings.WORK_ROOT).resolve()
        ).as_posix()
    except ValueError as error:
        raise StageProcessingError("artifact_path_invalid") from error

    if final.exists() or final.is_symlink():
        if final.is_symlink() or not final.is_file():
            raise StageProcessingError("artifact_destination_unsafe")
        try:
            fingerprint = fingerprint_file(final)
        except OSError as error:
            raise StageProcessingError("artifact_destination_unreadable") from error
        if (
            fingerprint.sha256 == expected_sha256
            and fingerprint.size_bytes == expected_size_bytes
        ):
            with transaction.atomic():
                if not heartbeat_stage(stage_id, expected_attempt=attempt):
                    raise StageProcessingError("stage_lease_lost")
                ArtifactPublication.objects.filter(
                    stage_id=stage_id,
                    attempt=attempt,
                    file_name=final_relative,
                ).delete()
            return False
        raise StageProcessingError("artifact_destination_mismatch")

    try:
        temporary_stat = temporary.stat(follow_symlinks=False)
    except OSError as error:
        raise StageProcessingError("artifact_temporary_unreadable") from error
    if not temporary.is_file() or temporary.is_symlink():
        raise StageProcessingError("artifact_temporary_unsafe")
    temporary_fingerprint = fingerprint_file(temporary)
    if (
        temporary_fingerprint.sha256 != expected_sha256
        or temporary_fingerprint.size_bytes != expected_size_bytes
    ):
        raise StageProcessingError("artifact_temporary_mismatch")

    # Flush file contents before the durable journal says these exact bytes may
    # be linked into evidence storage.
    try:
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
    except OSError as error:
        raise StageProcessingError("artifact_temporary_sync_failed") from error

    try:
        with transaction.atomic():
            publication = ArtifactPublication.objects.create(
                stage_id=stage_id,
                attempt=attempt,
                file_name=final_relative,
                temporary_name=temporary_relative,
                sha256=expected_sha256,
                byte_size=expected_size_bytes,
                source_device=temporary_stat.st_dev,
                source_inode=temporary_stat.st_ino,
            )
            claimed = ProcessingStage.objects.filter(
                pk=stage_id,
                status=ProcessingStage.Status.RUNNING,
                attempts=attempt,
            ).exists()
            if not claimed:
                raise StageProcessingError("stage_lease_lost")
    except IntegrityError as error:
        raise StageProcessingError("artifact_publication_conflict") from error

    # Fence reconciliation with a short SQLite write transaction while the
    # filesystem link is created. The intent above is already committed, so a
    # process death after linking still leaves durable ownership information.
    with transaction.atomic():
        if not heartbeat_stage(stage_id, expected_attempt=attempt):
            raise StageProcessingError("stage_lease_lost")
        try:
            os.link(temporary, final, follow_symlinks=False)
        except FileExistsError:
            if final.is_symlink() or not final.is_file():
                raise StageProcessingError("artifact_destination_unsafe") from None
            fingerprint = fingerprint_file(final)
            if (
                fingerprint.sha256 == expected_sha256
                and fingerprint.size_bytes == expected_size_bytes
            ):
                publication.delete()
                return False
            raise StageProcessingError("artifact_destination_mismatch") from None
        except OSError as error:
            if error.errno == 18:  # EXDEV: configured roots span filesystems.
                raise StageProcessingError(
                    "artifact_storage_filesystem_mismatch",
                    "Work and media storage must use the same filesystem.",
                ) from error
            raise StageProcessingError("artifact_publication_failed") from error

        try:
            fingerprint = fingerprint_file(final)
        except OSError as error:
            raise StageProcessingError("artifact_publication_unreadable") from error
        if (
            fingerprint.sha256 != expected_sha256
            or fingerprint.size_bytes != expected_size_bytes
        ):
            raise StageProcessingError("artifact_publication_mismatch")
        try:
            directory_fd = os.open(final.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            raise StageProcessingError("artifact_publication_sync_failed") from error
    return True


def _artifact_spec(
    relative_name: str,
    value,
    *,
    role: str,
    metadata: Mapping[str, Any] | None = None,
    publication_created: bool = False,
) -> ArtifactSpec:
    artifact_metadata = dict(value.metadata or {})
    artifact_metadata.update(metadata or {})
    return ArtifactSpec(
        file_name=relative_name,
        role=role,
        mime_type=value.mime_type,
        sha256=value.sha256,
        byte_size=value.size_bytes,
        stream_index=value.stream_index,
        frame_index=value.frame_index,
        source_start_seconds=(
            Decimal(str(value.source_start_seconds))
            if value.source_start_seconds is not None
            else None
        ),
        source_end_seconds=(
            Decimal(str(value.source_end_seconds))
            if value.source_end_seconds is not None
            else None
        ),
        metadata=artifact_metadata,
        publication_created=publication_created,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _timestamp_specs(values, *, processor: str) -> tuple[TimestampClaimSpec, ...]:
    specs = []
    for value in values:
        parsed = value.parsed_value
        if not isinstance(parsed, datetime) or parsed.tzinfo is None:
            parsed = None
        precision = value.precision
        if precision not in TimestampClaim.Precision.values:
            precision = TimestampClaim.Precision.UNKNOWN
        specs.append(
            TimestampClaimSpec(
                namespace=value.namespace,
                tag=value.tag,
                raw_value=str(_json_value(value.raw_value)),
                precision=precision,
                parsed_value=parsed,
                timezone_evidence=value.timezone_evidence,
                confidence=value.confidence,
                conflicts=list(value.conflicts),
                provenance={
                    "processor": processor,
                    "operational": value.operational,
                    "semantic": value.semantic,
                },
            )
        )
    return tuple(specs)


def _message_profile(stage: ProcessingStage) -> StageOutcome:
    body = stage.run.catalog_item.message.body
    profile = extract_message_text(body)
    return StageOutcome(
        results=(
            _result(
                stage,
                ProcessingResult.Kind.TEXT_EXTRACTION,
                profile,
                processor="pandect.text-profile",
            ),
        )
    )


def _writing_style(stage: ProcessingStage) -> StageOutcome:
    profile = extract_message_text(stage.run.catalog_item.message.body)
    feature = build_writing_identity_vector(profile)
    data = {
        "schema_version": profile["schema_version"],
        "counts": profile["counts"],
        "language_hints": profile["language_hints"],
        "readability": profile["readability"],
        "lexical_profile": profile["lexical_profile"],
        "writing_style": profile["writing_style"],
        "ngrams": profile["ngrams"],
    }
    observations: tuple[IdentityObservationSpec, ...] = ()
    if feature["has_signal"] and feature["word_count"]:
        provenance = dict(feature["provenance"])
        quality_status = (
            IdentityObservation.QualityStatus.APPROVED
            if feature["word_count"] >= 25
            else IdentityObservation.QualityStatus.UNASSESSED
        )
        observations = (
            IdentityObservationSpec(
                modality=IdentityObservation.Modality.WRITING,
                unit_key="body",
                source_location={
                    "start": 0,
                    "end": len(stage.run.catalog_item.message.body),
                },
                source_sha256=stage.run.source_fingerprint,
                embedding=encode_embedding(feature["vector"]),
                embedding_dimension=feature["dimension"],
                feature_space=feature["feature_space"],
                processor_name=str(provenance["processor_name"]),
                processor_version=str(provenance["processor_version"]),
                preprocessing_version=str(provenance["preprocessing_version"]),
                provenance=provenance,
                quality_status=quality_status,
                quality_score=(
                    1.0
                    if quality_status == IdentityObservation.QualityStatus.APPROVED
                    else None
                ),
                quality_details={
                    "word_count": feature["word_count"],
                    "minimum_words_for_auto_quality": 25,
                },
                sample_word_count=feature["word_count"],
            ),
        )
    return StageOutcome(
        results=(
            _result(
                stage,
                ProcessingResult.Kind.WRITING_STYLE,
                data,
                processor="pandect.writing-profile",
            ),
        ),
        identity_observations=observations,
    )


def _writing_identity(stage: ProcessingStage) -> StageOutcome:
    return _identity_candidates(stage, IdentityObservation.Modality.WRITING)


def _image_metadata(stage: ProcessingStage) -> StageOutcome:
    path = _source_path(stage)
    metadata = extract_metadata(path)
    technical = None
    pixel_warning = None
    try:
        technical = analyze_image(
            path,
            max_pixels=settings.PROCESSING_MAX_IMAGE_PIXELS,
            max_frames=settings.PROCESSING_MAX_IMAGE_FRAMES,
        ).as_dict()
    except Exception as error:  # Preserve metadata when pixels are unsupported.
        from pandect.processing.extractors.errors import ExtractorError

        if not isinstance(error, ExtractorError):
            raise
        pixel_warning = error.code
    payload = {
        "technical": technical,
        "metadata": metadata.as_dict(),
        "pixel_analysis_warning": pixel_warning,
    }
    return StageOutcome(
        results=(
            _result(
                stage,
                ProcessingResult.Kind.IMAGE_METADATA,
                payload,
                processor="pillow+exiftool",
            ),
        ),
        timestamp_claims=_timestamp_specs(
            timestamp_claims_from_tags(metadata.raw_tags),
            processor="exiftool",
        ),
    )


def _bounded_image_preflight(path: Path) -> dict[str, Any]:
    """Approve the exact frame/page indexes available to pixel processors."""
    return analyze_image(
        path,
        max_pixels=settings.PROCESSING_MAX_IMAGE_PIXELS,
        max_frames=settings.PROCESSING_MAX_IMAGE_FRAMES,
        max_total_decoded_pixels=settings.PROCESSING_MAX_IMAGE_PIXELS,
    ).as_dict()


def _bounded_image_units(stage: ProcessingStage, path: Path, technical: Mapping):
    """Yield metadata-free, single-unit scratch inputs after one preflight."""
    approved_units = list(technical.get("frames") or [])
    if len(approved_units) != int(technical["frames_analyzed"]):
        raise StageProcessingError("image_preflight_invalid")
    decoded_pixels = 0
    for approved_unit in approved_units:
        source_unit_index = int(approved_unit["index"])
        _heartbeat(stage)
        processor_input = _scratch_path(stage, ".png")
        rendered = render_image_frame(
            path,
            processor_input,
            frame_index=source_unit_index,
            max_pixels=settings.PROCESSING_MAX_IMAGE_PIXELS,
        )
        fingerprint = fingerprint_file(processor_input)
        rendered = {
            **rendered,
            "sha256": fingerprint.sha256,
            "byte_size": fingerprint.size_bytes,
        }
        geometry = {
            "width": int(rendered["width"]),
            "height": int(rendered["height"]),
            "pixel_count": int(rendered["pixel_count"]),
        }
        expected_geometry = {
            "width": int(approved_unit["width"]),
            "height": int(approved_unit["height"]),
            "pixel_count": int(approved_unit["width"]) * int(approved_unit["height"]),
        }
        if geometry != expected_geometry:
            raise StageProcessingError("image_source_unit_changed")
        decoded_pixels += geometry["pixel_count"]
        if decoded_pixels > int(settings.PROCESSING_MAX_IMAGE_PIXELS):
            raise ResourceLimitError
        yield (
            processor_input,
            {
                "source_unit_index": source_unit_index,
                "source_unit_geometry": geometry,
                "rendered_input": rendered,
            },
        )


def _bounded_image_source_scope(
    technical: Mapping,
    processed_unit_indexes: list[int],
    **extra: Any,
) -> dict[str, Any]:
    """Describe the bounded source coverage shared by image processors."""
    available_units = int(technical["frame_count"])
    scope = {
        "policy": "bounded_all_frames_or_pages",
        "available_units": available_units,
        "processed_units": len(processed_unit_indexes),
        "processed_unit_indexes": processed_unit_indexes,
        "analysis_truncated": len(processed_unit_indexes) < available_units,
        "truncation_reason": technical["truncation_reason"],
        "technical_preflight_truncated": bool(technical["analysis_truncated"]),
        "technical_preflight_truncation_reason": technical["truncation_reason"],
    }
    scope.update(extra)
    return scope


def _image_ocr(stage: ProcessingStage) -> StageOutcome:
    path = _source_path(stage)
    technical = _bounded_image_preflight(path)

    # Tesseract can independently traverse multi-page inputs. Render each
    # preflight-approved source unit to its own metadata-free single-page PNG,
    # so the 60-second timeout and pixel bound apply independently per unit.
    units = []
    texts = []
    words = []
    lines = []
    blocks = []
    confidences = []
    processed_unit_indexes = []
    for ocr_input, source_unit in _bounded_image_units(stage, path, technical):
        frame_index = int(source_unit["source_unit_index"])
        processed_unit_indexes.append(frame_index)
        value = extract_ocr(
            ocr_input,
            timeout_seconds=TESSERACT_TIMEOUT_SECONDS,
        )
        unit_payload = value.as_dict()
        units.append(
            {
                **source_unit,
                "ocr": unit_payload,
            }
        )
        if value.text:
            texts.append(value.text)
        for collection, key in ((words, "words"), (lines, "lines"), (blocks, "blocks")):
            collection.extend(
                {**entry, "source_unit_index": frame_index}
                for entry in unit_payload.get(key, [])
            )
        if value.mean_confidence is not None:
            confidences.append(value.mean_confidence)

    combined_text = "\n\n".join(texts)
    combined_confidence = (
        round(sum(confidences) / len(confidences), 6) if confidences else None
    )
    payload = {
        "text": combined_text,
        "words": words,
        "lines": lines,
        "blocks": blocks,
        "page_count": len(processed_unit_indexes),
        "mean_confidence": combined_confidence,
        "language": "eng",
        "units": units,
    }
    payload["source_scope"] = _bounded_image_source_scope(
        technical,
        processed_unit_indexes,
        timeout_seconds_per_unit=TESSERACT_TIMEOUT_SECONDS,
    )
    return StageOutcome(
        results=(
            _result(
                stage,
                ProcessingResult.Kind.OCR,
                payload,
                text=combined_text,
                confidence=(
                    combined_confidence / 100
                    if combined_confidence is not None
                    else None
                ),
                processor="pillow-bounded-unit-scope+tesseract-eng",
            ),
        )
    )


def _decode_qr_codes(path: Path) -> list[dict[str, Any]]:
    """Decode local QR payloads with geometry and no network lookup."""
    try:
        import cv2
    except ImportError as error:
        raise StageProcessingError("optional_dependency_unavailable") from error
    cv2.setNumThreads(1)
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        # _image_qr performs Pillow's bounded decode first, so this is a local
        # decoder capability gap rather than an unverified corrupt source.
        raise UnsupportedPixelFormatError
    detector = cv2.QRCodeDetector()
    codes = []
    try:
        detected, values, points, _ = detector.detectAndDecodeMulti(image)
    except cv2.error as error:
        raise StageProcessingError("qr_decode_failed") from error
    if detected and points is not None:
        for index, value in enumerate(values):
            geometry = points[index].tolist() if index < len(points) else []
            codes.append({"value": value, "polygon": geometry})
    return codes


def _image_qr(stage: ProcessingStage) -> StageOutcome:
    path = _source_path(stage)
    technical = _bounded_image_preflight(path)
    units = []
    codes = []
    processed_unit_indexes = []
    for qr_input, source_unit in _bounded_image_units(stage, path, technical):
        source_unit_index = int(source_unit["source_unit_index"])
        source_unit_geometry = dict(source_unit["source_unit_geometry"])
        processed_unit_indexes.append(source_unit_index)
        unit_codes = [
            {
                **code,
                "source_unit_index": source_unit_index,
                "source_unit_geometry": source_unit_geometry,
            }
            for code in _decode_qr_codes(qr_input)
        ]
        codes.extend(unit_codes)
        units.append({**source_unit, "codes": unit_codes})
    payload = {
        "codes": codes,
        "decoder": "opencv-qrcode",
        "units": units,
        "source_scope": _bounded_image_source_scope(
            technical,
            processed_unit_indexes,
        ),
    }
    return StageOutcome(
        results=(
            _result(
                stage,
                ProcessingResult.Kind.QR_CODES,
                payload,
                processor="pillow-bounded-unit-scope+opencv-qrcode",
            ),
        )
    )


def _face_analysis(stage: ProcessingStage) -> StageOutcome:
    path = _source_path(stage)
    technical = _bounded_image_preflight(path)
    model_paths = FaceModelPaths.from_root(Path(settings.MODELS_ROOT))
    units = []
    result_observations = []
    result_warnings = []
    observations = []
    processed_unit_indexes = []
    result_provenance: dict[str, Any] = {}
    multiple_source_units = int(technical["frame_count"]) > 1
    for face_input, source_unit in _bounded_image_units(stage, path, technical):
        source_unit_index = int(source_unit["source_unit_index"])
        source_unit_geometry = dict(source_unit["source_unit_geometry"])
        processed_unit_indexes.append(source_unit_index)
        analysis = analyze_faces(face_input, model_paths=model_paths)
        result_provenance = dict(analysis.provenance)
        unit_observations = []
        for value in analysis.observations:
            unit_key = (
                f"unit:{source_unit_index:03d}/{value.unit_key}"
                if multiple_source_units
                else value.unit_key
            )
            source_provenance = {
                "index": source_unit_index,
                "geometry": source_unit_geometry,
                "rendered_input_sha256": source_unit["rendered_input"]["sha256"],
                "rendered_input_byte_size": source_unit["rendered_input"]["byte_size"],
                "metadata_stripped": True,
            }
            provenance = {
                **dict(value.provenance),
                "source_unit": source_provenance,
            }
            display_value = {
                **value.as_dict(),
                "unit_key": unit_key,
                "source_unit_index": source_unit_index,
                "source_unit_geometry": source_unit_geometry,
                "provenance": provenance,
            }
            unit_observations.append(display_value)
            result_observations.append(display_value)

            sface = value.provenance["models"]["sface"]
            quality_status = (
                IdentityObservation.QualityStatus.APPROVED
                if value.quality_assessment == "usable"
                else IdentityObservation.QualityStatus.UNASSESSED
            )
            observations.append(
                IdentityObservationSpec(
                    modality=IdentityObservation.Modality.FACE,
                    unit_key=unit_key,
                    source_location={
                        "bbox": list(value.bbox),
                        "source_unit_index": source_unit_index,
                        "source_unit_geometry": source_unit_geometry,
                    },
                    source_sha256=stage.run.source_fingerprint,
                    embedding=encode_embedding(value.embedding),
                    embedding_dimension=len(value.embedding),
                    feature_space=value.feature_space,
                    processor_name="pandect.local-face-observation",
                    processor_version=PROCESSOR_VERSION,
                    preprocessing_version="yunet-sface-alignment-v1",
                    model_identifier=str(sface["identifier"]),
                    model_version="2021dec",
                    model_sha256=str(sface["sha256"]),
                    provenance=provenance,
                    quality_status=quality_status,
                    quality_score=value.quality_score,
                    quality_details={
                        **dict(value.quality_details),
                        "source_unit_index": source_unit_index,
                        "source_unit_geometry": source_unit_geometry,
                    },
                )
            )
        unit_warnings = [
            (
                f"unit:{source_unit_index:03d}/{warning}"
                if multiple_source_units
                else warning
            )
            for warning in analysis.warnings
        ]
        result_warnings.extend(unit_warnings)
        units.append(
            {
                **source_unit,
                "face_analysis": {
                    "schema_version": "1",
                    "observations": unit_observations,
                    "warnings": unit_warnings,
                    "provenance": dict(analysis.provenance),
                },
            }
        )
    payload = {
        "schema_version": "1",
        "observations": result_observations,
        "warnings": result_warnings,
        "provenance": result_provenance,
        "units": units,
        "source_scope": _bounded_image_source_scope(
            technical,
            processed_unit_indexes,
        ),
    }
    return StageOutcome(
        results=(
            _model_result(
                stage,
                ProcessingResult.Kind.FACE_OBSERVATIONS,
                payload,
                processor="pandect.local-face-observation",
            ),
        ),
        identity_observations=tuple(observations),
    )


def _image_visual_payload(technical: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize literal pixels and explicitly bounded quality heuristics."""
    width = int(technical.get("width") or 0)
    height = int(technical.get("height") or 0)
    if width > height:
        orientation = "landscape"
    elif height > width:
        orientation = "portrait"
    elif width and height:
        orientation = "square"
    else:
        orientation = "unknown"

    quality = technical.get("quality")
    quality = quality if isinstance(quality, Mapping) else {}
    mean_luminance = float(quality.get("mean") or 0)
    shadow_clip = float(quality.get("shadow_clip_proportion") or 0)
    highlight_clip = float(quality.get("highlight_clip_proportion") or 0)
    edge_detail = float(quality.get("edge_standard_deviation") or 0)

    if mean_luminance < 50:
        luminance_label = "predominantly_dark"
    elif mean_luminance > 205:
        luminance_label = "predominantly_bright"
    else:
        luminance_label = "mid_range"

    return {
        "schema_version": "1",
        "literal_observations": {
            "dimensions": {"width": width, "height": height},
            "aspect_ratio": technical.get("aspect_ratio"),
            "orientation": orientation,
            "color_mode": technical.get("mode"),
            "has_alpha": bool(technical.get("has_alpha")),
            "is_animated": bool(technical.get("is_animated")),
            "frame_count": int(technical.get("frame_count") or 0),
            "dominant_colors": list(technical.get("dominant_colors") or []),
        },
        "quality_heuristics": {
            "luminance": {
                "mean_0_to_255": mean_luminance,
                "label": luminance_label,
                "threshold_revision": "fixed-luminance-v1",
            },
            "clipping": {
                "shadow_proportion": shadow_clip,
                "highlight_proportion": highlight_clip,
                "substantial_shadow_clipping": shadow_clip >= 0.10,
                "substantial_highlight_clipping": highlight_clip >= 0.10,
                "threshold_revision": "outer-six-levels-10pct-v1",
            },
            "edge_detail": {
                "standard_deviation": edge_detail,
                "low_edge_detail": edge_detail < 12,
                "threshold_revision": "find-edges-stddev-12-v1",
                "interpretation": (
                    "Low edge detail can be consistent with blur, but can also be "
                    "caused by a naturally smooth scene."
                ),
            },
            "compression_artifacts": {
                "status": "not_assessed",
                "reason": (
                    "No reliable content-independent artifact classifier is used."
                ),
            },
        },
        "limitations": [
            "This is a literal pixel summary, not a semantic scene description.",
            "Quality labels are deterministic screening heuristics, not findings.",
            "Only the first decoded frame supplies palette and quality measurements.",
        ],
    }


def _image_visual(stage: ProcessingStage) -> StageOutcome:
    technical = analyze_image(
        _source_path(stage),
        max_pixels=settings.PROCESSING_MAX_IMAGE_PIXELS,
        max_frames=settings.PROCESSING_MAX_IMAGE_FRAMES,
    ).as_dict()
    return StageOutcome(
        results=(
            _result(
                stage,
                ProcessingResult.Kind.IMAGE_VISUAL,
                _image_visual_payload(technical),
                processor="pillow-literal-visual-v1",
            ),
        )
    )


def _unsupported_local_observation(stage: ProcessingStage) -> StageOutcome:
    reasons = {
        ProcessingStage.Kind.IMAGE_QR: "qr_decoder_not_available",
        ProcessingStage.Kind.FACE_ANALYSIS: "face_models_not_provisioned",
        ProcessingStage.Kind.FACE_IDENTITY: "no_eligible_face_observations",
        ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS: "frame_models_not_provisioned",
        ProcessingStage.Kind.AUDIO_ACOUSTICS: "acoustic_analyzer_not_available",
        ProcessingStage.Kind.VOICE_ANALYSIS: "voice_models_not_provisioned",
        ProcessingStage.Kind.VOICE_IDENTITY: "no_eligible_voice_observations",
    }
    result_kind = {
        ProcessingStage.Kind.IMAGE_QR: ProcessingResult.Kind.QR_CODES,
        ProcessingStage.Kind.FACE_ANALYSIS: ProcessingResult.Kind.FACE_OBSERVATIONS,
        ProcessingStage.Kind.FACE_IDENTITY: ProcessingResult.Kind.FACE_CANDIDATES,
        ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS: ProcessingResult.Kind.IMAGE_VISUAL,
        ProcessingStage.Kind.AUDIO_ACOUSTICS: ProcessingResult.Kind.ACOUSTICS,
        ProcessingStage.Kind.VOICE_ANALYSIS: ProcessingResult.Kind.VOICE_OBSERVATIONS,
        ProcessingStage.Kind.VOICE_IDENTITY: ProcessingResult.Kind.VOICE_CANDIDATES,
    }[stage.kind]
    return StageOutcome(
        results=(
            _result(
                stage,
                result_kind,
                {
                    "observations": [],
                    "candidates": [],
                    "unknown": True,
                    "reason": reasons[stage.kind],
                },
                processor="pandect.local-observation",
            ),
        )
    )


def _artwork_analysis_result(
    stage: ProcessingStage,
    kind: str,
    data: Any,
    *,
    processor: str,
    artifact_hashes: list[str],
    text: str = "",
) -> ResultSpec:
    """Build a result whose immediate inputs are extracted artwork files."""
    return ResultSpec(
        result_kind=kind,
        evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
        data=data,
        text=text,
        processor_name=processor,
        processor_version=PROCESSOR_VERSION,
        input_hashes={
            "canonical": stage.run.source_fingerprint,
            "embedded_artwork": artifact_hashes,
        },
        provenance={
            **_provenance(stage, processor),
            "derived_artifact_role": DerivedArtifact.Role.EMBEDDED_ARTWORK,
        },
    )


def _extract_embedded_artwork(
    stage: ProcessingStage,
    path: Path,
    probe,
) -> tuple[tuple[ArtifactSpec, ...], dict[str, Any], tuple[ResultSpec, ...]]:
    """Extract and locally analyze every FFprobe attached-picture stream."""
    streams = tuple(
        {
            **stream,
            "pixel_preflight": attached_picture_pixel_preflight(
                stream,
                maximum_pixels=int(settings.PROCESSING_MAX_IMAGE_PIXELS),
            ),
        }
        for stream in embedded_artwork_streams_from_probe(probe.streams)
    )
    if not streams:
        return (), {"declared_streams": [], "artifacts": [], "warnings": []}, ()

    duration = probe.technical["format"].get("duration_seconds") or 0
    artifacts = []
    public_artifacts = []
    technical_images = []
    ocr_images = []
    qr_images = []
    warnings = []
    artwork_hashes = []
    for stream in streams:
        _heartbeat(stage)
        stream_index = stream["stream_index"]
        pixel_preflight = stream["pixel_preflight"]
        if pixel_preflight["status"] == "exceeds_limit":
            warnings.append(
                {
                    "stream_index": stream_index,
                    "analyzer": "ffprobe_pixel_preflight",
                    "code": "image_pixel_limit",
                    "declared_width": pixel_preflight["declared_width"],
                    "declared_height": pixel_preflight["declared_height"],
                    "declared_pixel_count": pixel_preflight["declared_pixel_count"],
                    "maximum_pixels": pixel_preflight["maximum_pixels"],
                }
            )
            continue
        temporary, final, relative = _storage_paths(
            stage,
            f"embedded-artwork-{stream_index}.{stream['extension']}",
        )
        try:
            derived = extract_embedded_artwork(
                path,
                temporary,
                stream_index=stream_index,
                codec_name=stream["source_codec"],
                duration_seconds=duration,
            )
        except ExtractorError as error:
            warnings.append(
                {
                    "stream_index": stream_index,
                    "analyzer": "extraction",
                    "code": error.code,
                }
            )
            continue

        publication_created = _publish_artifact(
            temporary,
            final,
            stage=stage,
            expected_sha256=derived.sha256,
            expected_size_bytes=derived.size_bytes,
        )
        artifacts.append(
            _artifact_spec(
                relative,
                derived,
                role=DerivedArtifact.Role.EMBEDDED_ARTWORK,
                metadata={
                    "source_codec": stream["source_codec"],
                    "source_language": stream["language"],
                    "source_title": stream["title"],
                    "source_comment": stream["comment"],
                    "ffprobe_pixel_preflight": pixel_preflight,
                    "remote_transfer_planned": False,
                },
                publication_created=publication_created,
            )
        )
        artwork_hashes.append(derived.sha256)
        context = {
            "stream_index": stream_index,
            "artifact_sha256": derived.sha256,
            "mime_type": derived.mime_type,
            "ffprobe_pixel_preflight": pixel_preflight,
        }
        public_artifacts.append(
            {
                **context,
                "file": relative,
                "byte_size": derived.size_bytes,
                "metadata": dict(derived.metadata or {}),
            }
        )

        try:
            technical = analyze_image(
                final,
                max_pixels=settings.PROCESSING_MAX_IMAGE_PIXELS,
                max_frames=settings.PROCESSING_MAX_IMAGE_FRAMES,
            )
        except ExtractorError as error:
            warnings.append({**context, "analyzer": "image", "code": error.code})
            # OCR and QR decoders may also expand the pixels. Do not invoke
            # them when the common bounded image preflight cannot succeed.
            continue
        technical_images.append({**context, "technical": technical.as_dict()})

        try:
            ocr = extract_ocr(
                final,
                timeout_seconds=TESSERACT_TIMEOUT_SECONDS,
            )
        except ExtractorError as error:
            warnings.append({**context, "analyzer": "ocr", "code": error.code})
        else:
            ocr_images.append({**context, "ocr": ocr.as_dict()})

        try:
            codes = _decode_qr_codes(final)
        except StageProcessingError as error:
            warnings.append({**context, "analyzer": "qr", "code": error.code})
        else:
            qr_images.append({**context, "codes": codes})

    analysis_results: tuple[ResultSpec, ...] = ()
    if artifacts:
        ocr_text = "\n\n".join(
            value["ocr"].get("text", "")
            for value in ocr_images
            if value["ocr"].get("text")
        )
        common = {"source": "embedded_artwork", "warnings": warnings}
        analysis_results = (
            _artwork_analysis_result(
                stage,
                ProcessingResult.Kind.IMAGE_METADATA,
                {**common, "images": technical_images},
                processor="pillow-embedded-artwork",
                artifact_hashes=artwork_hashes,
            ),
            _artwork_analysis_result(
                stage,
                ProcessingResult.Kind.OCR,
                {**common, "images": ocr_images},
                text=ocr_text,
                processor="tesseract-eng-embedded-artwork",
                artifact_hashes=artwork_hashes,
            ),
            _artwork_analysis_result(
                stage,
                ProcessingResult.Kind.QR_CODES,
                {**common, "images": qr_images},
                processor="opencv-qrcode-embedded-artwork",
                artifact_hashes=artwork_hashes,
            ),
        )
    manifest = {
        "declared_streams": list(streams),
        "artifacts": public_artifacts,
        "warnings": warnings,
    }
    return tuple(artifacts), manifest, analysis_results


def _probe(stage: ProcessingStage, result_kind: str) -> StageOutcome:
    path = _source_path(stage)
    value = probe_media(path)
    artifacts, artwork, artwork_results = _extract_embedded_artwork(stage, path, value)
    data = value.as_dict()
    data["embedded_artwork"] = artwork
    return StageOutcome(
        results=(
            _result(
                stage,
                result_kind,
                data,
                processor="ffprobe",
            ),
            *artwork_results,
        ),
        artifacts=artifacts,
        timestamp_claims=_timestamp_specs(
            timestamp_claims_from_ffprobe(value),
            processor="ffprobe",
        ),
    )


def _video_metadata(stage: ProcessingStage) -> StageOutcome:
    return _probe(stage, ProcessingResult.Kind.VIDEO_METADATA)


def _audio_metadata(stage: ProcessingStage) -> StageOutcome:
    return _probe(stage, ProcessingResult.Kind.AUDIO_METADATA)


def _video_subtitles(stage: ProcessingStage) -> StageOutcome:
    path = _source_path(stage)
    probe = probe_media(path)
    duration = probe.technical["format"].get("duration_seconds") or 0
    streams = subtitle_streams_from_probe(probe.streams)
    artifacts = []
    extracted = []
    for stream in streams:
        _heartbeat(stage)
        extension = stream["extension"]
        temporary, final, relative = _storage_paths(
            stage, f"subtitle-{stream['stream_index']}.{extension}"
        )
        value = extract_subtitle_stream(
            path,
            temporary,
            stream_index=stream["stream_index"],
            codec_name=stream["source_codec"],
            duration_seconds=duration,
            language=stream["language"],
            title=stream["title"],
        )
        if value is None:
            continue
        publication_created = _publish_artifact(
            temporary,
            final,
            stage=stage,
            expected_sha256=value.sha256,
            expected_size_bytes=value.size_bytes,
        )
        artifacts.append(
            _artifact_spec(
                relative,
                value,
                role=DerivedArtifact.Role.SUBTITLE,
                publication_created=publication_created,
            )
        )
        extracted.append({**stream, "artifact": relative})
    return StageOutcome(
        results=(
            _result(
                stage,
                ProcessingResult.Kind.SUBTITLES,
                {"streams": list(streams), "extracted": extracted},
                processor="ffprobe+ffmpeg",
            ),
        ),
        artifacts=tuple(artifacts),
    )


def _representative_frame_limit(
    duration_seconds: float,
    scene_changes: tuple[Any, ...],
) -> int:
    """Choose useful coverage without extracting the full 120-frame ceiling."""
    configured = min(120, max(1, int(settings.PROCESSING_MAX_VIDEO_FRAMES)))
    duration = max(0.0, float(duration_seconds))
    endpoint_count = 2 if duration > 0 else 1
    minute_coverage = math.ceil(duration / 60) + 1 if duration > 0 else 1
    scene_times = (
        getattr(value, "timestamp_seconds", value) for value in scene_changes
    )
    scene_coverage = (
        len({round(float(value), 3) for value in scene_times}) + endpoint_count
    )
    return min(configured, max(endpoint_count, minute_coverage, scene_coverage))


_FRAME_UNIT_KEY = re.compile(r"\Aframe:(?P<index>\d{3,})\Z")
_KEYFRAME_PLAN_UNIT_KEYS = frozenset({"plan", "default"})
_KEYFRAME_SELECTION_REASONS = frozenset(
    {"first_frame", "last_frame", "scene_change", "even_coverage"}
)


def _video_keyframe_plan(stage: ProcessingStage) -> StageOutcome:
    """Persist deterministic scene and coverage choices before extraction."""
    path = _source_path(stage)
    probe = probe_media(path)
    value = probe.technical
    video_streams = [
        stream
        for stream in value["streams"]
        if stream.get("codec_type") == "video"
        and stream.get("disposition", {}).get("attached_pic") != 1
    ]
    if not video_streams:
        raise StageProcessingError("video_stream_unavailable")
    stream = video_streams[0]
    stream_index = stream.get("index")
    if (
        not isinstance(stream_index, int)
        or isinstance(stream_index, bool)
        or stream_index < 0
    ):
        raise StageProcessingError("video_stream_invalid")
    try:
        frame_width = int(stream.get("width"))
        frame_height = int(stream.get("height"))
    except (TypeError, ValueError):
        frame_width = frame_height = 0
    if (
        frame_width > 0
        and frame_height > 0
        and frame_width * frame_height > int(settings.PROCESSING_MAX_IMAGE_PIXELS)
    ):
        raise StageProcessingError(
            "image_pixel_limit",
            "The video frame dimensions exceed the configured local pixel limit.",
        )
    try:
        duration = float(value["format"].get("duration_seconds") or 0)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as error:
        raise StageProcessingError("video_duration_invalid") from error
    if not math.isfinite(duration) or duration < 0:
        raise StageProcessingError("video_duration_invalid")
    _heartbeat(stage)
    scene_changes = detect_scene_changes(
        path,
        duration_seconds=duration,
        threshold=DEFAULT_SCENE_CHANGE_THRESHOLD,
        video_stream_index=stream_index,
    )
    frame_limit = _representative_frame_limit(duration, scene_changes)
    selections = representative_frame_selection(
        duration,
        maximum_frames=frame_limit,
        scene_changes=scene_changes,
    )
    frames = [
        {
            "unit_key": f"frame:{index:03d}",
            "selection_index": index,
            "timestamp_seconds": selection.timestamp_seconds,
            "source_stream_index": stream_index,
            "selection_reason": selection.reason,
            "scene_change_score": selection.scene_score,
        }
        for index, selection in enumerate(selections)
    ]
    selection_policy = {
        "endpoints": True,
        "scene_changes": True,
        "even_coverage_interval_seconds": 60,
        "scene_change_threshold": DEFAULT_SCENE_CHANGE_THRESHOLD,
        "scene_score_field": "lavfi.scene_score",
        "scene_candidate_order": "score_descending_timestamp_ascending",
    }
    return StageOutcome(
        results=(
            _result(
                stage,
                ProcessingResult.Kind.KEYFRAMES,
                {
                    "phase": "plan",
                    "frames": frames,
                    "maximum": int(settings.PROCESSING_MAX_VIDEO_FRAMES),
                    "selected_frame_count": len(frames),
                    "duration_seconds": duration,
                    "selection_policy": selection_policy,
                    "scene_candidates": [
                        candidate.as_dict() for candidate in scene_changes
                    ],
                    "video_streams": video_streams,
                },
                processor="ffprobe+ffmpeg-scene-plan",
            ),
        ),
    )


def _persisted_keyframe_plan(
    stage: ProcessingStage,
) -> tuple[ProcessingResult, dict[str, Any]]:
    """Return the immutable plan result for one extraction or aggregate unit."""
    common = {
        "stage__run_id": stage.run_id,
        "stage__kind": ProcessingStage.Kind.VIDEO_KEYFRAMES,
        "stage__status": ProcessingStage.Status.SUCCEEDED,
        "result_kind": ProcessingResult.Kind.KEYFRAMES,
    }
    result = (
        ProcessingResult.objects.select_related("stage")
        .filter(stage__unit_key="plan", **common)
        .order_by("created_at", "pk")
        .first()
    )
    if result is None:
        result = (
            ProcessingResult.objects.select_related("stage")
            .filter(stage__unit_key="default", **common)
            .order_by("created_at", "pk")
            .first()
        )
    data = result.data if result is not None and isinstance(result.data, dict) else {}
    if result is None or data.get("phase") != "plan":
        raise StageProcessingError("keyframe_plan_unavailable")
    return result, data


def _keyframe_plan_coordinate(
    stage: ProcessingStage,
) -> tuple[ProcessingResult, dict[str, Any], dict[str, Any]]:
    """Validate this durable frame unit against its exact persisted coordinate."""
    match = _FRAME_UNIT_KEY.fullmatch(getattr(stage, "unit_key", ""))
    if match is None:
        raise StageProcessingError("keyframe_unit_invalid")
    selection_index = int(match["index"])
    result, plan = _persisted_keyframe_plan(stage)
    frames = plan.get("frames")
    if not isinstance(frames, list) or selection_index >= len(frames):
        raise StageProcessingError("keyframe_plan_invalid")
    coordinate = frames[selection_index]
    if not isinstance(coordinate, dict):
        raise StageProcessingError("keyframe_plan_invalid")
    timestamp = coordinate.get("timestamp_seconds")
    stream_index = coordinate.get("source_stream_index")
    reason = coordinate.get("selection_reason")
    score = coordinate.get("scene_change_score")
    duration = plan.get("duration_seconds")
    valid_score = score is None or (
        isinstance(score, (int, float))
        and not isinstance(score, bool)
        and math.isfinite(float(score))
        and 0 <= float(score) <= 1
    )
    if (
        coordinate.get("unit_key") != stage.unit_key
        or coordinate.get("selection_index") != selection_index
        or not isinstance(timestamp, (int, float))
        or isinstance(timestamp, bool)
        or not math.isfinite(float(timestamp))
        or float(timestamp) < 0
        or not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not math.isfinite(float(duration))
        or float(duration) < float(timestamp)
        or not isinstance(stream_index, int)
        or isinstance(stream_index, bool)
        or stream_index < 0
        or reason not in _KEYFRAME_SELECTION_REASONS
        or not valid_score
        or (reason == "scene_change") != (score is not None)
    ):
        raise StageProcessingError("keyframe_plan_invalid")
    return result, plan, coordinate


def _keyframe_plan_reference(
    result: ProcessingResult,
    coordinate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    reference = {
        "result_id": str(result.pk),
        "stage_id": str(result.stage_id),
        "stage_unit_key": result.stage.unit_key,
    }
    if coordinate is not None:
        reference["coordinate"] = _json_value(coordinate)
    return reference


def _video_keyframe_extract(stage: ProcessingStage) -> StageOutcome:
    """Extract and publish exactly one planned representative frame."""
    plan_result, plan, coordinate = _keyframe_plan_coordinate(stage)
    selection_index = int(coordinate["selection_index"])
    timestamp = float(coordinate["timestamp_seconds"])
    stream_index = int(coordinate["source_stream_index"])
    duration = float(plan["duration_seconds"])
    temporary, final, relative = _storage_paths(
        stage,
        f"frame-{selection_index:03d}.jpg",
    )
    _heartbeat(stage)
    derived = extract_video_frame(
        _source_path(stage),
        temporary,
        timestamp_seconds=timestamp,
        frame_index=selection_index,
        duration_seconds=duration,
        video_stream_index=stream_index,
    )
    publication_created = _publish_artifact(
        Path(derived.path),
        final,
        stage=stage,
        expected_sha256=derived.sha256,
        expected_size_bytes=derived.size_bytes,
    )
    plan_reference = _keyframe_plan_reference(plan_result, coordinate)
    artifact = _artifact_spec(
        relative,
        derived,
        role=DerivedArtifact.Role.KEYFRAME,
        publication_created=publication_created,
        metadata={
            "selection_unit_key": stage.unit_key,
            "selection_reason": coordinate["selection_reason"],
            "scene_change_score": coordinate["scene_change_score"],
            "scene_change_threshold": DEFAULT_SCENE_CHANGE_THRESHOLD,
            "scene_score_field": "lavfi.scene_score",
            "keyframe_plan_result_id": str(plan_result.pk),
            "keyframe_plan_stage_id": str(plan_result.stage_id),
        },
    )
    result = _result(
        stage,
        ProcessingResult.Kind.KEYFRAMES,
        {
            "phase": "extraction",
            "unit_key": stage.unit_key,
            "coordinate": _json_value(coordinate),
            "frame": {
                **derived.as_dict(),
                "file": relative,
                "path": None,
                "selection_reason": coordinate["selection_reason"],
                "scene_change_score": coordinate["scene_change_score"],
            },
        },
        processor="ffmpeg-keyframe-extraction",
    )
    provenance = dict(result.provenance)
    provenance["keyframe_plan"] = plan_reference
    return StageOutcome(
        results=(replace(result, provenance=provenance),),
        artifacts=(artifact,),
    )


def _video_keyframe_aggregate(stage: ProcessingStage) -> StageOutcome:
    """Summarize terminal frame units without redoing successful siblings."""
    plan_result, plan = _persisted_keyframe_plan(stage)
    units = [
        value
        for value in ProcessingStage.objects.filter(
            run=stage.run,
            kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
        ).order_by("unit_key", "pk")
        if _FRAME_UNIT_KEY.fullmatch(value.unit_key)
    ]
    if not units:
        raise StageProcessingError("keyframe_units_unavailable")
    if any(
        value.status
        in {
            ProcessingStage.Status.PENDING,
            ProcessingStage.Status.QUEUED,
            ProcessingStage.Status.RUNNING,
        }
        for value in units
    ):
        raise StageProcessingError("keyframe_units_not_ready", retryable=True)
    successful = [
        value for value in units if value.status == ProcessingStage.Status.SUCCEEDED
    ]
    artifacts = list(
        DerivedArtifact.objects.filter(
            stage_id__in=[value.pk for value in successful],
            role=DerivedArtifact.Role.KEYFRAME,
        ).order_by("frame_index", "created_at", "pk")
    )
    artifact_by_stage: dict[Any, DerivedArtifact] = {}
    for artifact in artifacts:
        if artifact.stage_id in artifact_by_stage:
            raise StageProcessingError("keyframe_unit_ambiguous")
        artifact_by_stage[artifact.stage_id] = artifact
    frame_values = []
    for unit in successful:
        artifact = artifact_by_stage.get(unit.pk)
        match = _FRAME_UNIT_KEY.fullmatch(unit.unit_key)
        expected_index = int(match["index"]) if match is not None else -1
        if artifact is None or artifact.frame_index != expected_index:
            raise StageProcessingError("keyframe_artifact_unavailable")
        frame_values.append(
            {
                "artifact_id": str(artifact.pk),
                "unit_key": unit.unit_key,
                "file": artifact.file.name,
                "mime_type": artifact.mime_type,
                "sha256": artifact.sha256,
                "size_bytes": artifact.byte_size,
                "stream_index": artifact.stream_index,
                "frame_index": artifact.frame_index,
                "source_start_seconds": _json_value(artifact.source_start_seconds),
                "source_end_seconds": _json_value(artifact.source_end_seconds),
                "metadata": _json_value(artifact.metadata),
            }
        )
    failed_units = [
        {
            "unit_key": value.unit_key,
            "status": value.status,
            "failure_code": value.failure_code,
        }
        for value in units
        if value.status != ProcessingStage.Status.SUCCEEDED
    ]
    result = _result(
        stage,
        ProcessingResult.Kind.KEYFRAMES,
        {
            "phase": "aggregate",
            "status": "partial" if failed_units else "succeeded",
            "frames": frame_values,
            "planned_frame_count": len(plan["frames"]),
            "selected_frame_count": len(frame_values),
            "maximum": plan["maximum"],
            "successful_units": [value.unit_key for value in successful],
            "failed_units": failed_units,
            "selection_policy": plan["selection_policy"],
            "video_streams": plan["video_streams"],
        },
        processor="pandect.video-keyframe-aggregate",
    )
    provenance = dict(result.provenance)
    provenance["keyframe_plan"] = _keyframe_plan_reference(plan_result)
    return StageOutcome(results=(replace(result, provenance=provenance),))


def _video_keyframes(stage: ProcessingStage) -> StageOutcome:
    unit_key = getattr(stage, "unit_key", "default")
    if unit_key in _KEYFRAME_PLAN_UNIT_KEYS:
        return _video_keyframe_plan(stage)
    if _FRAME_UNIT_KEY.fullmatch(unit_key):
        return _video_keyframe_extract(stage)
    if unit_key == "aggregate" and isinstance(stage, ProcessingStage):
        return _video_keyframe_aggregate(stage)
    raise StageProcessingError("keyframe_unit_invalid")


_FACE_TRACK_COSINE_THRESHOLD = 0.55
_VOICE_UNIT_KEY = re.compile(r"\Astream:(?P<stream>\d+)/chunk:(?P<chunk>\d{3,})\Z")


def _add_face_to_track(
    tracks: list[dict[str, Any]],
    member: dict[str, Any],
) -> int:
    """Greedily correlate compatible SFace features without exposing them."""
    observation = member["observation"]
    embedding = tuple(observation.embedding)
    selected_index = None
    selected_similarity = -1.0
    for index, track in enumerate(tracks):
        if track["feature_space"] != observation.feature_space:
            continue
        try:
            similarity = cosine_similarity(embedding, track["centroid"])
        except ValueError:
            continue
        if (
            similarity >= _FACE_TRACK_COSINE_THRESHOLD
            and similarity > selected_similarity
        ):
            selected_index = index
            selected_similarity = similarity

    if selected_index is None:
        tracks.append(
            {
                "feature_space": observation.feature_space,
                "centroid": embedding,
                "members": [member],
                "best": member,
            }
        )
        return len(tracks)

    track = tracks[selected_index]
    count = len(track["members"])
    mean = tuple(
        (track["centroid"][index] * count + embedding[index]) / (count + 1)
        for index in range(len(embedding))
    )
    magnitude = math.sqrt(sum(value * value for value in mean))
    if magnitude > 0:
        track["centroid"] = tuple(value / magnitude for value in mean)
    track["members"].append(member)
    if observation.quality_score > track["best"]["observation"].quality_score:
        track["best"] = member
    return selected_index + 1


def _aggregate_video_frame_units(stage: ProcessingStage) -> StageOutcome:
    """Correlate persisted child observations without re-running frame tools."""
    unit_stages = list(
        ProcessingStage.objects.filter(
            run=stage.run,
            kind=ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
        )
        .exclude(pk=stage.pk)
        .order_by("unit_key", "pk")
    )
    successful_ids = [
        value.pk
        for value in unit_stages
        if value.status == ProcessingStage.Status.SUCCEEDED
    ]
    child_results = list(
        ProcessingResult.objects.filter(stage_id__in=successful_ids)
        .order_by("stage__unit_key", "created_at", "pk")
        .values_list("pk", flat=True)
    )
    observations = list(
        IdentityObservation.objects.filter(
            catalog_item=stage.run.catalog_item,
            modality=IdentityObservation.Modality.FACE,
            provenance__processing_stage_id__in=[
                str(value) for value in successful_ids
            ],
        ).order_by("created_at", "pk")
    )
    tracks: list[dict[str, Any]] = []
    for observation in observations:
        vector = observation_vector(observation).values
        selected = None
        selected_similarity = -1.0
        for candidate in tracks:
            if candidate["feature_space"] != observation.feature_space:
                continue
            similarity = cosine_similarity(vector, candidate["centroid"])
            if (
                similarity >= _FACE_TRACK_COSINE_THRESHOLD
                and similarity > selected_similarity
            ):
                selected = candidate
                selected_similarity = similarity
        member = {"observation": observation, "vector": vector}
        if selected is None:
            tracks.append(
                {
                    "feature_space": observation.feature_space,
                    "centroid": vector,
                    "members": [member],
                    "best": member,
                }
            )
            continue
        count = len(selected["members"])
        mean = tuple(
            (selected["centroid"][index] * count + vector[index]) / (count + 1)
            for index in range(len(vector))
        )
        magnitude = math.sqrt(sum(value * value for value in mean))
        selected["centroid"] = (
            tuple(value / magnitude for value in mean) if magnitude else mean
        )
        selected["members"].append(member)
        if (observation.quality_score or 0) > (
            selected["best"]["observation"].quality_score or 0
        ):
            selected["best"] = member

    identity_specs = []
    public_tracks = []
    for track_index, track in enumerate(tracks, start=1):
        best = track["best"]["observation"]
        members = [value["observation"] for value in track["members"]]
        provenance = dict(best.provenance)
        provenance["identity_evidence_role"] = VIDEO_FACE_AGGREGATE_ROLE
        provenance["video_face_tracking"] = {
            "revision": "greedy-sface-cosine-v1",
            "cosine_threshold": _FACE_TRACK_COSINE_THRESHOLD,
            "member_observation_ids": [str(value.pk) for value in members],
        }
        quality_status = (
            IdentityObservation.QualityStatus.APPROVED
            if best.quality_details.get("quality_assessment") == "usable"
            else IdentityObservation.QualityStatus.UNASSESSED
        )
        identity_specs.append(
            IdentityObservationSpec(
                modality=IdentityObservation.Modality.FACE,
                unit_key=f"track:{track_index:03d}",
                source_location=dict(best.source_location),
                source_sha256=best.source_sha256,
                embedding=encode_embedding(track["centroid"]),
                embedding_dimension=len(track["centroid"]),
                feature_space=best.feature_space,
                processor_name="pandect.video-face-track",
                processor_version=PROCESSOR_VERSION,
                preprocessing_version="yunet-sface-alignment+tracking-v1",
                model_identifier=best.model_identifier,
                model_version=best.model_version,
                model_sha256=best.model_sha256,
                provenance=provenance,
                quality_status=quality_status,
                quality_score=best.quality_score,
                quality_details={
                    **dict(best.quality_details),
                    "correlated_frame_count": len(members),
                },
            )
        )
        public_tracks.append(
            {
                "track_id": track_index,
                "frame_count": len(members),
                "representative_frame_index": best.source_location.get("frame_index"),
                "representative_timestamp_ms": best.source_location.get("timestamp_ms"),
                "quality_score": best.quality_score,
                "quality_assessment": best.quality_details.get("quality_assessment"),
                "member_observation_ids": [str(value.pk) for value in members],
            }
        )
    failed_analysis_units = [
        value.unit_key
        for value in unit_stages
        if value.status != ProcessingStage.Status.SUCCEEDED
    ]
    failed_keyframe_units = list(
        ProcessingStage.objects.filter(
            run=stage.run,
            kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
            unit_key__startswith="frame:",
        )
        .exclude(status=ProcessingStage.Status.SUCCEEDED)
        .order_by("unit_key", "pk")
        .values_list("unit_key", flat=True)
    )
    failed_units = sorted(set(failed_analysis_units + failed_keyframe_units))
    return StageOutcome(
        results=(
            _model_result(
                stage,
                ProcessingResult.Kind.FACE_OBSERVATIONS,
                {
                    "unit_result_ids": [str(value) for value in child_results],
                    "successful_units": len(successful_ids),
                    "failed_units": failed_units,
                    "tracks": public_tracks,
                    "correlation": {
                        "revision": "greedy-sface-cosine-v1",
                        "cosine_threshold": _FACE_TRACK_COSINE_THRESHOLD,
                        "scores_are_probabilities": False,
                    },
                },
                processor="pandect.video-face-track",
            ),
        ),
        identity_observations=tuple(identity_specs),
    )


def _video_frame_analysis(stage: ProcessingStage) -> StageOutcome:
    """Analyze persisted, metadata-free keyframes and correlate face tracks."""
    unit_key = getattr(stage, "unit_key", "aggregate")
    if isinstance(stage, ProcessingStage) and unit_key in {"aggregate", "default"}:
        return _aggregate_video_frame_units(stage)
    frames_query = stage.run.catalog_item.derived_artifacts.filter(
        role=DerivedArtifact.Role.KEYFRAME,
        stage__run=stage.run,
        stage__kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
    )
    unit_match = _FRAME_UNIT_KEY.fullmatch(unit_key)
    if unit_match is not None:
        frames_query = frames_query.filter(frame_index=int(unit_match["index"]))
    elif unit_key not in {"aggregate", "default"}:
        raise StageProcessingError("frame_unit_invalid")
    frames = list(
        frames_query.order_by("frame_index", "created_at", "pk")[
            : min(120, max(1, int(settings.PROCESSING_MAX_VIDEO_FRAMES)))
        ]
    )
    if not frames:
        raise StageProcessingError("keyframes_unavailable")
    if unit_match is not None and len(frames) != 1:
        raise StageProcessingError("keyframe_unit_ambiguous")

    technical_frames = []
    ocr_frames = []
    qr_frames = []
    face_frames = []
    warnings = []
    face_tracks: list[dict[str, Any]] = []

    for artifact in frames:
        _heartbeat(stage)
        frame_index = int(artifact.frame_index or 0)
        timestamp_ms = round(float(artifact.source_start_seconds or 0) * 1000)
        frame_context = {
            "artifact_id": str(artifact.pk),
            "artifact_sha256": artifact.sha256,
            "frame_index": frame_index,
            "timestamp_ms": timestamp_ms,
            "selection_reason": artifact.metadata.get("selection_reason"),
            "scene_change_score": artifact.metadata.get("scene_change_score"),
        }
        if artifact.metadata.get("metadata_stripped") is not True:
            raise StageProcessingError("keyframe_metadata_not_stripped")
        path = _verified_derived_artifact_path(artifact)
        try:
            technical = analyze_image(
                path,
                max_pixels=settings.PROCESSING_MAX_IMAGE_PIXELS,
                max_frames=1,
            )
        except ExtractorError as error:
            warnings.append({**frame_context, "analyzer": "image", "code": error.code})
            continue
        technical_frames.append({**frame_context, "technical": technical.as_dict()})

        try:
            ocr = extract_ocr(
                path,
                timeout_seconds=TESSERACT_TIMEOUT_SECONDS,
            )
        except ExtractorError as error:
            warnings.append({**frame_context, "analyzer": "ocr", "code": error.code})
        else:
            ocr_frames.append({**frame_context, "ocr": ocr.as_dict()})

        try:
            codes = _decode_qr_codes(path)
        except StageProcessingError as error:
            warnings.append({**frame_context, "analyzer": "qr", "code": error.code})
        else:
            qr_frames.append({**frame_context, "codes": codes})

        try:
            face_analysis = analyze_faces(
                path,
                model_paths=FaceModelPaths.from_root(Path(settings.MODELS_ROOT)),
            )
        except ExtractorError as error:
            if error.code in {
                "local_model_unavailable",
                "local_model_integrity",
            }:
                # Digest-pinned face models are required for this explicit
                # stage.  A missing or altered model is operationally
                # actionable and must remain visible as a durable failure.
                raise
            warnings.append({**frame_context, "analyzer": "faces", "code": error.code})
            continue

        public_faces = []
        for observation in face_analysis.observations:
            member = {
                **frame_context,
                "observation": observation,
            }
            track_id = _add_face_to_track(face_tracks, member)
            public_faces.append({**observation.as_dict(), "track_id": track_id})
        face_frames.append({**frame_context, "faces": public_faces})

    # Do not allow results attributed to an artifact's recorded fingerprint to
    # leave the processor if its bytes changed while local analysis was running.
    for artifact in frames:
        _verified_derived_artifact_path(artifact)

    identity_specs = []
    public_tracks = []
    for track_index, track in enumerate(face_tracks, start=1):
        best = track["best"]
        observation = best["observation"]
        sface = observation.provenance["models"]["sface"]
        members = [
            {
                "artifact_sha256": member["artifact_sha256"],
                "frame_index": member["frame_index"],
                "timestamp_ms": member["timestamp_ms"],
            }
            for member in track["members"]
        ]
        provenance = dict(observation.provenance)
        provenance["identity_evidence_role"] = (
            VIDEO_FACE_FRAME_ROLE
            if unit_match is not None
            else VIDEO_FACE_AGGREGATE_ROLE
        )
        provenance["video_face_tracking"] = {
            "revision": "greedy-sface-cosine-v1",
            "cosine_threshold": _FACE_TRACK_COSINE_THRESHOLD,
            "members": members,
        }
        quality_status = (
            IdentityObservation.QualityStatus.APPROVED
            if observation.quality_assessment == "usable"
            else IdentityObservation.QualityStatus.UNASSESSED
        )
        identity_specs.append(
            IdentityObservationSpec(
                modality=IdentityObservation.Modality.FACE,
                unit_key=f"track:{track_index:03d}",
                source_location={
                    "bbox": list(observation.bbox),
                    "frame_index": best["frame_index"],
                    "timestamp_ms": best["timestamp_ms"],
                },
                source_sha256=best["artifact_sha256"],
                embedding=encode_embedding(track["centroid"]),
                embedding_dimension=len(track["centroid"]),
                feature_space=observation.feature_space,
                processor_name="pandect.video-face-track",
                processor_version=PROCESSOR_VERSION,
                preprocessing_version="yunet-sface-alignment+tracking-v1",
                model_identifier=str(sface["identifier"]),
                model_version="2021dec",
                model_sha256=str(sface["sha256"]),
                provenance=provenance,
                quality_status=quality_status,
                quality_score=observation.quality_score,
                quality_details={
                    **dict(observation.quality_details),
                    "quality_assessment": observation.quality_assessment,
                    "correlated_frame_count": len(track["members"]),
                },
            )
        )
        public_tracks.append(
            {
                "track_id": track_index,
                "frame_count": len(track["members"]),
                "representative_frame_index": best["frame_index"],
                "representative_timestamp_ms": best["timestamp_ms"],
                "quality_score": observation.quality_score,
                "quality_assessment": observation.quality_assessment,
            }
        )

    ocr_text = "\n\n".join(
        value["ocr"].get("text", "") for value in ocr_frames if value["ocr"].get("text")
    )
    return StageOutcome(
        results=(
            _artifact_bound_result(
                _result(
                    stage,
                    ProcessingResult.Kind.IMAGE_VISUAL,
                    {"frames": technical_frames, "warnings": warnings},
                    processor="pillow-video-frames",
                ),
                stage,
                frames[0],
            ),
            _artifact_bound_result(
                _result(
                    stage,
                    ProcessingResult.Kind.OCR,
                    {"frames": ocr_frames, "warnings": warnings},
                    text=ocr_text,
                    processor="tesseract-eng-video-frames",
                ),
                stage,
                frames[0],
            ),
            _artifact_bound_result(
                _result(
                    stage,
                    ProcessingResult.Kind.QR_CODES,
                    {"frames": qr_frames, "warnings": warnings},
                    processor="opencv-qrcode-video-frames",
                ),
                stage,
                frames[0],
            ),
            _artifact_bound_result(
                _model_result(
                    stage,
                    ProcessingResult.Kind.FACE_OBSERVATIONS,
                    {
                        "frames": face_frames,
                        "tracks": public_tracks,
                        "warnings": warnings,
                        "correlation": {
                            "revision": "greedy-sface-cosine-v1",
                            "cosine_threshold": _FACE_TRACK_COSINE_THRESHOLD,
                            "scores_are_probabilities": False,
                        },
                    },
                    processor="pandect.video-face-track",
                ),
                stage,
                frames[0],
            ),
        ),
        consumed_artifacts=tuple(
            ConsumedArtifactSpec(
                artifact_id=artifact.pk,
                sha256=artifact.sha256,
                byte_size=artifact.byte_size,
            )
            for artifact in frames
        ),
        identity_observations=tuple(identity_specs),
    )


def _video_audio(stage: ProcessingStage) -> StageOutcome:
    path = _source_path(stage)
    value = probe_media(path).technical
    streams = [
        stream for stream in value["streams"] if stream.get("codec_type") == "audio"
    ]
    artifacts = []
    artifact_values = []
    duration = value["format"].get("duration_seconds") or 0
    for stream in streams:
        _heartbeat(stage)
        stream_index = stream.get("index")
        if not isinstance(stream_index, int) or isinstance(stream_index, bool):
            raise StageProcessingError("audio_stream_index_invalid")
        stream_duration = stream.get("duration_seconds")
        if stream_duration is None:
            stream_duration = duration
        output = faithful_audio_output_for_codec(stream.get("codec_name"))
        faithful_temporary, faithful_final, faithful_relative = _storage_paths(
            stage, f"source-audio-stream-{stream_index}.{output.extension}"
        )
        faithful = extract_faithful_audio_stream(
            path,
            faithful_temporary,
            stream_index=stream_index,
            duration_seconds=stream_duration,
            codec_name=stream.get("codec_name"),
            sample_rate=stream.get("sample_rate"),
            channels=stream.get("channels"),
            channel_layout=stream.get("channel_layout"),
            sample_format=stream.get("sample_fmt"),
            bits_per_raw_sample=stream.get("bits_per_raw_sample"),
            profile=stream.get("profile"),
            codec_tag=stream.get("codec_tag_string"),
        )
        faithful_publication_created = _publish_artifact(
            faithful_temporary,
            faithful_final,
            stage=stage,
            expected_sha256=faithful.sha256,
            expected_size_bytes=faithful.size_bytes,
        )
        artifacts.append(
            _artifact_spec(
                faithful_relative,
                faithful,
                role=DerivedArtifact.Role.AUDIO_STREAM,
                metadata={
                    "source_language": stream.get("tags", {}).get("language"),
                    "source_title": stream.get("tags", {}).get("title"),
                    "remote_transfer_planned": False,
                },
                publication_created=faithful_publication_created,
            )
        )
        artifact_values.append(
            {
                **faithful.as_dict(),
                "file": faithful_relative,
                "path": None,
                "role": DerivedArtifact.Role.AUDIO_STREAM,
            }
        )

        _heartbeat(stage)
        normalized_temporary, normalized_final, normalized_relative = _storage_paths(
            stage, f"analysis-audio-stream-{stream_index}.flac"
        )
        normalized = extract_normalized_audio_stream(
            path,
            normalized_temporary,
            stream_index=stream_index,
            duration_seconds=stream_duration,
        )
        normalized_publication_created = _publish_artifact(
            normalized_temporary,
            normalized_final,
            stage=stage,
            expected_sha256=normalized.sha256,
            expected_size_bytes=normalized.size_bytes,
        )
        artifacts.append(
            _artifact_spec(
                normalized_relative,
                normalized,
                role=DerivedArtifact.Role.NORMALIZED_MEDIA,
                metadata={
                    "derivative_purpose": "mono_16khz_analysis_audio_stream",
                    "source_stream_index": stream_index,
                    "source_codec": stream.get("codec_name") or "not_available",
                    "source_sample_rate": stream.get("sample_rate") or "not_available",
                    "source_channels": stream.get("channels") or "not_available",
                    "source_channel_layout": stream.get("channel_layout")
                    or "not_available",
                    "source_language": stream.get("tags", {}).get("language"),
                    "remote_transfer_planned": False,
                },
                publication_created=normalized_publication_created,
            )
        )
        artifact_values.append(
            {
                **normalized.as_dict(),
                "file": normalized_relative,
                "path": None,
                "role": DerivedArtifact.Role.NORMALIZED_MEDIA,
            }
        )
    return StageOutcome(
        results=(
            _result(
                stage,
                ProcessingResult.Kind.AUDIO_STREAMS,
                {
                    "streams": streams,
                    "artifacts": artifact_values,
                    "has_audio": bool(streams),
                },
                processor="ffprobe+ffmpeg",
            ),
        ),
        artifacts=tuple(artifacts),
    )


def _audio_split(stage: ProcessingStage) -> StageOutcome:
    path = _source_path(stage)
    value = probe_media(path)
    duration = value.technical["format"].get("duration_seconds") or 0
    audio_streams = [
        stream for stream in value.streams if stream.get("codec_type") == "audio"
    ]
    artifacts = []
    artifact_values = []
    stream_values = []
    for stream in audio_streams:
        _heartbeat(stage)
        stream_index = stream.get("index")
        stream_duration = stream.get("duration_seconds")
        if stream_duration is None:
            stream_duration = duration
        silences = detect_silence(
            path,
            duration_seconds=stream_duration,
            stream_index=stream_index,
        )
        plan = plan_audio_splits(
            stream_duration,
            chapters=value.chapters,
            silences=silences,
        )
        stream_artifacts = []
        provider_split_encodings = 0
        provider_split_encodings_by_initial_chunk: dict[int, int] = {}
        next_chunk_index = 0

        def materialize_chunk(
            chunk,
            *,
            depth: int,
            split_path: str,
            initial_chunk_index: int,
            bound_stream_index=stream_index,
            bound_stream_artifacts=stream_artifacts,
            bound_encoding_counts=provider_split_encodings_by_initial_chunk,
        ) -> None:
            nonlocal provider_split_encodings, next_chunk_index
            initial_encodings = bound_encoding_counts.get(initial_chunk_index, 0)
            if initial_encodings >= MAX_PROVIDER_SPLIT_ENCODINGS:
                raise StageProcessingError(
                    "audio_chunk_payload_limit",
                    "A normalized audio segment could not be reduced below the "
                    "remote adapter payload limit within bounded local splitting.",
                )
            provider_split_encodings += 1
            bound_encoding_counts[initial_chunk_index] = initial_encodings + 1
            _heartbeat(stage)
            temporary, final, relative = _storage_paths(
                stage,
                f"audio-stream-{bound_stream_index}-chunk-{next_chunk_index:03d}.flac",
            )
            derived = extract_normalized_audio_chunk(
                path,
                temporary,
                stream_index=bound_stream_index,
                start_seconds=chunk.start_seconds,
                end_seconds=chunk.end_seconds,
                overlap_before_seconds=chunk.overlap_before_seconds,
            )
            if derived.size_bytes > MAX_TRANSCRIPTION_AUDIO_BYTES:
                try:
                    temporary.unlink()
                except OSError as error:
                    raise StageProcessingError(
                        "audio_chunk_temporary_cleanup_failed"
                    ) from error
                if depth >= MAX_PROVIDER_SPLIT_DEPTH:
                    raise StageProcessingError(
                        "audio_chunk_payload_limit",
                        "A normalized audio segment could not be reduced below the "
                        "remote adapter payload limit within bounded local splitting.",
                    )
                try:
                    children = split_audio_chunk_for_payload_limit(chunk)
                except ValueError as error:
                    raise StageProcessingError(
                        "audio_chunk_payload_limit",
                        "A normalized audio segment is too short to split safely "
                        "below the remote adapter payload limit.",
                    ) from error
                materialize_chunk(
                    children[0],
                    depth=depth + 1,
                    split_path=f"{split_path}L",
                    initial_chunk_index=initial_chunk_index,
                )
                materialize_chunk(
                    children[1],
                    depth=depth + 1,
                    split_path=f"{split_path}R",
                    initial_chunk_index=initial_chunk_index,
                )
                return

            artifact_metadata = {
                "chunk_index": next_chunk_index,
                "initial_chunk_index": chunk.index,
                "end_reason": chunk.end_reason,
                "provider_payload_limit_bytes": MAX_TRANSCRIPTION_AUDIO_BYTES,
                "provider_split_applied": depth > 0,
                "provider_split_depth": depth,
                "provider_split_path": split_path or "root",
            }
            publication_created = _publish_artifact(
                temporary,
                final,
                stage=stage,
                expected_sha256=derived.sha256,
                expected_size_bytes=derived.size_bytes,
            )
            artifacts.append(
                _artifact_spec(
                    relative,
                    derived,
                    role=DerivedArtifact.Role.AUDIO_CHUNK,
                    metadata=artifact_metadata,
                    publication_created=publication_created,
                )
            )
            public_artifact = {
                **derived.as_dict(),
                "file": relative,
                "path": None,
                **artifact_metadata,
            }
            artifact_values.append(public_artifact)
            bound_stream_artifacts.append(public_artifact)
            next_chunk_index += 1

        for chunk in plan.chunks:
            materialize_chunk(
                chunk,
                depth=0,
                split_path="",
                initial_chunk_index=chunk.index,
            )
        stream_values.append(
            {
                "stream_index": stream_index,
                "duration_seconds": stream_duration,
                "source_codec": stream.get("codec_name"),
                "plan": plan.as_dict(),
                "provider_payload_limit_bytes": MAX_TRANSCRIPTION_AUDIO_BYTES,
                "provider_split_encodings": provider_split_encodings,
                "provider_split_encodings_by_initial_chunk": {
                    str(index): count
                    for index, count in sorted(
                        provider_split_encodings_by_initial_chunk.items()
                    )
                },
                "provider_split_applied": any(
                    artifact["provider_split_applied"] for artifact in stream_artifacts
                ),
                "silences": [
                    {
                        "start_seconds": silence.start_seconds,
                        "end_seconds": silence.end_seconds,
                    }
                    for silence in silences
                ],
                "artifacts": stream_artifacts,
            }
        )
    return StageOutcome(
        results=(
            _result(
                stage,
                ProcessingResult.Kind.AUDIO_SPLITS,
                {
                    "has_audio": bool(audio_streams),
                    "streams": stream_values,
                    "artifacts": artifact_values,
                },
                processor="pandect.audio-split-planner+ffmpeg",
            ),
        ),
        artifacts=tuple(artifacts),
    )


def _audio_acoustics(stage: ProcessingStage) -> StageOutcome:
    value = probe_media(_source_path(stage))
    duration = value.technical["format"].get("duration_seconds") or 0
    audio_streams = [
        stream for stream in value.streams if stream.get("codec_type") == "audio"
    ]
    observations = []
    for stream in audio_streams:
        _heartbeat(stage)
        stream_duration = stream.get("duration_seconds")
        if stream_duration is None:
            stream_duration = duration
        measurement = analyze_acoustics(
            _source_path(stage),
            duration_seconds=stream_duration,
            stream_index=stream.get("index"),
            progress_callback=lambda: _heartbeat(stage),
        )
        observations.append(
            {
                "stream_index": stream.get("index"),
                "duration_seconds": stream_duration,
                **measurement.as_dict(),
            }
        )
    return StageOutcome(
        results=(
            _result(
                stage,
                ProcessingResult.Kind.ACOUSTICS,
                {"has_audio": bool(audio_streams), "streams": observations},
                processor="ffmpeg-acoustic-analysis",
            ),
        )
    )


def _read_flac_waveform(
    stage: ProcessingStage,
    path: Path,
) -> tuple[array[float], int]:
    """Decode normalized FLAC into one compact float32 sample buffer."""
    pcm_path = _scratch_path(stage, ".wav")
    try:
        run_command(
            (
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-v",
                "error",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-filter_complex_threads",
                "1",
                "-i",
                path,
                "-map_metadata",
                "-1",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-threads",
                "1",
                "-n",
                pcm_path,
            ),
            timeout_seconds=2 * 60 * 60,
        )
        with wave.open(str(pcm_path), "rb") as source:
            if source.getnchannels() != 1 or source.getframerate() != 16_000:
                raise StageProcessingError("normalized_audio_invalid")
            if source.getsampwidth() != 2:
                raise StageProcessingError("normalized_audio_invalid")
            if source.getnframes() > 601 * 16_000:
                raise StageProcessingError("normalized_audio_too_long")
            raw = source.readframes(source.getnframes())
        integers = array("h")
        integers.frombytes(raw)
        del raw
        if struct.pack("=h", 1) != struct.pack("<h", 1):
            integers.byteswap()
        samples = array("f", integers)
        del integers
        scale = 1.0 / 32768.0
        for index in range(len(samples)):
            samples[index] *= scale
        return samples, 16_000
    finally:
        try:
            pcm_path.unlink()
        except OSError:
            pass


def _voice_analysis(stage: ProcessingStage) -> StageOutcome:
    unit_key = getattr(stage, "unit_key", "aggregate")
    if isinstance(stage, ProcessingStage) and unit_key in {"aggregate", "default"}:
        unit_stages = list(
            ProcessingStage.objects.filter(
                run=stage.run,
                kind=ProcessingStage.Kind.VOICE_ANALYSIS,
            )
            .exclude(pk=stage.pk)
            .order_by("unit_key", "pk")
        )
        successful_ids = [
            value.pk
            for value in unit_stages
            if value.status == ProcessingStage.Status.SUCCEEDED
        ]
        child_results = list(
            ProcessingResult.objects.filter(
                stage_id__in=successful_ids,
                result_kind=ProcessingResult.Kind.VOICE_OBSERVATIONS,
            ).order_by("stage__unit_key", "created_at", "pk")
        )
        speech_regions = []
        non_speech_regions = []
        observations = []
        for result in child_results:
            data = result.data if isinstance(result.data, dict) else {}
            speech_regions.extend(data.get("speech_regions", []))
            non_speech_regions.extend(data.get("non_speech_regions", []))
            observations.extend(data.get("observations", []))
        reason = "no_audio_stream" if not unit_stages else "no_successful_units"
        return StageOutcome(
            results=(
                replace(
                    _model_result(
                        stage,
                        ProcessingResult.Kind.VOICE_OBSERVATIONS,
                        {
                            "schema_version": "2",
                            "speech_regions": speech_regions,
                            "non_speech_regions": non_speech_regions,
                            "observations": observations,
                            "overlap_status": "not_assessed",
                            "unit_result_ids": [
                                str(value.pk) for value in child_results
                            ],
                            "successful_units": len(successful_ids),
                            "failed_units": [
                                value.unit_key
                                for value in unit_stages
                                if value.status != ProcessingStage.Status.SUCCEEDED
                            ],
                            "reason": reason,
                        },
                        processor="pandect.local-voice-observation-aggregate",
                    ),
                    schema_version=2,
                ),
            )
        )

    chunks_query = stage.run.catalog_item.derived_artifacts.filter(
        role=DerivedArtifact.Role.AUDIO_CHUNK,
        stage__run=stage.run,
        stage__kind=ProcessingStage.Kind.AUDIO_SPLIT,
    )
    unit_match = _VOICE_UNIT_KEY.fullmatch(unit_key)
    if unit_match is None:
        raise StageProcessingError("voice_unit_invalid")
    chunks_query = chunks_query.filter(
        stream_index=int(unit_match["stream"]),
        metadata__chunk_index=int(unit_match["chunk"]),
    )
    chunks = list(
        chunks_query.order_by(
            "stream_index", "source_start_seconds", "created_at", "pk"
        )[:2]
    )
    if len(chunks) > 1:
        raise StageProcessingError("audio_chunk_unit_ambiguous")
    if not chunks:
        raise StageProcessingError("audio_chunk_unavailable")
    public_observations = []
    identity_specs = []
    speech_regions = []
    non_speech_regions = []
    for chunk in chunks:
        _heartbeat(stage)
        path = _verified_derived_artifact_path(chunk)
        waveform, sample_rate = _read_flac_waveform(stage, path)
        source_start_ms = round(float(chunk.source_start_seconds or 0) * 1000)
        analysis = analyze_voice(
            waveform,
            model_paths=VoiceModelPaths.from_root(Path(settings.MODELS_ROOT)),
            sample_rate=sample_rate,
            source_start_ms=source_start_ms,
        )
        chunk_index = int(chunk.metadata.get("chunk_index", 0))
        stream_index = chunk.stream_index
        speech_regions.extend(
            {
                **dict(value),
                "stream_index": stream_index,
                "chunk_index": chunk_index,
                "artifact_sha256": chunk.sha256,
            }
            for value in analysis.speech_regions
        )
        non_speech_regions.extend(
            {
                **dict(value),
                "stream_index": stream_index,
                "chunk_index": chunk_index,
                "artifact_sha256": chunk.sha256,
            }
            for value in getattr(analysis, "non_speech_regions", ())
        )
        public_observations.extend(
            {
                **value.as_dict(),
                "stream_index": stream_index,
                "chunk_index": chunk_index,
                "artifact_sha256": chunk.sha256,
            }
            for value in analysis.observations
        )
        model = analysis.provenance["models"]["campplus"]
        for value in analysis.observations:
            if value.embedding is None:
                continue
            quality_status = (
                IdentityObservation.QualityStatus.APPROVED
                if value.quality_assessment == "usable"
                else IdentityObservation.QualityStatus.UNASSESSED
            )
            identity_specs.append(
                IdentityObservationSpec(
                    modality=IdentityObservation.Modality.VOICE,
                    unit_key=(
                        f"stream:{stream_index}/chunk:{chunk_index:03d}/"
                        f"{value.unit_key}"
                    ),
                    source_location={
                        "start_ms": value.start_ms,
                        "end_ms": value.end_ms,
                        "stream_index": stream_index,
                    },
                    source_sha256=chunk.sha256,
                    embedding=encode_embedding(value.embedding),
                    embedding_dimension=len(value.embedding),
                    feature_space=value.feature_space,
                    processor_name="pandect.local-voice-observation",
                    processor_version=PROCESSOR_VERSION,
                    preprocessing_version="mono-16khz-float-v1",
                    model_identifier=str(model["identifier"]),
                    model_version="1.0.0",
                    model_sha256=str(model["sha256"]),
                    provenance={
                        **dict(value.provenance),
                        "audio_chunk": {
                            "sha256": chunk.sha256,
                            "stream_index": stream_index,
                            "chunk_index": chunk_index,
                            "source_start_seconds": str(
                                chunk.source_start_seconds or 0
                            ),
                            "source_end_seconds": str(chunk.source_end_seconds or 0),
                        },
                    },
                    quality_status=quality_status,
                    quality_score=value.quality_score,
                    quality_details=dict(value.quality_details),
                    sample_duration_ms=value.sample_duration_ms,
                )
            )
    # Recheck after decoding/inference so an outcome is never persisted against
    # a stale chunk hash if the derived file changed during analysis.
    for chunk in chunks:
        _verified_derived_artifact_path(chunk)
    result = replace(
        _model_result(
            stage,
            ProcessingResult.Kind.VOICE_OBSERVATIONS,
            {
                "schema_version": "2",
                "speech_regions": speech_regions,
                "non_speech_regions": non_speech_regions,
                "observations": public_observations,
                "overlap_status": "not_assessed",
            },
            processor="pandect.local-voice-observation",
        ),
        schema_version=2,
    )
    return StageOutcome(
        results=(_artifact_bound_result(result, stage, chunks[0]),),
        consumed_artifacts=tuple(
            ConsumedArtifactSpec(
                artifact_id=chunk.pk,
                sha256=chunk.sha256,
                byte_size=chunk.byte_size,
            )
            for chunk in chunks
        ),
        identity_observations=tuple(identity_specs),
    )


def _identity_candidates(stage: ProcessingStage, modality: str) -> StageOutcome:
    observations_query = IdentityObservation.objects.filter(
        catalog_item=stage.run.catalog_item,
        modality=modality,
    )
    if (
        modality == IdentityObservation.Modality.FACE
        and stage.run.catalog_item.item_type == CatalogItem.ItemType.VIDEO
    ):
        observations_query = observations_query.filter(
            provenance__identity_evidence_role=VIDEO_FACE_AGGREGATE_ROLE
        )
    if stage.run.configuration.get("workflow") == "identity_gallery_rematch":
        # An observation hashes the bytes from which its feature was computed.
        # For video faces and audio voices those bytes are a derived keyframe or
        # chunk, not the canonical item. Follow the immutable originating-run
        # provenance to establish that the observation belongs to this exact
        # canonical source, while retaining the derivative hash on the
        # observation itself.
        compatible_run_ids = list(
            ProcessingRun.objects.filter(
                catalog_item_id=stage.run.catalog_item_id,
                scope=ProcessingRun.Scope.LOCAL,
                source_fingerprint=stage.run.source_fingerprint,
                source_size=stage.run.source_size,
            )
            .exclude(pk=stage.run_id)
            .order_by("-created_at", "-pk")
            .values_list("pk", flat=True)[:IDENTITY_REMATCH_OBSERVATION_LIMIT]
        )
        observations_query = observations_query.filter(
            provenance__processing_run_id__in=[
                str(run_id) for run_id in compatible_run_ids
            ]
        )
        observations = list(
            observations_query.order_by("-created_at", "-pk")[
                :IDENTITY_REMATCH_OBSERVATION_LIMIT
            ]
        )
    else:
        # Ordinary workflows rank only the observations produced by their
        # dependency stage. Historical observations remain immutable evidence.
        observations_query = observations_query.filter(
            provenance__processing_run_id=str(stage.run_id)
        )
        observations = list(observations_query.order_by("created_at", "pk"))
    observations = [
        observation
        for observation in observations
        if observation_is_gallery_eligible(observation)
    ]
    result_kinds = {
        IdentityObservation.Modality.FACE: ProcessingResult.Kind.FACE_CANDIDATES,
        IdentityObservation.Modality.VOICE: ProcessingResult.Kind.VOICE_CANDIDATES,
        IdentityObservation.Modality.WRITING: (
            ProcessingResult.Kind.AUTHORSHIP_CANDIDATES
        ),
    }
    try:
        result_kind = result_kinds[modality]
    except KeyError as error:
        raise StageProcessingError("identity_modality_unsupported") from error
    if not observations:
        return StageOutcome(
            results=(
                _model_result(
                    stage,
                    result_kind,
                    {
                        "candidates": [],
                        "unknown": True,
                        "unknown_label": "Unknown / not in gallery",
                        "reason": "no_observation",
                    },
                    processor="pandect.closed-gallery",
                ),
            )
        )
    references = list(
        SubjectReference.objects.select_related(
            "observation",
            "observation__catalog_item",
        ).all()
    )
    reference_vectors = current_reference_vectors(references)
    results = []
    candidate_specs = []
    for observation in observations:
        ranked = rank_closed_gallery(observation_vector(observation), reference_vectors)
        candidate_set_id = uuid4()
        values = []
        for candidate in ranked.candidates:
            values.append(
                {
                    "subject_id": str(candidate.subject_id),
                    "rank": candidate.rank,
                    "similarity": candidate.similarity_score,
                    "reference_count": candidate.reference_count,
                }
            )
            candidate_specs.append(
                IdentityCandidateSpec(
                    observation_id=observation.pk,
                    subject_id=candidate.subject_id,
                    candidate_set_id=candidate_set_id,
                    rank=candidate.rank,
                    similarity_score=candidate.similarity_score,
                    gallery_fingerprint=ranked.gallery_fingerprint,
                    reference_count=candidate.reference_count,
                    calibration_revision="uncalibrated-cosine-v1",
                )
            )
        results.append(
            {
                "observation_id": str(observation.pk),
                "candidates": values,
                "unknown": True,
                "unknown_label": ranked.unknown.label,
                "reason": ranked.reason,
                "gallery_fingerprint": ranked.gallery_fingerprint,
                "scores_are_probabilities": False,
            }
        )
    return StageOutcome(
        results=(
            _model_result(
                stage,
                result_kind,
                {"observations": results},
                processor="pandect.closed-gallery",
            ),
        ),
        identity_candidates=tuple(candidate_specs),
    )


def _face_identity(stage: ProcessingStage) -> StageOutcome:
    return _identity_candidates(stage, IdentityObservation.Modality.FACE)


def _voice_identity(stage: ProcessingStage) -> StageOutcome:
    return _identity_candidates(stage, IdentityObservation.Modality.VOICE)


_PROCESSORS: dict[str, Callable[[ProcessingStage], StageOutcome]] = {
    ProcessingStage.Kind.TEXT_EXTRACTION: _message_profile,
    ProcessingStage.Kind.WRITING_STYLE: _writing_style,
    ProcessingStage.Kind.WRITING_IDENTITY: _writing_identity,
    ProcessingStage.Kind.IMAGE_METADATA: _image_metadata,
    ProcessingStage.Kind.IMAGE_OCR: _image_ocr,
    ProcessingStage.Kind.IMAGE_QR: _image_qr,
    ProcessingStage.Kind.IMAGE_VISUAL: _image_visual,
    ProcessingStage.Kind.FACE_ANALYSIS: _face_analysis,
    ProcessingStage.Kind.FACE_IDENTITY: _face_identity,
    ProcessingStage.Kind.VIDEO_METADATA: _video_metadata,
    ProcessingStage.Kind.VIDEO_SUBTITLES: _video_subtitles,
    ProcessingStage.Kind.VIDEO_KEYFRAMES: _video_keyframes,
    ProcessingStage.Kind.VIDEO_AUDIO: _video_audio,
    ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS: _video_frame_analysis,
    ProcessingStage.Kind.AUDIO_METADATA: _audio_metadata,
    ProcessingStage.Kind.AUDIO_SPLIT: _audio_split,
    ProcessingStage.Kind.AUDIO_ACOUSTICS: _audio_acoustics,
    ProcessingStage.Kind.VOICE_ANALYSIS: _voice_analysis,
    ProcessingStage.Kind.VOICE_IDENTITY: _voice_identity,
}


def execute_stage(stage: ProcessingStage) -> StageOutcome:
    """Execute one explicitly wired local stage without hidden network access."""
    try:
        processor = _PROCESSORS[stage.kind]
    except KeyError as error:
        raise StageProcessingError(
            "processor_unavailable",
            "The local processor for this stage is unavailable.",
        ) from error
    return processor(stage)
