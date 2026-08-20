"""Consent-bound planning and execution for optional remote processing."""

import hashlib
import hmac
import json
import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from jsonschema import ValidationError as JSONSchemaError
from jsonschema import validate

from pandect.catalog.models import CatalogItem
from pandect.processing.extractors.derived_media import MAX_REMOTE_KEYFRAMES
from pandect.processing.extractors.hashing import fingerprint_stream, sha256_bytes
from pandect.processing.models import (
    DerivedArtifact,
    ProcessingResult,
    ProcessingRun,
    ProcessingStage,
    RemoteConsent,
)
from pandect.processing.remote import cloudflare as cloudflare_adapter
from pandect.processing.remote.cloudflare import (
    MAX_TRANSCRIPTION_AUDIO_BYTES,
    MAX_VISION_IMAGE_BYTES,
    CloudflareConfig,
    CloudflareConfigurationError,
    CloudflareGateway,
    _unwrap_completed_result,
)
from pandect.processing.remote.consent import (
    ConsentManifest,
    build_adapter_privacy_contract,
    build_manifest,
)
from pandect.processing.remote.schemas import (
    SEMANTIC_SCHEMA,
    TRANSCRIPTION_SCHEMA,
    VISION_SCHEMA,
    validate_source_spans,
)
from pandect.processing.remote.timing import (
    TranscriptTimingError,
    validate_chunk_transcription_timing,
)
from pandect.processing.services import (
    ResultSpec,
    SourceUnavailableError,
    StageOutcome,
    recompute_run_status,
    source_fingerprint,
)

logger = logging.getLogger(__name__)

REMOTE_NOTICE_VERSION = "1"
REMOTE_PROCESSOR_NAME = "pandect.cloudflare"
REMOTE_PROCESSOR_VERSION = "1"

SEMANTIC_OPERATION = "semantic"
IMAGE_DESCRIPTION_OPERATION = "image_description"
TRANSCRIPTION_OPERATION = "transcription"

PROMPT_VERSIONS = {
    SEMANTIC_OPERATION: "semantic-schema-v1",
    IMAGE_DESCRIPTION_OPERATION: "vision-schema-v1",
    TRANSCRIPTION_OPERATION: "nova3-options-schema-v2",
}

_MESSAGE_ITEM_TYPES = frozenset(
    {
        CatalogItem.ItemType.TEXT_MESSAGE,
        CatalogItem.ItemType.IMAGE_MESSAGE,
        CatalogItem.ItemType.VIDEO_MESSAGE,
    }
)
_ARTIFACT_PARAMETER_KEYS = frozenset(
    {
        "channels",
        "chunk_index",
        "codec",
        "downmix",
        "end_reason",
        "hard_cut",
        "initial_chunk_index",
        "jpeg_quality",
        "metadata_stripped",
        "overlap_before_seconds",
        "overlap_seconds",
        "provider_payload_limit_bytes",
        "provider_split_applied",
        "provider_split_depth",
        "provider_split_path",
        "resampling",
        "sample_rate",
        "source_stream_index",
    }
)


class RemoteWorkflowError(Exception):
    """A failure safe to expose without including evidence content."""

    code = "remote_workflow_error"

    def __init__(self, public_message: str) -> None:
        self.public_message = public_message[:512]
        super().__init__(self.public_message)


class RemotePlanError(RemoteWorkflowError):
    code = "remote_plan_invalid"


class RemoteConsentRequiredError(RemoteWorkflowError):
    code = "remote_consent_required"


class RemoteConsentInvalidatedError(RemoteWorkflowError):
    code = "remote_consent_invalidated"


class RemoteSourceChangedError(RemoteConsentInvalidatedError):
    code = "source_changed"


class RemoteResponseValidationError(RemoteWorkflowError):
    code = "remote_response_invalid"


@dataclass(frozen=True)
class RemotePlan:
    """A deterministic description of every byte transfer and remote request."""

    item_id: UUID
    item_type: str
    source_sha256: str
    source_size: int
    operations: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    payloads: tuple[dict[str, Any], ...]
    model_ids: dict[str, str]
    prompt_versions: dict[str, str]
    prompt_hashes: dict[str, str]
    recipients: tuple[str, ...]
    destination: dict[str, str]
    manifest: ConsentManifest

    @property
    def total_payload_bytes(self) -> int:
        return sum(int(payload["byte_size"]) for payload in self.payloads)

    @property
    def total_audio_duration_seconds(self) -> Decimal:
        total = Decimal(0)
        for payload in self.payloads:
            value = payload.get("duration_seconds")
            if value is None:
                continue
            try:
                total += Decimal(str(value))
            except InvalidOperation:
                continue
        return total

    @property
    def keyframe_count(self) -> int:
        return sum(
            payload.get("transfer") == "metadata_stripped_video_keyframe"
            for payload in self.payloads
        )

    @property
    def audio_chunk_count(self) -> int:
        return sum(
            payload.get("transfer") == "normalized_audio_chunk"
            for payload in self.payloads
        )

    @property
    def adapter_privacy_contract(self) -> dict[str, Any]:
        value = self.manifest.value.get("adapter_privacy_contract", {})
        return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class ValidatedRemoteTransfer:
    """A stage, consent, and one exact payload proven to still agree."""

    consent: RemoteConsent
    plan: RemotePlan
    payload: dict[str, Any]


def _catalog_item(item_or_id: CatalogItem | UUID | str) -> CatalogItem:
    item_id = item_or_id.pk if isinstance(item_or_id, CatalogItem) else item_or_id
    return CatalogItem.objects.get(pk=item_id)


def _models_for(config: CloudflareConfig) -> dict[str, str]:
    models = {
        SEMANTIC_OPERATION: config.text_model,
        IMAGE_DESCRIPTION_OPERATION: config.vision_model,
        TRANSCRIPTION_OPERATION: config.transcription_model,
    }
    try:
        config.validate_model_roles()
    except CloudflareConfigurationError as error:
        raise RemotePlanError("A configured remote model is unsupported.") from error

    return models


def _prompt_template_hash(operation: str) -> str:
    if operation == SEMANTIC_OPERATION:
        value = {
            "system": cloudflare_adapter.SYSTEM_INSTRUCTION,
            "template": cloudflare_adapter.SEMANTIC_EVIDENCE_PROMPT,
            "schema": cloudflare_adapter.SEMANTIC_SCHEMA,
        }
    elif operation == IMAGE_DESCRIPTION_OPERATION:
        value = {
            "system": cloudflare_adapter.SYSTEM_INSTRUCTION,
            "template": cloudflare_adapter.VISION_EVIDENCE_PROMPT,
            "schema": cloudflare_adapter.VISION_SCHEMA,
        }
    elif operation == TRANSCRIPTION_OPERATION:
        value = {
            "parameters": cloudflare_adapter.TRANSCRIPTION_PARAMETERS,
            "schema": cloudflare_adapter.TRANSCRIPTION_SCHEMA,
        }
    else:
        raise RemotePlanError("The requested remote operation is not available.")
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _recipients_for(models: Iterable[str]) -> tuple[str, ...]:
    recipients = {
        "Cloudflare AI Gateway",
        "Cloudflare Workers AI",
    }
    selected = set(models)
    if any("deepgram" in model for model in selected):
        recipients.add("Deepgram Nova-3 model service")
    if any("meta/llama" in model for model in selected):
        recipients.add("Meta Llama model via Cloudflare Workers AI")
    return tuple(sorted(recipients))


def _destination_for(config: CloudflareConfig) -> dict[str, str]:
    return {
        "cloudflare_account_id": config.account_id,
        "cloudflare_gateway_id": config.gateway_id,
        "adapter_policy": "cloudflare-direct-http-v1",
    }


def _adapter_privacy_contract(
    config: CloudflareConfig,
    operations: Iterable[str],
) -> dict[str, Any]:
    """Bind each initial operation to its actual HTTP and privacy behaviour."""
    contracts: dict[str, dict[str, Any]] = {}
    for operation in operations:
        if operation == SEMANTIC_OPERATION:
            contracts[operation] = {
                "endpoint": "/accounts/{account_id}/ai/run",
                "request_encoding": "json",
                "gateway_attempt_limit": 2,
                "model_options": {
                    "max_tokens": 4096,
                    "response_format": "json_object",
                    "temperature": 0,
                },
            }
        elif operation == IMAGE_DESCRIPTION_OPERATION:
            contracts[operation] = {
                "endpoint": "/accounts/{account_id}/ai/run",
                "request_encoding": "json_integer_array_exact_image_bytes",
                "gateway_attempt_limit": 2,
                "model_options": {
                    "max_tokens": 4096,
                    "response_format": "json_object",
                    "temperature": 0,
                },
            }
        elif operation == TRANSCRIPTION_OPERATION:
            contracts[operation] = {
                "endpoint": "/accounts/{account_id}/ai/run/{model_id}",
                "request_encoding": "raw_selected_audio_bytes",
                "gateway_attempt_limit": 2,
                "provider_parameters": dict(
                    sorted(cloudflare_adapter.TRANSCRIPTION_PARAMETERS.items())
                ),
            }
        else:
            raise RemotePlanError("The requested remote operation is not available.")
    return build_adapter_privacy_contract(
        client_timeout_seconds=config.timeout_seconds,
        operation_contracts=contracts,
        gateway_request_timeout_seconds=(
            cloudflare_adapter._GATEWAY_REQUEST_TIMEOUT_SECONDS
        ),
        gateway_retry_delay_seconds=(cloudflare_adapter._GATEWAY_RETRY_DELAY_SECONDS),
        response_limit_bytes=cloudflare_adapter.MAX_GATEWAY_RESPONSE_BYTES,
    )


def _default_operations(item: CatalogItem) -> tuple[str, ...]:
    if item.item_type in _MESSAGE_ITEM_TYPES:
        return (SEMANTIC_OPERATION,)
    if item.item_type == CatalogItem.ItemType.IMAGE:
        return (IMAGE_DESCRIPTION_OPERATION,)
    if item.item_type == CatalogItem.ItemType.AUDIO:
        return (IMAGE_DESCRIPTION_OPERATION, TRANSCRIPTION_OPERATION)
    if item.item_type == CatalogItem.ItemType.VIDEO:
        return (IMAGE_DESCRIPTION_OPERATION, TRANSCRIPTION_OPERATION)
    raise RemotePlanError("This item type has no remote processing workflow.")


def _validated_operations(
    item: CatalogItem,
    operations: Iterable[str] | None,
) -> tuple[str, ...]:
    defaults = _default_operations(item)
    requested = tuple(dict.fromkeys(operations or defaults))
    if not requested or any(operation not in defaults for operation in requested):
        raise RemotePlanError("The requested remote operation is not available.")
    return requested


def _artifact_transfer_parameters(artifact: DerivedArtifact) -> dict[str, Any]:
    if not isinstance(artifact.metadata, dict):
        return {}
    return {
        key: artifact.metadata[key]
        for key in sorted(_ARTIFACT_PARAMETER_KEYS)
        if key in artifact.metadata
    }


def _artifact_payloads(
    item: CatalogItem,
    artifact_ids: Iterable[UUID | str] | None,
    *,
    source_sha256: str,
    source_size: int,
    model_id: str,
    prompt_template_sha256: str,
    auto_select: bool = True,
    allow_empty: bool = False,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    queryset = DerivedArtifact.objects.filter(
        catalog_item=item,
        role=DerivedArtifact.Role.AUDIO_CHUNK,
        stage__run__scope=ProcessingRun.Scope.LOCAL,
        stage__run__source_fingerprint=source_sha256,
        stage__run__source_size=source_size,
    )
    try:
        requested_ids = tuple(
            sorted({str(UUID(str(value))) for value in artifact_ids or ()})
        )
    except (TypeError, ValueError, AttributeError) as error:
        raise RemotePlanError(
            "A selected audio chunk identifier is invalid."
        ) from error
    if requested_ids:
        queryset = queryset.filter(pk__in=requested_ids)
    elif auto_select:
        newest_run_id = (
            queryset.order_by("-stage__run__created_at", "-stage__run_id")
            .values_list("stage__run_id", flat=True)
            .first()
        )
        if newest_run_id is not None:
            queryset = queryset.filter(stage__run_id=newest_run_id)
    else:
        queryset = queryset.none()
    artifacts = list(
        queryset.select_related("stage__run").order_by(
            "source_start_seconds",
            "created_at",
            "pk",
        )
    )
    if not artifacts and not allow_empty:
        raise RemotePlanError("No normalized audio chunks are available to transfer.")
    actual_ids = {str(artifact.pk) for artifact in artifacts}
    if requested_ids and actual_ids != set(requested_ids):
        raise RemotePlanError("A selected audio chunk is unavailable.")

    payloads: list[dict[str, Any]] = []
    for artifact in artifacts:
        if artifact.byte_size > MAX_TRANSCRIPTION_AUDIO_BYTES:
            raise RemotePlanError(
                "A selected normalized audio chunk exceeds this adapter's 25 MiB "
                "payload limit. Run local processing again to split it before "
                "consenting."
            )
        try:
            with artifact.file.open("rb") as stream:
                fingerprint = fingerprint_stream(stream, preserve_position=False)
        except (FileNotFoundError, OSError, ValueError) as error:
            raise RemotePlanError("A selected audio chunk is unreadable.") from error
        if (
            fingerprint.sha256 != artifact.sha256
            or fingerprint.size_bytes != artifact.byte_size
        ):
            raise RemotePlanError(
                "A selected audio chunk no longer matches its record."
            )
        duration_seconds = None
        if (
            artifact.source_start_seconds is not None
            and artifact.source_end_seconds is not None
        ):
            duration_seconds = str(
                artifact.source_end_seconds - artifact.source_start_seconds
            )
        payloads.append(
            {
                "payload_id": str(artifact.pk),
                "operation": TRANSCRIPTION_OPERATION,
                "stage_kind": ProcessingStage.Kind.REMOTE_TRANSCRIPTION,
                "unit_key": f"artifact:{artifact.pk}",
                "transfer": "normalized_audio_chunk",
                "artifact_id": str(artifact.pk),
                "derivative_run_id": str(artifact.stage.run_id),
                "derivative_stage_id": str(artifact.stage_id),
                "sha256": artifact.sha256,
                "byte_size": artifact.byte_size,
                "payload_limit_bytes": MAX_TRANSCRIPTION_AUDIO_BYTES,
                "mime_type": artifact.mime_type or "application/octet-stream",
                "source_start_seconds": (
                    str(artifact.source_start_seconds)
                    if artifact.source_start_seconds is not None
                    else None
                ),
                "source_end_seconds": (
                    str(artifact.source_end_seconds)
                    if artifact.source_end_seconds is not None
                    else None
                ),
                "duration_seconds": duration_seconds,
                "stream_index": artifact.stream_index,
                "transfer_parameters": _artifact_transfer_parameters(artifact),
                "model_id": model_id,
                "prompt_version": PROMPT_VERSIONS[TRANSCRIPTION_OPERATION],
                "prompt_template_sha256": prompt_template_sha256,
            }
        )
    return tuple(payloads), tuple(str(artifact.pk) for artifact in artifacts)


def _evenly_spaced_artifacts(
    artifacts: list[DerivedArtifact],
    *,
    limit: int,
) -> list[DerivedArtifact]:
    """Retain first/last coverage when a local run produced many keyframes."""
    if len(artifacts) <= limit:
        return artifacts
    indexes = {
        round(position * (len(artifacts) - 1) / (limit - 1))
        for position in range(limit)
    }
    return [artifacts[index] for index in sorted(indexes)]


def _keyframe_payloads(
    item: CatalogItem,
    artifact_ids: Iterable[UUID | str] | None,
    *,
    source_sha256: str,
    source_size: int,
    model_id: str,
    prompt_template_sha256: str,
    auto_select: bool = True,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    """Plan exact metadata-stripped video-frame transfers, never the video."""
    queryset = DerivedArtifact.objects.filter(
        catalog_item=item,
        role=DerivedArtifact.Role.KEYFRAME,
        stage__kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
        stage__run__scope=ProcessingRun.Scope.LOCAL,
        stage__run__source_fingerprint=source_sha256,
        stage__run__source_size=source_size,
    )
    try:
        requested_ids = tuple(
            sorted({str(UUID(str(value))) for value in artifact_ids or ()})
        )
    except (TypeError, ValueError, AttributeError) as error:
        raise RemotePlanError(
            "A selected video keyframe identifier is invalid."
        ) from error
    if len(requested_ids) > MAX_REMOTE_KEYFRAMES:
        raise RemotePlanError(
            f"At most {MAX_REMOTE_KEYFRAMES} video keyframes may be transferred."
        )
    if requested_ids:
        queryset = queryset.filter(pk__in=requested_ids)
    elif auto_select:
        newest_run_id = (
            queryset.order_by("-stage__run__created_at", "-stage__run_id")
            .values_list("stage__run_id", flat=True)
            .first()
        )
        if newest_run_id is not None:
            queryset = queryset.filter(stage__run_id=newest_run_id)
    else:
        queryset = queryset.none()
    artifacts = list(
        queryset.select_related("stage__run").order_by(
            "source_start_seconds",
            "frame_index",
            "created_at",
            "pk",
        )
    )
    actual_ids = {str(artifact.pk) for artifact in artifacts}
    if requested_ids and actual_ids != set(requested_ids):
        raise RemotePlanError("A selected video keyframe is unavailable.")
    if not requested_ids:
        artifacts = _evenly_spaced_artifacts(
            artifacts,
            limit=MAX_REMOTE_KEYFRAMES,
        )
    if not artifacts:
        raise RemotePlanError(
            "No metadata-stripped representative video keyframes are available."
        )

    payloads: list[dict[str, Any]] = []
    for artifact in artifacts:
        metadata = artifact.metadata if isinstance(artifact.metadata, dict) else {}
        if metadata.get("metadata_stripped") is not True:
            raise RemotePlanError(
                "A selected video keyframe is not recorded as metadata-stripped."
            )
        try:
            with artifact.file.open("rb") as stream:
                fingerprint = fingerprint_stream(stream, preserve_position=False)
        except (FileNotFoundError, OSError, ValueError) as error:
            raise RemotePlanError("A selected video keyframe is unreadable.") from error
        if (
            fingerprint.sha256 != artifact.sha256
            or fingerprint.size_bytes != artifact.byte_size
        ):
            raise RemotePlanError(
                "A selected video keyframe no longer matches its record."
            )
        payloads.append(
            {
                "payload_id": str(artifact.pk),
                "operation": IMAGE_DESCRIPTION_OPERATION,
                "stage_kind": ProcessingStage.Kind.REMOTE_IMAGE_DESCRIPTION,
                "unit_key": f"artifact:{artifact.pk}",
                "transfer": "metadata_stripped_video_keyframe",
                "artifact_id": str(artifact.pk),
                "derivative_run_id": str(artifact.stage.run_id),
                "derivative_stage_id": str(artifact.stage_id),
                "sha256": artifact.sha256,
                "byte_size": artifact.byte_size,
                "mime_type": artifact.mime_type or "image/jpeg",
                "frame_index": artifact.frame_index,
                "source_start_seconds": (
                    str(artifact.source_start_seconds)
                    if artifact.source_start_seconds is not None
                    else None
                ),
                "source_end_seconds": (
                    str(artifact.source_end_seconds)
                    if artifact.source_end_seconds is not None
                    else None
                ),
                "transfer_parameters": _artifact_transfer_parameters(artifact),
                "model_id": model_id,
                "prompt_version": PROMPT_VERSIONS[IMAGE_DESCRIPTION_OPERATION],
                "prompt_template_sha256": prompt_template_sha256,
            }
        )
    return tuple(payloads), tuple(str(artifact.pk) for artifact in artifacts)


def _artwork_payloads(
    item: CatalogItem,
    artifact_ids: Iterable[UUID | str] | None,
    *,
    source_sha256: str,
    source_size: int,
    model_id: str,
    prompt_template_sha256: str,
    auto_select: bool = True,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    """Plan exact, operator-visible embedded-artwork transfers for audio."""
    queryset = DerivedArtifact.objects.filter(
        catalog_item=item,
        role=DerivedArtifact.Role.EMBEDDED_ARTWORK,
        stage__run__scope=ProcessingRun.Scope.LOCAL,
        stage__run__source_fingerprint=source_sha256,
        stage__run__source_size=source_size,
    )
    try:
        requested_ids = tuple(
            sorted({str(UUID(str(value))) for value in artifact_ids or ()})
        )
    except (TypeError, ValueError, AttributeError) as error:
        raise RemotePlanError(
            "A selected embedded-artwork identifier is invalid."
        ) from error
    if requested_ids:
        queryset = queryset.filter(pk__in=requested_ids)
    elif auto_select:
        newest_run_id = (
            queryset.order_by("-stage__run__created_at", "-stage__run_id")
            .values_list("stage__run_id", flat=True)
            .first()
        )
        if newest_run_id is not None:
            queryset = queryset.filter(stage__run_id=newest_run_id)
    else:
        queryset = queryset.none()
    artifacts = list(
        queryset.select_related("stage__run").order_by(
            "stream_index",
            "created_at",
            "pk",
        )
    )
    actual_ids = {str(artifact.pk) for artifact in artifacts}
    if requested_ids and actual_ids != set(requested_ids):
        raise RemotePlanError("A selected embedded artwork is unavailable.")

    payloads: list[dict[str, Any]] = []
    for artifact in artifacts:
        if artifact.byte_size > MAX_VISION_IMAGE_BYTES:
            raise RemotePlanError(
                "An exact embedded artwork derivative exceeds this adapter's "
                "10 MiB request limit. Pandect will not transform it without new "
                "consent."
            )
        try:
            with artifact.file.open("rb") as stream:
                fingerprint = fingerprint_stream(stream, preserve_position=False)
        except (FileNotFoundError, OSError, ValueError) as error:
            raise RemotePlanError(
                "A selected embedded artwork is unreadable."
            ) from error
        if (
            fingerprint.sha256 != artifact.sha256
            or fingerprint.size_bytes != artifact.byte_size
        ):
            raise RemotePlanError(
                "A selected embedded artwork no longer matches its record."
            )
        payloads.append(
            {
                "payload_id": str(artifact.pk),
                "operation": IMAGE_DESCRIPTION_OPERATION,
                "stage_kind": ProcessingStage.Kind.REMOTE_IMAGE_DESCRIPTION,
                "unit_key": f"artifact:{artifact.pk}",
                "transfer": "exact_embedded_artwork",
                "artifact_id": str(artifact.pk),
                "derivative_run_id": str(artifact.stage.run_id),
                "derivative_stage_id": str(artifact.stage_id),
                "sha256": artifact.sha256,
                "byte_size": artifact.byte_size,
                "mime_type": artifact.mime_type or "application/octet-stream",
                "stream_index": artifact.stream_index,
                "transfer_parameters": _artifact_transfer_parameters(artifact),
                "embedded_metadata_included": (
                    artifact.metadata.get("metadata_stripped") is not True
                    if isinstance(artifact.metadata, dict)
                    else True
                ),
                "model_id": model_id,
                "prompt_version": PROMPT_VERSIONS[IMAGE_DESCRIPTION_OPERATION],
                "prompt_template_sha256": prompt_template_sha256,
            }
        )
    return tuple(payloads), tuple(str(artifact.pk) for artifact in artifacts)


def _requested_artifacts_by_role(
    item: CatalogItem,
    artifact_ids: Iterable[UUID | str] | None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], bool]:
    try:
        requested_ids = tuple(
            sorted({str(UUID(str(value))) for value in artifact_ids or ()})
        )
    except (TypeError, ValueError, AttributeError) as error:
        raise RemotePlanError("A selected derivative identifier is invalid.") from error
    if not requested_ids:
        return (), (), (), False
    rows = DerivedArtifact.objects.filter(
        catalog_item=item,
        pk__in=requested_ids,
        stage__run__scope=ProcessingRun.Scope.LOCAL,
    ).values_list("pk", "role")
    roles = {str(pk): role for pk, role in rows}
    if set(roles) != set(requested_ids):
        raise RemotePlanError("A selected derivative is unavailable.")
    allowed_roles = {
        DerivedArtifact.Role.KEYFRAME,
        DerivedArtifact.Role.AUDIO_CHUNK,
        DerivedArtifact.Role.EMBEDDED_ARTWORK,
    }
    if not set(roles.values()) <= allowed_roles:
        raise RemotePlanError("A selected derivative cannot be transferred remotely.")
    keyframe_ids = tuple(
        artifact_id
        for artifact_id in requested_ids
        if roles[artifact_id] == DerivedArtifact.Role.KEYFRAME
    )
    audio_ids = tuple(
        artifact_id
        for artifact_id in requested_ids
        if roles[artifact_id] == DerivedArtifact.Role.AUDIO_CHUNK
    )
    artwork_ids = tuple(
        artifact_id
        for artifact_id in requested_ids
        if roles[artifact_id] == DerivedArtifact.Role.EMBEDDED_ARTWORK
    )
    return keyframe_ids, audio_ids, artwork_ids, True


def build_remote_plan(
    item_or_id: CatalogItem | UUID | str,
    *,
    operations: Iterable[str] | None = None,
    artifact_ids: Iterable[UUID | str] | None = None,
    config: CloudflareConfig | None = None,
) -> RemotePlan:
    """Build a local-only, hash-bound plan without authorizing any transfer."""
    item = _catalog_item(item_or_id)
    selected_operations = _validated_operations(item, operations)
    fingerprint = source_fingerprint(item)
    effective_config = config or CloudflareConfig.from_settings()
    model_ids = _models_for(effective_config)
    prompt_hashes = {
        operation: _prompt_template_hash(operation) for operation in selected_operations
    }
    planned_payloads: list[dict[str, Any]] = []
    selected_artifact_ids: list[str] = []
    (
        keyframe_ids,
        audio_ids,
        artwork_ids,
        has_explicit_selection,
    ) = _requested_artifacts_by_role(item, artifact_ids)
    if has_explicit_selection and item.item_type not in {
        CatalogItem.ItemType.AUDIO,
        CatalogItem.ItemType.VIDEO,
    }:
        raise RemotePlanError("This item does not accept derivative selection.")

    if selected_operations == (SEMANTIC_OPERATION,):
        planned_payloads.append(
            {
                "payload_id": "message-body",
                "operation": SEMANTIC_OPERATION,
                "stage_kind": ProcessingStage.Kind.REMOTE_SEMANTIC,
                "unit_key": "body",
                "transfer": "exact_message_body",
                "sha256": fingerprint.sha256,
                "byte_size": fingerprint.size_bytes,
                "mime_type": "text/plain; charset=utf-8",
                "model_id": model_ids[SEMANTIC_OPERATION],
                "prompt_version": PROMPT_VERSIONS[SEMANTIC_OPERATION],
                "prompt_template_sha256": prompt_hashes[SEMANTIC_OPERATION],
            }
        )
    elif item.item_type == CatalogItem.ItemType.IMAGE:
        try:
            declared_mime_type = item.image.mime_type
        except ObjectDoesNotExist as error:
            raise RemotePlanError("The original image is unavailable.") from error
        if fingerprint.size_bytes > MAX_VISION_IMAGE_BYTES:
            raise RemotePlanError(
                "The exact original image exceeds this adapter's 10 MiB request "
                "limit. Pandect will not transform it without new consent."
            )
        planned_payloads.append(
            {
                "payload_id": "original-image",
                "operation": IMAGE_DESCRIPTION_OPERATION,
                "stage_kind": ProcessingStage.Kind.REMOTE_IMAGE_DESCRIPTION,
                "unit_key": "original",
                "transfer": "exact_original_image",
                "exact_original": True,
                "embedded_metadata_included": True,
                "sha256": fingerprint.sha256,
                "byte_size": fingerprint.size_bytes,
                "mime_type": declared_mime_type or "application/octet-stream",
                "model_id": model_ids[IMAGE_DESCRIPTION_OPERATION],
                "prompt_version": PROMPT_VERSIONS[IMAGE_DESCRIPTION_OPERATION],
                "prompt_template_sha256": prompt_hashes[IMAGE_DESCRIPTION_OPERATION],
            }
        )
    else:
        if (
            item.item_type == CatalogItem.ItemType.VIDEO
            and IMAGE_DESCRIPTION_OPERATION in selected_operations
        ):
            keyframe_payloads, selected_keyframes = _keyframe_payloads(
                item,
                keyframe_ids,
                source_sha256=fingerprint.sha256,
                source_size=fingerprint.size_bytes,
                model_id=model_ids[IMAGE_DESCRIPTION_OPERATION],
                prompt_template_sha256=prompt_hashes[IMAGE_DESCRIPTION_OPERATION],
                auto_select=not has_explicit_selection,
            )
            planned_payloads.extend(keyframe_payloads)
            selected_artifact_ids.extend(selected_keyframes)
        if (
            item.item_type == CatalogItem.ItemType.AUDIO
            and IMAGE_DESCRIPTION_OPERATION in selected_operations
        ):
            artwork_payloads, selected_artwork = _artwork_payloads(
                item,
                artwork_ids,
                source_sha256=fingerprint.sha256,
                source_size=fingerprint.size_bytes,
                model_id=model_ids[IMAGE_DESCRIPTION_OPERATION],
                prompt_template_sha256=prompt_hashes[IMAGE_DESCRIPTION_OPERATION],
                auto_select=not has_explicit_selection,
            )
            planned_payloads.extend(artwork_payloads)
            selected_artifact_ids.extend(selected_artwork)
        if TRANSCRIPTION_OPERATION in selected_operations:
            allow_empty = (
                item.item_type
                in {
                    CatalogItem.ItemType.AUDIO,
                    CatalogItem.ItemType.VIDEO,
                }
                and IMAGE_DESCRIPTION_OPERATION in selected_operations
            )
            audio_payloads, selected_audio = _artifact_payloads(
                item,
                audio_ids if has_explicit_selection else None,
                source_sha256=fingerprint.sha256,
                source_size=fingerprint.size_bytes,
                model_id=model_ids[TRANSCRIPTION_OPERATION],
                prompt_template_sha256=prompt_hashes[TRANSCRIPTION_OPERATION],
                auto_select=not has_explicit_selection,
                allow_empty=allow_empty,
            )
            planned_payloads.extend(audio_payloads)
            selected_artifact_ids.extend(selected_audio)

    payloads = tuple(planned_payloads)
    if not payloads:
        raise RemotePlanError("No remote payloads are available for this item.")
    active_operations = tuple(
        operation
        for operation in selected_operations
        if any(payload["operation"] == operation for payload in payloads)
    )

    selected_model_ids = {
        operation: model_ids[operation] for operation in active_operations
    }
    prompt_versions = {
        operation: PROMPT_VERSIONS[operation] for operation in active_operations
    }
    prompt_hashes = {
        operation: prompt_hashes[operation] for operation in active_operations
    }
    recipients = _recipients_for(selected_model_ids.values())
    destination = _destination_for(effective_config)
    adapter_privacy_contract = _adapter_privacy_contract(
        effective_config,
        active_operations,
    )
    manifest = build_manifest(
        item_id=str(item.pk),
        item_type=item.item_type,
        source_sha256=fingerprint.sha256,
        source_size=fingerprint.size_bytes,
        payloads=list(payloads),
        models=sorted(set(selected_model_ids.values())),
        request_count=len(payloads),
        notice_version=REMOTE_NOTICE_VERSION,
        adapter_privacy_contract=adapter_privacy_contract,
        prompt_versions=prompt_versions,
        prompt_hashes=prompt_hashes,
        recipients=list(recipients),
        destination=destination,
    )
    return RemotePlan(
        item_id=item.pk,
        item_type=item.item_type,
        source_sha256=fingerprint.sha256,
        source_size=fingerprint.size_bytes,
        operations=active_operations,
        artifact_ids=tuple(selected_artifact_ids),
        payloads=payloads,
        model_ids=selected_model_ids,
        prompt_versions=prompt_versions,
        prompt_hashes=prompt_hashes,
        recipients=recipients,
        destination=destination,
        manifest=manifest,
    )


def create_consented_remote_run(
    item_or_id: CatalogItem | UUID | str,
    *,
    expected_manifest_hash: str,
    notice_version: str = REMOTE_NOTICE_VERSION,
    operations: Iterable[str] | None = None,
    artifact_ids: Iterable[UUID | str] | None = None,
    trigger: str = ProcessingRun.Trigger.MANUAL,
    enqueue: bool = True,
) -> ProcessingRun:
    """Persist exact consent and remote stages, dispatching only after commit."""
    if notice_version != REMOTE_NOTICE_VERSION:
        raise RemoteConsentInvalidatedError("The consent notice has changed.")
    config = CloudflareConfig.from_settings()
    config.validate()
    item = _catalog_item(item_or_id)
    plan = build_remote_plan(
        item,
        operations=operations,
        artifact_ids=artifact_ids,
        config=config,
    )
    if not isinstance(expected_manifest_hash, str) or not hmac.compare_digest(
        expected_manifest_hash,
        plan.manifest.sha256,
    ):
        raise RemoteConsentInvalidatedError(
            "The planned remote transfer changed before consent was recorded."
        )

    configuration = {
        "workflow": "cloudflare-consented-v1",
        "notice_version": notice_version,
        "operations": list(plan.operations),
        "artifact_ids": list(plan.artifact_ids),
        "manifest_hash": plan.manifest.sha256,
        "model_ids": plan.model_ids,
        "prompt_versions": plan.prompt_versions,
        "prompt_hashes": plan.prompt_hashes,
        "destination": plan.destination,
        "adapter_privacy_contract": plan.adapter_privacy_contract,
    }
    with transaction.atomic():
        run = ProcessingRun.objects.create(
            catalog_item=item,
            trigger=trigger,
            scope=ProcessingRun.Scope.REMOTE,
            source_fingerprint=plan.source_sha256,
            source_size=plan.source_size,
            processor_version=REMOTE_PROCESSOR_VERSION,
            configuration=configuration,
        )
        ProcessingStage.objects.bulk_create(
            [
                ProcessingStage(
                    run=run,
                    kind=payload["stage_kind"],
                    unit_key=payload["unit_key"],
                    max_attempts=1,
                    priority=index * 10,
                    processor_name=REMOTE_PROCESSOR_NAME,
                    processor_version=REMOTE_PROCESSOR_VERSION,
                    schema_version=1,
                )
                for index, payload in enumerate(plan.payloads, start=1)
            ]
        )
        consent = RemoteConsent(
            run=run,
            catalog_item=item,
            notice_version=notice_version,
            source_fingerprint=plan.source_sha256,
            payload_manifest=plan.manifest.value,
            payload_manifest_hash=plan.manifest.sha256,
            recipients=list(plan.recipients),
            model_ids=plan.model_ids,
            prompt_versions=plan.prompt_versions,
            planned_request_count=len(plan.payloads),
        )
        consent.full_clean()
        consent.save()
        if enqueue:
            enqueue_remote_run_on_commit(run.pk)
    return run


def enqueue_remote_run_on_commit(run_id: UUID | str) -> None:
    """Register a UUID-only enqueue callback for one consented remote run."""
    serialized_id = str(run_id)
    transaction.on_commit(lambda: _enqueue_remote_run_safely(serialized_id))


def _enqueue_remote_run_safely(run_id: UUID | str) -> None:
    """Keep committed consent recoverable when immediate dispatch is unavailable."""
    try:
        enqueue_remote_run(run_id)
    except Exception as error:  # noqa: BLE001 - reconciliation owns recovery.
        logger.error(
            "Remote enqueue callback failed: run_id=%s exception_type=%s",
            run_id,
            type(error).__name__,
        )


def _dispatch_remote_stage_task(stage_id: UUID | str):
    from pandect.processing.remote.tasks import run_remote_processing_stage

    return run_remote_processing_stage(str(stage_id))


def enqueue_remote_run(run_id: UUID | str) -> int:
    """Queue all remote units before dispatch so eager delivery stays isolated."""
    now = timezone.now()
    with transaction.atomic():
        stage_ids = list(
            ProcessingStage.objects.filter(
                run_id=run_id,
                run__scope=ProcessingRun.Scope.REMOTE,
                run__status__in=(
                    ProcessingRun.Status.PENDING,
                    ProcessingRun.Status.RUNNING,
                ),
                status=ProcessingStage.Status.PENDING,
                attempts__lt=F("max_attempts"),
            ).values_list("pk", flat=True)
        )
        if not stage_ids:
            return 0
        ProcessingStage.objects.filter(
            pk__in=stage_ids,
            status=ProcessingStage.Status.PENDING,
        ).update(
            status=ProcessingStage.Status.QUEUED,
            queued_at=now,
            task_id="",
            failure_code="",
            failure_detail="",
            finished_at=None,
            updated_at=now,
        )

    for stage_id in stage_ids:
        result = _dispatch_remote_stage_task(stage_id)
        task_id = str(getattr(result, "id", ""))[:64]
        if task_id:
            ProcessingStage.objects.filter(pk=stage_id).update(
                task_id=task_id,
                updated_at=timezone.now(),
            )
    recompute_run_status(run_id)
    return len(stage_ids)


def _single_consent(run: ProcessingRun) -> RemoteConsent:
    consents = list(RemoteConsent.objects.filter(run_id=run.pk)[:2])
    if not consents:
        raise RemoteConsentRequiredError(
            "Explicit consent is required before remote processing."
        )
    if len(consents) != 1:
        raise RemoteConsentInvalidatedError(
            "The run does not have one unambiguous consent record."
        )
    return consents[0]


def validate_remote_consent(stage: ProcessingStage) -> ValidatedRemoteTransfer:
    """Recompute every bound input before permitting a remote adapter call."""
    run = stage.run
    if run.scope != ProcessingRun.Scope.REMOTE:
        raise RemoteConsentRequiredError("This is not a consented remote stage.")
    consent = _single_consent(run)
    try:
        current_source = source_fingerprint(run.catalog_item_id)
    except SourceUnavailableError as error:
        raise RemoteSourceChangedError(
            "The canonical source is unavailable."
        ) from error
    if (
        current_source.sha256 != run.source_fingerprint
        or current_source.size_bytes != run.source_size
    ):
        raise RemoteSourceChangedError(
            "The canonical source changed after remote consent."
        )

    configuration = run.configuration if isinstance(run.configuration, dict) else {}
    try:
        plan = build_remote_plan(
            run.catalog_item_id,
            operations=configuration.get("operations"),
            artifact_ids=configuration.get("artifact_ids"),
        )
    except (RemotePlanError, SourceUnavailableError) as error:
        raise RemoteConsentInvalidatedError(
            "The remote payload plan no longer matches consent."
        ) from error

    checks = (
        consent.catalog_item_id == run.catalog_item_id,
        consent.notice_version == REMOTE_NOTICE_VERSION,
        consent.source_fingerprint == plan.source_sha256,
        consent.payload_manifest == plan.manifest.value,
        hmac.compare_digest(
            consent.payload_manifest_hash,
            plan.manifest.sha256,
        ),
        hmac.compare_digest(
            str(configuration.get("manifest_hash", "")),
            plan.manifest.sha256,
        ),
        consent.recipients == list(plan.recipients),
        consent.model_ids == plan.model_ids,
        consent.prompt_versions == plan.prompt_versions,
        consent.planned_request_count == len(plan.payloads),
        configuration.get("model_ids") == plan.model_ids,
        configuration.get("prompt_versions") == plan.prompt_versions,
        configuration.get("prompt_hashes") == plan.prompt_hashes,
        configuration.get("destination") == plan.destination,
        configuration.get("adapter_privacy_contract") == plan.adapter_privacy_contract,
        configuration.get("notice_version") == REMOTE_NOTICE_VERSION,
    )
    if not all(checks):
        raise RemoteConsentInvalidatedError(
            "The source, model, prompt, or payload plan changed after consent."
        )
    matching_payloads = [
        payload
        for payload in plan.payloads
        if payload["stage_kind"] == stage.kind and payload["unit_key"] == stage.unit_key
    ]
    if len(matching_payloads) != 1 or stage.max_attempts != 1:
        raise RemoteConsentInvalidatedError(
            "The remote stage no longer matches the consented request plan."
        )
    return ValidatedRemoteTransfer(consent, plan, matching_payloads[0])


def _validate_gateway_execution_contract(
    plan: RemotePlan,
    gateway: Any,
) -> None:
    """Prevent an injected real adapter from sending under a different policy."""
    config = getattr(gateway, "config", None)
    if not isinstance(config, CloudflareConfig):
        # Narrow fake adapters are used for hermetic processor tests. The real
        # adapter always exposes its immutable CloudflareConfig.
        return
    current_models = _models_for(config)
    selected_models = {
        operation: current_models[operation] for operation in plan.operations
    }
    if (
        selected_models != plan.model_ids
        or _destination_for(config) != plan.destination
        or _adapter_privacy_contract(config, plan.operations)
        != plan.adapter_privacy_contract
    ):
        raise RemoteConsentInvalidatedError(
            "The remote adapter or privacy policy changed after consent."
        )


def _read_exact_payload(
    stage: ProcessingStage,
    payload: dict[str, Any],
) -> tuple[bytes, str | None]:
    operation = payload["operation"]
    item = _catalog_item(stage.run.catalog_item_id)
    if operation == SEMANTIC_OPERATION:
        try:
            source_text = item.message.body
        except ObjectDoesNotExist as error:
            raise RemoteSourceChangedError(
                "The message source is unavailable."
            ) from error
        content = source_text.encode("utf-8")
    elif operation == IMAGE_DESCRIPTION_OPERATION:
        if payload.get("transfer") in {
            "metadata_stripped_video_keyframe",
            "exact_embedded_artwork",
        }:
            role = (
                DerivedArtifact.Role.KEYFRAME
                if payload["transfer"] == "metadata_stripped_video_keyframe"
                else DerivedArtifact.Role.EMBEDDED_ARTWORK
            )
            try:
                artifact = DerivedArtifact.objects.get(
                    pk=payload["artifact_id"],
                    catalog_item=item,
                    role=role,
                    stage__run__scope=ProcessingRun.Scope.LOCAL,
                )
                metadata = (
                    artifact.metadata if isinstance(artifact.metadata, dict) else {}
                )
                if (
                    role == DerivedArtifact.Role.KEYFRAME
                    and metadata.get("metadata_stripped") is not True
                ):
                    raise RemoteConsentInvalidatedError(
                        "A consented video keyframe is not metadata-stripped."
                    )
                with artifact.file.open("rb") as stream:
                    content = stream.read()
            except DerivedArtifact.DoesNotExist as error:
                raise RemoteConsentInvalidatedError(
                    "A consented image derivative is unavailable."
                ) from error
            except (FileNotFoundError, OSError, ValueError) as error:
                raise RemoteConsentInvalidatedError(
                    "A consented image derivative is unreadable."
                ) from error
        else:
            try:
                evidence_file = item.image.file
                with evidence_file.open("rb") as stream:
                    content = stream.read()
            except (
                ObjectDoesNotExist,
                FileNotFoundError,
                OSError,
                ValueError,
            ) as error:
                raise RemoteSourceChangedError(
                    "The original image is unavailable."
                ) from error
        source_text = None
    elif operation == TRANSCRIPTION_OPERATION:
        if int(payload.get("byte_size", 0)) > MAX_TRANSCRIPTION_AUDIO_BYTES:
            raise RemoteConsentInvalidatedError(
                "The consented audio chunk exceeds the current adapter payload "
                "limit. Run local processing again before consenting."
            )
        try:
            artifact = DerivedArtifact.objects.get(
                pk=payload["artifact_id"],
                catalog_item=item,
                role=DerivedArtifact.Role.AUDIO_CHUNK,
            )
            with artifact.file.open("rb") as stream:
                content = stream.read()
        except (
            DerivedArtifact.DoesNotExist,
            FileNotFoundError,
            OSError,
            ValueError,
        ) as error:
            raise RemoteConsentInvalidatedError(
                "A consented audio chunk is unavailable."
            ) from error
        source_text = None
    else:
        raise RemoteConsentInvalidatedError(
            "The consented operation is no longer supported."
        )

    if (
        len(content) != payload["byte_size"]
        or sha256_bytes(content) != payload["sha256"]
    ):
        raise RemoteConsentInvalidatedError(
            "The payload bytes changed after remote consent."
        )
    if (
        operation == TRANSCRIPTION_OPERATION
        and len(content) > MAX_TRANSCRIPTION_AUDIO_BYTES
    ):
        raise RemoteConsentInvalidatedError(
            "The consented audio chunk exceeds the current adapter payload limit."
        )
    return content, source_text


def _remote_metadata(
    transfer: ValidatedRemoteTransfer,
    response: Any | None = None,
) -> dict[str, Any]:
    operation_contract = transfer.plan.adapter_privacy_contract.get(
        "operations", {}
    ).get(transfer.payload["operation"], {})
    metadata: dict[str, Any] = {
        "provider": "Cloudflare Workers AI through AI Gateway",
        "model_id": transfer.payload["model_id"],
        "consent_id": str(transfer.consent.pk),
        "payload_manifest_hash": transfer.plan.manifest.sha256,
        "gateway_payload_logging": False,
        "gateway_cache": False,
        "gateway_attempt_limit": operation_contract.get("gateway_attempt_limit"),
        "adapter_privacy_contract_version": (
            transfer.plan.adapter_privacy_contract.get("version")
        ),
    }
    response_metadata = getattr(response, "request_metadata", None)
    if not isinstance(response_metadata, dict):
        return metadata
    http_status = response_metadata.get("http_status")
    if isinstance(http_status, int):
        metadata["http_status"] = http_status
    headers = response_metadata.get("response_headers")
    if isinstance(headers, dict):
        allowed_headers = {
            key: value[:256]
            for key, value in headers.items()
            if key in {"cf-ray", "cf-aig-request-id", "x-request-id"}
            and isinstance(value, str)
        }
        if allowed_headers:
            metadata["response_headers"] = allowed_headers
    gateway = response_metadata.get("gateway")
    if isinstance(gateway, dict):
        allowed_gateway = {
            key: value
            for key, value in gateway.items()
            if key
            in {
                "cacheStatus",
                "cost",
                "duration",
                "keySource",
                "model",
                "provider",
                "requestId",
                "status",
            }
            and isinstance(value, (str, int, float, bool))
        }
        if allowed_gateway:
            metadata["gateway"] = allowed_gateway
    return metadata


def _provenance(transfer: ValidatedRemoteTransfer) -> dict[str, Any]:
    provenance = {
        "provider": "cloudflare_workers_ai",
        "model_id": transfer.payload["model_id"],
        "consent_id": str(transfer.consent.pk),
        "notice_version": transfer.consent.notice_version,
        "payload_id": transfer.payload["payload_id"],
        "transfer": transfer.payload["transfer"],
    }
    for key in (
        "artifact_id",
        "derivative_run_id",
        "derivative_stage_id",
        "frame_index",
        "source_start_seconds",
        "source_end_seconds",
        "stream_index",
        "transfer_parameters",
    ):
        if transfer.payload.get(key) is not None:
            provenance[key] = transfer.payload[key]
    return provenance


def _validated_transcription(
    payload: dict[str, Any],
    *,
    duration_seconds: Any,
) -> dict[str, Any]:
    try:
        result = _unwrap_completed_result(payload)
    except cloudflare_adapter.CloudflareResponseError as error:
        raise RemoteResponseValidationError(
            "The transcription response failed validation."
        ) from error
    transcript = result.get("results", result)
    try:
        validate(instance=transcript, schema=TRANSCRIPTION_SCHEMA)
    except JSONSchemaError as error:
        raise RemoteResponseValidationError(
            "The transcription response failed validation."
        ) from error
    if not _valid_transcription_numbers(transcript):
        raise RemoteResponseValidationError(
            "The transcription response failed validation."
        )
    try:
        validate_chunk_transcription_timing(
            transcript,
            duration_seconds=duration_seconds,
        )
    except TranscriptTimingError as error:
        raise RemoteResponseValidationError(
            "The transcription response failed validation."
        ) from error
    return transcript


def _valid_transcription_numbers(transcript: dict[str, Any]) -> bool:
    def confidence(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and 0 <= value <= 1
        )

    def timed(record: dict[str, Any]) -> bool:
        start, end = record["start"], record["end"]
        return (
            isinstance(start, (int, float))
            and not isinstance(start, bool)
            and isinstance(end, (int, float))
            and not isinstance(end, bool)
            and math.isfinite(start)
            and math.isfinite(end)
            and 0 <= start <= end
            and confidence(record["confidence"])
        )

    for channel in transcript["channels"]:
        if not confidence(channel["language_confidence"]):
            return False
        for alternative in channel["alternatives"]:
            if not confidence(alternative["confidence"]):
                return False
            for word in alternative["words"]:
                if not timed(word) or (
                    "speaker_confidence" in word
                    and not confidence(word["speaker_confidence"])
                ):
                    return False
    for utterance in transcript["utterances"]:
        if not timed(utterance):
            return False
        for word in utterance["words"]:
            if not timed(word) or (
                "speaker_confidence" in word
                and not confidence(word["speaker_confidence"])
            ):
                return False
    return True


def _validated_model_object(
    value: Any,
    *,
    schema: dict[str, Any],
    response_name: str,
) -> dict[str, Any]:
    try:
        validate(instance=value, schema=schema)
    except JSONSchemaError as error:
        raise RemoteResponseValidationError(
            f"The {response_name} response failed validation."
        ) from error
    return value


def _primary_transcript(data: dict[str, Any]) -> tuple[str, float | None]:
    alternative = data["channels"][0]["alternatives"][0]
    confidence = alternative.get("confidence")
    return alternative["transcript"], confidence


def execute_remote_stage(
    stage: ProcessingStage,
    *,
    gateway: CloudflareGateway | None = None,
) -> StageOutcome:
    """Execute one stage only after locally proving its exact current consent."""
    transfer = validate_remote_consent(stage)
    content, source_text = _read_exact_payload(stage, transfer.payload)

    def assert_current_source() -> None:
        current_source = source_fingerprint(stage.run.catalog_item_id)
        if (
            current_source.sha256 != transfer.plan.source_sha256
            or current_source.size_bytes != transfer.plan.source_size
        ):
            raise RemoteSourceChangedError(
                "The canonical source changed after remote consent."
            )

    assert_current_source()

    gateway = gateway or CloudflareGateway()
    _validate_gateway_execution_contract(transfer.plan, gateway)
    operation = transfer.payload["operation"]
    task_metadata = {
        "run_id": str(stage.run_id),
        "stage_id": str(stage.pk),
        "operation": operation,
    }
    common = {
        "evidence_tier": ProcessingResult.EvidenceTier.MODEL_GENERATED,
        "processor_name": REMOTE_PROCESSOR_NAME,
        "processor_version": REMOTE_PROCESSOR_VERSION,
        "prompt_template_version": transfer.payload["prompt_version"],
        "input_hashes": {
            "source": transfer.plan.source_sha256,
            "payload": transfer.payload["sha256"],
        },
        "provenance": _provenance(transfer),
    }

    if operation == SEMANTIC_OPERATION:
        assert source_text is not None
        response = gateway.analyze_text(source_text, metadata=task_metadata)
        assert_current_source()
        validate_remote_consent(stage)
        _validate_gateway_execution_contract(transfer.plan, gateway)
        data = _validated_model_object(
            response,
            schema=SEMANTIC_SCHEMA,
            response_name="semantic analysis",
        )
        common["remote_request_metadata"] = _remote_metadata(transfer, response)
        warnings = validate_source_spans(data, source_text)
        results = [
            ResultSpec(
                result_kind=ProcessingResult.Kind.SEMANTIC_ANALYSIS,
                text=data["summary"],
                data=data,
                **common,
            )
        ]
        if warnings:
            results.append(
                ResultSpec(
                    result_kind=ProcessingResult.Kind.VALIDATION_WARNING,
                    evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
                    data={"warnings": warnings},
                    processor_name="pandect.remote.span-validator",
                    processor_version=REMOTE_PROCESSOR_VERSION,
                    prompt_template_version=transfer.payload["prompt_version"],
                    input_hashes=common["input_hashes"],
                    provenance={
                        "model_result_kind": ProcessingResult.Kind.SEMANTIC_ANALYSIS,
                        "consent_id": str(transfer.consent.pk),
                    },
                )
            )
        return StageOutcome(results=tuple(results))

    if operation == IMAGE_DESCRIPTION_OPERATION:
        response = gateway.describe_image(content, metadata=task_metadata)
        assert_current_source()
        validate_remote_consent(stage)
        _validate_gateway_execution_contract(transfer.plan, gateway)
        data = _validated_model_object(
            response,
            schema=VISION_SCHEMA,
            response_name="image description",
        )
        common["remote_request_metadata"] = _remote_metadata(transfer, response)
        uncertainty = ""
        if transfer.plan.item_type == CatalogItem.ItemType.VIDEO:
            uncertainty = (
                "This describes one consented representative keyframe only. "
                "No cross-frame or transcript synthesis was transferred or performed."
            )
        return StageOutcome(
            results=(
                ResultSpec(
                    result_kind=ProcessingResult.Kind.IMAGE_DESCRIPTION,
                    text=data["description"],
                    data=data,
                    uncertainty=uncertainty,
                    **common,
                ),
            )
        )

    if operation == TRANSCRIPTION_OPERATION:
        response = gateway.transcribe_audio(
            content,
            mime_type=transfer.payload["mime_type"],
            metadata=task_metadata,
        )
        assert_current_source()
        validate_remote_consent(stage)
        _validate_gateway_execution_contract(transfer.plan, gateway)
        common["remote_request_metadata"] = _remote_metadata(transfer, response)
        data = _validated_transcription(
            response,
            duration_seconds=transfer.payload["duration_seconds"],
        )
        text, confidence = _primary_transcript(data)
        uncertainty = ""
        if transfer.plan.item_type == CatalogItem.ItemType.VIDEO:
            uncertainty = (
                "This transcript covers one consented normalized audio chunk only. "
                "No cross-chunk or keyframe synthesis was transferred or performed."
            )
        return StageOutcome(
            results=(
                ResultSpec(
                    result_kind=ProcessingResult.Kind.TRANSCRIPT,
                    text=text,
                    data=data,
                    confidence=confidence,
                    uncertainty=uncertainty,
                    **common,
                ),
            )
        )

    logger.error(
        "Unsupported consented remote operation: stage_id=%s",
        stage.pk,
    )
    raise RemoteConsentInvalidatedError(
        "The consented operation is no longer supported."
    )
