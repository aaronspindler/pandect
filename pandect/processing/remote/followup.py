"""Separately consented model analysis of durable remote aggregate results."""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from django.db import transaction

from pandect.catalog.models import CatalogItem
from pandect.processing.extractors.hashing import sha256_bytes
from pandect.processing.models import (
    ProcessingResult,
    ProcessingRun,
    ProcessingStage,
    RemoteConsent,
)
from pandect.processing.remote import cloudflare as cloudflare_adapter
from pandect.processing.remote.aggregation import verify_video_timeline_bindings
from pandect.processing.remote.cloudflare import (
    ALLOWED_TEXT_MODELS,
    CloudflareAIError,
    CloudflareConfig,
    CloudflareGateway,
)
from pandect.processing.remote.consent import (
    ConsentManifest,
    build_adapter_privacy_contract,
    build_manifest,
)
from pandect.processing.remote.schemas import (
    TRANSCRIPT_ANALYSIS_SCHEMA,
    VIDEO_ACCOUNT_SCHEMA,
    validate_source_spans,
)
from pandect.processing.remote.timing import (
    TranscriptTimingError,
    validate_followup_transcript_references,
)
from pandect.processing.remote.workflows import (
    REMOTE_PROCESSOR_VERSION,
    RemoteConsentInvalidatedError,
    RemoteConsentRequiredError,
    RemotePlanError,
    RemoteResponseValidationError,
    RemoteSourceChangedError,
    _remote_metadata,
    _single_consent,
    _validated_model_object,
    enqueue_remote_run_on_commit,
)
from pandect.processing.services import (
    ResultSpec,
    SourceUnavailableError,
    StageOutcome,
    source_fingerprint,
)

FOLLOWUP_WORKFLOW = "cloudflare-followup-consented-v1"
FOLLOWUP_NOTICE_VERSION = "followup-2"
FOLLOWUP_PROCESSOR_NAME = "pandect.cloudflare.followup"

TRANSCRIPT_SEMANTIC_OPERATION = "transcript_semantic"
VIDEO_SYNTHESIS_OPERATION = "video_synthesis"

FOLLOWUP_PROMPT_VERSIONS = {
    TRANSCRIPT_SEMANTIC_OPERATION: "structured-transcript-analysis-v2",
    VIDEO_SYNTHESIS_OPERATION: "video-account-synthesis-v1",
}
MAX_FOLLOWUP_PAYLOAD_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class FollowupPlan:
    """Exact aggregate-result transfer plan that does not itself authorize I/O."""

    item_id: UUID
    item_type: str
    source_sha256: str
    source_size: int
    aggregate_run_id: UUID
    operations: tuple[str, ...]
    payloads: tuple[dict[str, Any], ...]
    model_ids: dict[str, str]
    prompt_versions: dict[str, str]
    prompt_hashes: dict[str, str]
    recipients: tuple[str, ...]
    destination: dict[str, str]
    input_result_hashes: dict[str, str]
    manifest: ConsentManifest
    _contents: tuple[tuple[str, bytes], ...] = field(repr=False)

    @property
    def total_payload_bytes(self) -> int:
        return sum(int(payload["byte_size"]) for payload in self.payloads)

    @property
    def input_result_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.input_result_hashes))

    @property
    def adapter_privacy_contract(self) -> dict[str, Any]:
        value = self.manifest.value.get("adapter_privacy_contract", {})
        return value if isinstance(value, dict) else {}

    def content_for(self, payload_id: str) -> bytes:
        matches = [value for key, value in self._contents if key == payload_id]
        if len(matches) != 1:
            raise RemoteConsentInvalidatedError(
                "The consented aggregate input is no longer available."
            )
        return matches[0]


@dataclass(frozen=True)
class ValidatedFollowupTransfer:
    consent: RemoteConsent
    plan: FollowupPlan
    payload: dict[str, Any]
    content: bytes


def _catalog_item(item_or_id: CatalogItem | UUID | str) -> CatalogItem:
    item_id = item_or_id.pk if isinstance(item_or_id, CatalogItem) else item_or_id
    return CatalogItem.objects.get(pk=item_id)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RemotePlanError(
            "A durable aggregate result cannot be serialized safely."
        ) from error


def _result_snapshot(result: ProcessingResult) -> dict[str, Any]:
    return {
        "result_id": str(result.pk),
        "result_kind": result.result_kind,
        "schema_version": result.schema_version,
        "text": result.text,
        "data": result.data,
        "input_hashes": result.input_hashes,
        "provenance": result.provenance,
        "processor_name": result.processor_name,
        "processor_version": result.processor_version,
        "prompt_template_version": result.prompt_template_version,
    }


def _result_descriptor(result: ProcessingResult) -> dict[str, str]:
    snapshot_sha256 = sha256_bytes(_canonical_json_bytes(_result_snapshot(result)))
    return {
        "result_id": str(result.pk),
        "result_kind": result.result_kind,
        "result_sha256": snapshot_sha256,
    }


def _structured_transcript_input(
    result: ProcessingResult,
    descriptor: dict[str, str],
) -> dict[str, Any]:
    data = result.data if isinstance(result.data, dict) else {}
    streams = data.get("streams")
    if data.get("text") != result.text or not isinstance(streams, list) or not streams:
        raise RemotePlanError(
            "The current merged transcript has no structured timing data."
        )
    if any(
        not isinstance(stream, dict)
        or not isinstance(stream.get("words"), list)
        or not isinstance(stream.get("utterances"), list)
        for stream in streams
    ):
        raise RemotePlanError(
            "The current merged transcript has invalid structured timing data."
        )
    timed_records = [
        record
        for stream in streams
        for key in ("words", "utterances")
        for record in stream[key]
    ]
    required = {"start", "end", "confidence", "chunk_speaker"}
    if not timed_records or any(
        not isinstance(record, dict)
        or not required <= record.keys()
        or not isinstance(record["chunk_speaker"], int)
        or isinstance(record["chunk_speaker"], bool)
        or not isinstance(record["confidence"], (int, float))
        or isinstance(record["confidence"], bool)
        or not isinstance(record["start"], (int, float))
        or isinstance(record["start"], bool)
        or not isinstance(record["end"], (int, float))
        or isinstance(record["end"], bool)
        or record["end"] < record["start"]
        for record in timed_records
    ):
        raise RemotePlanError(
            "The current merged transcript has incomplete timing or speaker data."
        )
    ambiguous_alternates = data.get("ambiguous_alternates", [])
    speaker_reconciliation = data.get("speaker_reconciliation", {})
    if not isinstance(ambiguous_alternates, list) or not isinstance(
        speaker_reconciliation, dict
    ):
        raise RemotePlanError(
            "The current merged transcript has invalid speaker reconciliation data."
        )
    return {
        "schema_version": "2",
        "input_results": [descriptor],
        "transcript": {
            "text": result.text,
            "streams": streams,
            "ambiguous_alternates": ambiguous_alternates,
            "speaker_reconciliation": speaker_reconciliation,
        },
    }


def _current_aggregate_stage(
    item: CatalogItem,
    *,
    source_sha256: str,
    source_size: int,
) -> ProcessingStage:
    stage = (
        ProcessingStage.objects.filter(
            run__catalog_item=item,
            run__scope=ProcessingRun.Scope.REMOTE,
            run__source_fingerprint=source_sha256,
            run__source_size=source_size,
            run__status__in=(
                ProcessingRun.Status.SUCCEEDED,
                ProcessingRun.Status.PARTIAL,
            ),
            run__configuration__workflow="cloudflare-consented-v1",
            kind=ProcessingStage.Kind.REMOTE_AGGREGATE,
            status=ProcessingStage.Status.SUCCEEDED,
        )
        .select_related("run")
        .order_by("-run__created_at", "-run_id", "-created_at", "-pk")
        .first()
    )
    if stage is None:
        raise RemotePlanError(
            "A current successful transcript or video aggregate is required first."
        )
    return stage


def _followup_prompt_hash(operation: str) -> str:
    if operation == TRANSCRIPT_SEMANTIC_OPERATION:
        value = {
            "system": cloudflare_adapter.SYSTEM_INSTRUCTION,
            "template": cloudflare_adapter.TRANSCRIPT_ANALYSIS_EVIDENCE_PROMPT,
            "schema": TRANSCRIPT_ANALYSIS_SCHEMA,
        }
    elif operation == VIDEO_SYNTHESIS_OPERATION:
        value = {
            "system": cloudflare_adapter.SYSTEM_INSTRUCTION,
            "template": cloudflare_adapter.VIDEO_SYNTHESIS_EVIDENCE_PROMPT,
            "schema": VIDEO_ACCOUNT_SCHEMA,
        }
    else:
        raise RemotePlanError("The requested follow-up operation is unavailable.")
    return sha256_bytes(_canonical_json_bytes(value))


def _recipients_for(model_id: str) -> tuple[str, ...]:
    recipients = {
        "Cloudflare AI Gateway",
        "Cloudflare Workers AI",
    }
    if "meta/llama" in model_id:
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
    operations: tuple[str, ...],
) -> dict[str, Any]:
    contracts: dict[str, dict[str, Any]] = {}
    for operation in operations:
        if operation not in {
            TRANSCRIPT_SEMANTIC_OPERATION,
            VIDEO_SYNTHESIS_OPERATION,
        }:
            raise RemotePlanError("The requested follow-up operation is unavailable.")
        contracts[operation] = {
            "endpoint": "/accounts/{account_id}/ai/run",
            "request_encoding": "json",
            # Unknown-outcome aggregate requests are never retried remotely.
            "gateway_attempt_limit": 1,
            "model_options": {
                "max_tokens": 4096,
                "response_format": "json_object",
                "temperature": 0,
            },
        }
    return build_adapter_privacy_contract(
        client_timeout_seconds=config.timeout_seconds,
        operation_contracts=contracts,
        gateway_request_timeout_seconds=(
            cloudflare_adapter._GATEWAY_REQUEST_TIMEOUT_SECONDS
        ),
        gateway_retry_delay_seconds=(cloudflare_adapter._GATEWAY_RETRY_DELAY_SECONDS),
        response_limit_bytes=cloudflare_adapter.MAX_GATEWAY_RESPONSE_BYTES,
    )


def _payload(
    *,
    operation: str,
    stage_kind: str,
    unit_key: str,
    transfer: str,
    content: bytes,
    input_results: list[dict[str, str]],
    aggregate_run_id: UUID,
    model_id: str,
    prompt_hash: str,
    mime_type: str,
    schema_version: str = "1",
) -> dict[str, Any]:
    return {
        "payload_id": operation,
        "operation": operation,
        "stage_kind": stage_kind,
        "unit_key": unit_key,
        "transfer": transfer,
        "sha256": sha256_bytes(content),
        "byte_size": len(content),
        "mime_type": mime_type,
        "aggregate_run_id": str(aggregate_run_id),
        "input_results": input_results,
        "model_id": model_id,
        "prompt_version": FOLLOWUP_PROMPT_VERSIONS[operation],
        "prompt_template_sha256": prompt_hash,
        "schema_version": schema_version,
    }


def build_followup_plan(
    item_or_id: CatalogItem | UUID | str,
    *,
    config: CloudflareConfig | None = None,
) -> FollowupPlan:
    """Build a second, separately reviewable plan from immutable aggregates."""
    item = _catalog_item(item_or_id)
    if item.item_type not in {
        CatalogItem.ItemType.AUDIO,
        CatalogItem.ItemType.VIDEO,
    }:
        raise RemotePlanError(
            "Transcript and video synthesis follow-up is unavailable for this item."
        )
    fingerprint = source_fingerprint(item)
    aggregate_stage = _current_aggregate_stage(
        item,
        source_sha256=fingerprint.sha256,
        source_size=fingerprint.size_bytes,
    )
    results = list(
        ProcessingResult.objects.filter(stage=aggregate_stage).order_by(
            "created_at", "pk"
        )
    )
    transcript = next(
        (
            result
            for result in reversed(results)
            if result.result_kind == ProcessingResult.Kind.TRANSCRIPT
            and result.text.strip()
        ),
        None,
    )
    video_account = next(
        (
            result
            for result in reversed(results)
            if result.result_kind == ProcessingResult.Kind.VIDEO_DESCRIPTION
            and isinstance(result.data, dict)
            and isinstance(result.data.get("entries"), list)
        ),
        None,
    )
    if transcript is None and video_account is None:
        raise RemotePlanError(
            "The current aggregate contains no follow-up input to transfer."
        )

    effective_config = config or CloudflareConfig.from_settings()
    model_id = effective_config.text_model
    if model_id not in ALLOWED_TEXT_MODELS:
        raise RemotePlanError("The configured follow-up model is unsupported.")

    payloads: list[dict[str, Any]] = []
    contents: list[tuple[str, bytes]] = []
    input_result_hashes: dict[str, str] = {}

    if transcript is not None:
        descriptor = _result_descriptor(transcript)
        input_result_hashes[descriptor["result_id"]] = descriptor["result_sha256"]
        content = _canonical_json_bytes(
            _structured_transcript_input(transcript, descriptor)
        )
        if len(content) > MAX_FOLLOWUP_PAYLOAD_BYTES:
            raise RemotePlanError(
                "The merged transcript exceeds the 16 MiB follow-up payload limit."
            )
        operation = TRANSCRIPT_SEMANTIC_OPERATION
        payloads.append(
            _payload(
                operation=operation,
                stage_kind=ProcessingStage.Kind.REMOTE_SEMANTIC,
                unit_key="aggregate-transcript",
                transfer="exact_structured_merged_transcript",
                content=content,
                input_results=[descriptor],
                aggregate_run_id=aggregate_stage.run_id,
                model_id=model_id,
                prompt_hash=_followup_prompt_hash(operation),
                mime_type="application/json; charset=utf-8",
                schema_version="2",
            )
        )
        contents.append((operation, content))

    if item.item_type == CatalogItem.ItemType.VIDEO and video_account is not None:
        if not verify_video_timeline_bindings(video_account):
            raise RemotePlanError(
                "The local evidence bound to the video timeline changed."
            )
        descriptors = [_result_descriptor(video_account)]
        if transcript is not None:
            descriptors.append(_result_descriptor(transcript))
        for descriptor in descriptors:
            input_result_hashes[descriptor["result_id"]] = descriptor["result_sha256"]
        exact_input = {
            "schema_version": "1",
            "input_results": descriptors,
            "timeline": video_account.data["entries"],
        }
        content = _canonical_json_bytes(exact_input)
        if len(content) > MAX_FOLLOWUP_PAYLOAD_BYTES:
            raise RemotePlanError(
                "The video timeline exceeds the 16 MiB follow-up payload limit."
            )
        operation = VIDEO_SYNTHESIS_OPERATION
        payloads.append(
            _payload(
                operation=operation,
                stage_kind=ProcessingStage.Kind.REMOTE_VIDEO_DESCRIPTION,
                unit_key="aggregate-video-timeline",
                transfer="exact_aggregate_video_timeline",
                content=content,
                input_results=descriptors,
                aggregate_run_id=aggregate_stage.run_id,
                model_id=model_id,
                prompt_hash=_followup_prompt_hash(operation),
                mime_type="application/json; charset=utf-8",
            )
        )
        contents.append((operation, content))

    if not payloads:
        raise RemotePlanError(
            "The current aggregate contains no follow-up input to transfer."
        )
    operations = tuple(payload["operation"] for payload in payloads)
    model_ids = {operation: model_id for operation in operations}
    prompt_versions = {
        operation: FOLLOWUP_PROMPT_VERSIONS[operation] for operation in operations
    }
    prompt_hashes = {
        operation: payload["prompt_template_sha256"]
        for operation, payload in zip(operations, payloads, strict=True)
    }
    recipients = _recipients_for(model_id)
    destination = _destination_for(effective_config)
    adapter_privacy_contract = _adapter_privacy_contract(
        effective_config,
        operations,
    )
    manifest = build_manifest(
        item_id=str(item.pk),
        item_type=item.item_type,
        source_sha256=fingerprint.sha256,
        source_size=fingerprint.size_bytes,
        payloads=payloads,
        models=[model_id],
        request_count=len(payloads),
        notice_version=FOLLOWUP_NOTICE_VERSION,
        adapter_privacy_contract=adapter_privacy_contract,
        prompt_versions=prompt_versions,
        prompt_hashes=prompt_hashes,
        recipients=list(recipients),
        destination=destination,
    )
    manifest.value["workflow"] = FOLLOWUP_WORKFLOW
    manifest.value["aggregate_run_id"] = str(aggregate_stage.run_id)
    manifest.value["input_result_hashes"] = dict(sorted(input_result_hashes.items()))
    manifest = ConsentManifest(
        manifest.value,
        sha256_bytes(_canonical_json_bytes(manifest.value)),
    )
    return FollowupPlan(
        item_id=item.pk,
        item_type=item.item_type,
        source_sha256=fingerprint.sha256,
        source_size=fingerprint.size_bytes,
        aggregate_run_id=aggregate_stage.run_id,
        operations=operations,
        payloads=tuple(payloads),
        model_ids=model_ids,
        prompt_versions=prompt_versions,
        prompt_hashes=prompt_hashes,
        recipients=recipients,
        destination=destination,
        input_result_hashes=dict(sorted(input_result_hashes.items())),
        manifest=manifest,
        _contents=tuple(contents),
    )


def create_consented_followup_run(
    item_or_id: CatalogItem | UUID | str,
    *,
    expected_manifest_hash: str,
    notice_version: str = FOLLOWUP_NOTICE_VERSION,
    trigger: str = ProcessingRun.Trigger.MANUAL,
    enqueue: bool = True,
) -> ProcessingRun:
    """Persist an immutable second consent and dispatch only after commit."""
    if notice_version != FOLLOWUP_NOTICE_VERSION:
        raise RemoteConsentInvalidatedError("The follow-up consent notice changed.")
    config = CloudflareConfig.from_settings()
    try:
        config.validate()
    except CloudflareAIError as error:
        raise RemotePlanError(str(error)) from error
    item = _catalog_item(item_or_id)
    plan = build_followup_plan(item, config=config)
    if not isinstance(expected_manifest_hash, str) or not hmac.compare_digest(
        expected_manifest_hash,
        plan.manifest.sha256,
    ):
        raise RemoteConsentInvalidatedError(
            "The follow-up transfer changed before consent was recorded."
        )
    configuration = {
        "workflow": FOLLOWUP_WORKFLOW,
        "notice_version": notice_version,
        "aggregate_run_id": str(plan.aggregate_run_id),
        "operations": list(plan.operations),
        "manifest_hash": plan.manifest.sha256,
        "input_result_hashes": plan.input_result_hashes,
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
                    processor_name=FOLLOWUP_PROCESSOR_NAME,
                    processor_version=REMOTE_PROCESSOR_VERSION,
                    schema_version=int(payload["schema_version"]),
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
        consent.save()
        if enqueue:
            enqueue_remote_run_on_commit(run.pk)
    return run


def validate_followup_consent(stage: ProcessingStage) -> ValidatedFollowupTransfer:
    """Recompute source, aggregate results, configuration, and exact bytes."""
    run = stage.run
    configuration = run.configuration if isinstance(run.configuration, dict) else {}
    if (
        run.scope != ProcessingRun.Scope.REMOTE
        or configuration.get("workflow") != FOLLOWUP_WORKFLOW
    ):
        raise RemoteConsentRequiredError(
            "This is not a separately consented follow-up stage."
        )
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
            "The canonical source changed after follow-up consent."
        )
    try:
        plan = build_followup_plan(run.catalog_item_id)
    except (RemotePlanError, SourceUnavailableError) as error:
        raise RemoteConsentInvalidatedError(
            "The aggregate follow-up plan no longer matches consent."
        ) from error
    checks = (
        consent.catalog_item_id == run.catalog_item_id,
        consent.notice_version == FOLLOWUP_NOTICE_VERSION,
        consent.source_fingerprint == plan.source_sha256,
        consent.payload_manifest == plan.manifest.value,
        hmac.compare_digest(consent.payload_manifest_hash, plan.manifest.sha256),
        hmac.compare_digest(
            str(configuration.get("manifest_hash", "")),
            plan.manifest.sha256,
        ),
        configuration.get("aggregate_run_id") == str(plan.aggregate_run_id),
        configuration.get("operations") == list(plan.operations),
        configuration.get("input_result_hashes") == plan.input_result_hashes,
        configuration.get("model_ids") == plan.model_ids,
        configuration.get("prompt_versions") == plan.prompt_versions,
        configuration.get("prompt_hashes") == plan.prompt_hashes,
        configuration.get("destination") == plan.destination,
        configuration.get("adapter_privacy_contract") == plan.adapter_privacy_contract,
        configuration.get("notice_version") == FOLLOWUP_NOTICE_VERSION,
        consent.recipients == list(plan.recipients),
        consent.model_ids == plan.model_ids,
        consent.prompt_versions == plan.prompt_versions,
        consent.planned_request_count == len(plan.payloads),
    )
    if not all(checks):
        raise RemoteConsentInvalidatedError(
            "The source, aggregate, model, prompt, or follow-up plan changed."
        )
    matching = [
        payload
        for payload in plan.payloads
        if payload["stage_kind"] == stage.kind and payload["unit_key"] == stage.unit_key
    ]
    if len(matching) != 1 or stage.max_attempts != 1:
        raise RemoteConsentInvalidatedError(
            "The follow-up stage no longer matches the consented request plan."
        )
    payload = matching[0]
    content = plan.content_for(payload["payload_id"])
    if len(content) != payload["byte_size"] or not hmac.compare_digest(
        sha256_bytes(content), payload["sha256"]
    ):
        raise RemoteConsentInvalidatedError(
            "The aggregate payload bytes changed after follow-up consent."
        )
    return ValidatedFollowupTransfer(consent, plan, payload, content)


def _validate_gateway_execution_contract(
    plan: FollowupPlan,
    gateway: Any,
) -> None:
    config = getattr(gateway, "config", None)
    if not isinstance(config, CloudflareConfig):
        return
    current_models = {operation: config.text_model for operation in plan.operations}
    if (
        current_models != plan.model_ids
        or _destination_for(config) != plan.destination
        or _adapter_privacy_contract(config, plan.operations)
        != plan.adapter_privacy_contract
    ):
        raise RemoteConsentInvalidatedError(
            "The follow-up adapter or privacy policy changed after consent."
        )


def _validate_video_source_references(data: dict[str, Any], entry_count: int) -> None:
    records = [data["summary"]]
    for key in (
        "scene_timeline",
        "people",
        "objects",
        "actions",
        "dialogue_summary",
        "important_events",
        "claims",
        "uncertainties",
    ):
        records.extend(data[key])
    if any(
        index >= entry_count
        for record in records
        for index in record["source_entry_indexes"]
    ):
        raise RemoteResponseValidationError(
            "The video synthesis response cited an invalid source entry."
        )
    if any(
        record["start_seconds"] is not None
        and record["end_seconds"] is not None
        and record["end_seconds"] < record["start_seconds"]
        for record in data["scene_timeline"]
    ):
        raise RemoteResponseValidationError(
            "The video synthesis response returned an invalid time range."
        )


def _transcript_source_text(source: str) -> str:
    try:
        exact_input = json.loads(source)
        transcript = exact_input["transcript"]
        text = transcript["text"]
        streams = transcript["streams"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise RemoteConsentInvalidatedError(
            "The consented structured transcript is invalid."
        ) from error
    if not isinstance(text, str) or not isinstance(streams, list) or not streams:
        raise RemoteConsentInvalidatedError(
            "The consented structured transcript is invalid."
        )
    return text


def _validate_transcript_time_ranges(data: dict[str, Any]) -> None:
    for key in ("speaker_turn_summaries", "important_timestamps"):
        if any(record["end_seconds"] < record["start_seconds"] for record in data[key]):
            raise RemoteResponseValidationError(
                "The transcript analysis returned an invalid time range."
            )


def _followup_provenance(transfer: ValidatedFollowupTransfer) -> dict[str, Any]:
    return {
        "provider": "cloudflare_workers_ai",
        "model_id": transfer.payload["model_id"],
        "consent_id": str(transfer.consent.pk),
        "notice_version": transfer.consent.notice_version,
        "payload_id": transfer.payload["payload_id"],
        "transfer": transfer.payload["transfer"],
        "aggregate_run_id": str(transfer.plan.aggregate_run_id),
        "input_results": transfer.payload["input_results"],
    }


def execute_followup_stage(
    stage: ProcessingStage,
    *,
    gateway: CloudflareGateway | None = None,
) -> StageOutcome:
    """Execute one exact aggregate transfer after its second consent."""
    transfer = validate_followup_consent(stage)
    source = transfer.content.decode("utf-8")
    gateway = gateway or CloudflareGateway()
    _validate_gateway_execution_contract(transfer.plan, gateway)
    operation = transfer.payload["operation"]
    metadata = {
        "run_id": str(stage.run_id),
        "stage_id": str(stage.pk),
        "operation": operation,
    }
    common = {
        "evidence_tier": ProcessingResult.EvidenceTier.MODEL_GENERATED,
        "schema_version": int(transfer.payload["schema_version"]),
        "processor_name": FOLLOWUP_PROCESSOR_NAME,
        "processor_version": REMOTE_PROCESSOR_VERSION,
        "prompt_template_version": transfer.payload["prompt_version"],
        "input_hashes": {
            "source": transfer.plan.source_sha256,
            "payload": transfer.payload["sha256"],
            "aggregate_results": transfer.plan.input_result_hashes,
        },
        "provenance": _followup_provenance(transfer),
    }
    if operation == TRANSCRIPT_SEMANTIC_OPERATION:
        transcript_text = _transcript_source_text(source)
        try:
            structured_source = json.loads(source)
        except (json.JSONDecodeError, TypeError) as error:
            raise RemoteConsentInvalidatedError(
                "The consented structured transcript is invalid."
            ) from error
        response = gateway.analyze_followup_transcript(source, metadata=metadata)
        validate_followup_consent(stage)
        _validate_gateway_execution_contract(transfer.plan, gateway)
        data = _validated_model_object(
            response,
            schema=TRANSCRIPT_ANALYSIS_SCHEMA,
            response_name="merged transcript semantic analysis",
        )
        _validate_transcript_time_ranges(data)
        try:
            validate_followup_transcript_references(
                data,
                structured_source=structured_source,
            )
        except TranscriptTimingError as error:
            raise RemoteResponseValidationError(
                "The transcript analysis cited unsupported timing or speakers."
            ) from error
        common["remote_request_metadata"] = _remote_metadata(transfer, response)
        warnings = validate_source_spans(data, transcript_text)
        results = [
            ResultSpec(
                result_kind=ProcessingResult.Kind.SEMANTIC_ANALYSIS,
                text=data["summary"]["text"],
                data=data,
                uncertainty=(
                    "Model-generated analysis of the separately consented merged "
                    "transcript; it is not a truthfulness determination."
                ),
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
                        "model_result_kind": (ProcessingResult.Kind.SEMANTIC_ANALYSIS),
                        "consent_id": str(transfer.consent.pk),
                        "aggregate_run_id": str(transfer.plan.aggregate_run_id),
                    },
                )
            )
        return StageOutcome(results=tuple(results))

    if operation == VIDEO_SYNTHESIS_OPERATION:
        response = gateway.synthesize_video_account(source, metadata=metadata)
        validate_followup_consent(stage)
        _validate_gateway_execution_contract(transfer.plan, gateway)
        data = _validated_model_object(
            response,
            schema=VIDEO_ACCOUNT_SCHEMA,
            response_name="video synthesis",
        )
        try:
            exact_input = json.loads(source)
            entry_count = len(exact_input["timeline"])
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise RemoteConsentInvalidatedError(
                "The consented video timeline is invalid."
            ) from error
        _validate_video_source_references(data, entry_count)
        common["remote_request_metadata"] = _remote_metadata(transfer, response)
        return StageOutcome(
            results=(
                ResultSpec(
                    result_kind=ProcessingResult.Kind.VIDEO_DESCRIPTION,
                    text=data["summary"]["text"],
                    data=data,
                    uncertainty=(
                        "Model-generated synthesis of a separately consented exact "
                        "aggregate timeline; source references do not establish fact."
                    ),
                    **common,
                ),
            )
        )
    raise RemoteConsentInvalidatedError(
        "The consented follow-up operation is no longer supported."
    )
