from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from pandect.audio.models import Audio
from pandect.catalog.models import CatalogItem
from pandect.messages.models import Message, TextMessage
from pandect.photos.models import Image
from pandect.processing.extractors.hashing import sha256_bytes
from pandect.processing.models import (
    DerivedArtifact,
    ProcessingResult,
    ProcessingRun,
    ProcessingStage,
)
from pandect.processing.remote import cloudflare as cloudflare_adapter
from pandect.processing.remote import workflows as remote_workflows
from pandect.processing.remote.cloudflare import (
    MAX_VISION_IMAGE_BYTES,
    CloudflareConfig,
    CloudflareGateway,
)
from pandect.processing.remote.tasks import run_remote_processing_stage
from pandect.processing.remote.workflows import (
    IMAGE_DESCRIPTION_OPERATION,
    PROMPT_VERSIONS,
    SEMANTIC_OPERATION,
    TRANSCRIPTION_OPERATION,
    RemoteConsentInvalidatedError,
    RemotePlanError,
    RemoteResponseValidationError,
    RemoteSourceChangedError,
    build_remote_plan,
    create_consented_remote_run,
    execute_remote_stage,
)
from pandect.processing.services import enqueue_ready_stages, source_fingerprint
from pandect.videos.models import Video


@pytest.fixture(autouse=True)
def configured_remote(settings):
    settings.CLOUDFLARE_AI_ENABLED = True
    settings.CLOUDFLARE_ACCOUNT_ID = "account"
    settings.CLOUDFLARE_API_TOKEN = "test-token"
    settings.CLOUDFLARE_GATEWAY_ID = "pandect"
    settings.CLOUDFLARE_TEXT_MODEL = "@cf/meta/llama-3.2-11b-vision-instruct"
    settings.CLOUDFLARE_VISION_MODEL = "@cf/meta/llama-3.2-11b-vision-instruct"
    settings.CLOUDFLARE_TRANSCRIPTION_MODEL = "@cf/deepgram/nova-3"


def make_text_item(body="Meet at noon."):
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.TEXT_MESSAGE)
    message = Message.objects.create(catalog_item=item, body=body)
    TextMessage.objects.create(message=message)
    return item


def make_image_item(content=b"original-image-with-metadata"):
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.IMAGE)
    Image.objects.create(
        catalog_item=item,
        file=SimpleUploadedFile("evidence.jpg", content),
        mime_type="image/jpeg",
        original_filename="evidence.jpg",
    )
    return item


@pytest.mark.django_db
def test_web_can_review_and_record_exact_consent_without_worker_token(settings):
    item = make_text_item()
    worker_plan = build_remote_plan(item)
    settings.CLOUDFLARE_API_TOKEN = ""
    web_plan = build_remote_plan(item)
    assert web_plan.manifest.sha256 == worker_plan.manifest.sha256
    run = create_consented_remote_run(
        item,
        expected_manifest_hash=web_plan.manifest.sha256,
        enqueue=False,
    )
    assert run.stages.filter(kind=ProcessingStage.Kind.REMOTE_SEMANTIC).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("item_factory", "schema_name"),
    (
        (make_text_item, "SEMANTIC_SCHEMA"),
        (make_image_item, "VISION_SCHEMA"),
    ),
)
def test_initial_consent_is_invalidated_by_exact_adapter_schema_change(
    item_factory,
    schema_name,
):
    item = item_factory()
    plan = build_remote_plan(item)
    original_schema = getattr(cloudflare_adapter, schema_name)
    changed_schema = {**original_schema, "$comment": "consent-bound revision"}

    with patch.object(cloudflare_adapter, schema_name, changed_schema):
        assert (
            remote_workflows._prompt_template_hash(plan.operations[0])
            != plan.prompt_hashes[plan.operations[0]]
        )
        with pytest.raises(RemoteConsentInvalidatedError):
            create_consented_remote_run(
                item,
                expected_manifest_hash=plan.manifest.sha256,
                enqueue=False,
            )


@pytest.mark.django_db
def test_initial_manifest_binds_notice_and_operation_adapter_privacy(settings):
    item = make_text_item()
    plan = build_remote_plan(item)
    contract = plan.adapter_privacy_contract

    assert plan.manifest.value["notice_version"] == "1"
    assert contract["version"] == "cloudflare-direct-http-v1"
    assert contract["gateway"]["payload_logging"] is False
    assert contract["gateway"]["cache"] is False
    assert contract["operations"][SEMANTIC_OPERATION] == {
        "endpoint": "/accounts/{account_id}/ai/run",
        "request_encoding": "json",
        "gateway_attempt_limit": 2,
        "model_options": {
            "max_tokens": 4096,
            "response_format": "json_object",
            "temperature": 0,
        },
    }

    with pytest.raises(RemoteConsentInvalidatedError, match="notice"):
        create_consented_remote_run(
            item,
            expected_manifest_hash=plan.manifest.sha256,
            notice_version="stale-page",
            enqueue=False,
        )

    settings.CLOUDFLARE_CLIENT_TIMEOUT_SECONDS = 299
    with pytest.raises(RemoteConsentInvalidatedError, match="planned remote transfer"):
        create_consented_remote_run(
            item,
            expected_manifest_hash=plan.manifest.sha256,
            enqueue=False,
        )


@pytest.mark.django_db
def test_adapter_privacy_change_invalidates_consent_before_execution(settings):
    item = make_text_item()
    plan = build_remote_plan(item)
    run = create_consented_remote_run(
        item,
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    stage = run.stages.get(kind=ProcessingStage.Kind.REMOTE_SEMANTIC)
    gateway = RecordingGateway()

    settings.CLOUDFLARE_CLIENT_TIMEOUT_SECONDS = 299
    with pytest.raises(RemoteConsentInvalidatedError):
        execute_remote_stage(stage, gateway=gateway)

    assert gateway.calls == []


@pytest.mark.django_db
def test_injected_real_gateway_must_match_consented_destination():
    item = make_text_item()
    plan = build_remote_plan(item)
    run = create_consented_remote_run(
        item,
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    stage = run.stages.get(kind=ProcessingStage.Kind.REMOTE_SEMANTIC)
    gateway = CloudflareGateway(
        replace(CloudflareConfig.from_settings(), gateway_id="different-gateway")
    )

    with pytest.raises(RemoteConsentInvalidatedError, match="adapter or privacy"):
        execute_remote_stage(stage, gateway=gateway)


@pytest.mark.django_db
def test_adapter_privacy_change_during_request_discards_response(settings):
    item = make_text_item()
    plan = build_remote_plan(item)
    run = create_consented_remote_run(
        item,
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    stage = run.stages.get(kind=ProcessingStage.Kind.REMOTE_SEMANTIC)

    class PolicyChangingGateway(RecordingGateway):
        def analyze_text(self, source, *, metadata=None):
            response = super().analyze_text(source, metadata=metadata)
            settings.CLOUDFLARE_CLIENT_TIMEOUT_SECONDS = 299
            return response

    gateway = PolicyChangingGateway()
    with pytest.raises(RemoteConsentInvalidatedError):
        execute_remote_stage(stage, gateway=gateway)

    assert len(gateway.calls) == 1


def make_audio_item_with_chunk(chunk=b"normalized-flac"):
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.AUDIO)
    Audio.objects.create(
        catalog_item=item,
        file=SimpleUploadedFile("source.wav", b"canonical-audio"),
        mime_type="audio/wav",
        original_filename="source.wav",
    )
    fingerprint = source_fingerprint(item)
    local_run = ProcessingRun.objects.create(
        catalog_item=item,
        trigger=ProcessingRun.Trigger.AUTOMATIC,
        scope=ProcessingRun.Scope.LOCAL,
        source_fingerprint=fingerprint.sha256,
        source_size=fingerprint.size_bytes,
    )
    local_stage = ProcessingStage.objects.create(
        run=local_run,
        kind=ProcessingStage.Kind.AUDIO_SPLIT,
    )
    artifact = DerivedArtifact.objects.create(
        stage=local_stage,
        catalog_item=item,
        file=SimpleUploadedFile("chunk.flac", chunk),
        role=DerivedArtifact.Role.AUDIO_CHUNK,
        mime_type="audio/flac",
        sha256=sha256_bytes(chunk),
        byte_size=len(chunk),
        source_start_seconds="0.000000",
        source_end_seconds="5.000000",
        metadata={
            "sample_rate": 16000,
            "channels": 1,
            "downmix": "mono",
            "resampling": "16000_hz",
            "overlap_before_seconds": 0,
            "private_filename": "must-not-enter-the-manifest",
        },
    )
    return item, artifact


def add_audio_artwork(item, local_stage, content=b"embedded-cover-art"):
    return DerivedArtifact.objects.create(
        stage=local_stage,
        catalog_item=item,
        file=SimpleUploadedFile("cover.jpg", content),
        role=DerivedArtifact.Role.EMBEDDED_ARTWORK,
        mime_type="image/jpeg",
        sha256=sha256_bytes(content),
        byte_size=len(content),
        stream_index=2,
        metadata={"metadata_stripped": False, "attached_picture": True},
    )


def make_video_item_with_derivatives(
    *,
    frame_count=3,
    include_audio=True,
    metadata_stripped=True,
):
    original = b"canonical-video-never-transfer"
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.VIDEO)
    Video.objects.create(
        catalog_item=item,
        file=SimpleUploadedFile("source.mp4", original),
        mime_type="video/mp4",
        original_filename="source.mp4",
    )
    fingerprint = source_fingerprint(item)
    local_run = ProcessingRun.objects.create(
        catalog_item=item,
        trigger=ProcessingRun.Trigger.AUTOMATIC,
        scope=ProcessingRun.Scope.LOCAL,
        source_fingerprint=fingerprint.sha256,
        source_size=fingerprint.size_bytes,
    )
    frame_stage = ProcessingStage.objects.create(
        run=local_run,
        kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
    )
    frames = []
    for index in range(frame_count):
        content = f"metadata-stripped-frame-{index}".encode()
        frames.append(
            DerivedArtifact.objects.create(
                stage=frame_stage,
                catalog_item=item,
                file=SimpleUploadedFile(f"frame-{index:03d}.jpg", content),
                role=DerivedArtifact.Role.KEYFRAME,
                mime_type="image/jpeg",
                sha256=sha256_bytes(content),
                byte_size=len(content),
                frame_index=index,
                source_start_seconds=f"{index * 5}.000000",
                source_end_seconds=f"{index * 5}.000000",
                metadata={
                    "metadata_stripped": metadata_stripped,
                    "jpeg_quality": 2,
                },
            )
        )
    chunk = None
    if include_audio:
        chunk_content = b"normalized-video-audio-chunk"
        audio_stage = ProcessingStage.objects.create(
            run=local_run,
            kind=ProcessingStage.Kind.AUDIO_SPLIT,
        )
        chunk = DerivedArtifact.objects.create(
            stage=audio_stage,
            catalog_item=item,
            file=SimpleUploadedFile("chunk.flac", chunk_content),
            role=DerivedArtifact.Role.AUDIO_CHUNK,
            mime_type="audio/flac",
            sha256=sha256_bytes(chunk_content),
            byte_size=len(chunk_content),
            source_start_seconds="0.000000",
            source_end_seconds="15.000000",
            metadata={
                "metadata_stripped": True,
                "sample_rate": 16000,
                "channels": 1,
            },
        )
    return item, original, frames, chunk


def semantic_response(*, invalid_span=False):
    end = 999 if invalid_span else 4
    evidence = {"text": "Meet", "start": 0, "end": end, "confidence": 0.8}
    grounding = {"text": "Meet", "start": 0, "end": 4, "confidence": 0.8}
    return {
        "summary": "A meeting is proposed.",
        "language": "en",
        "summary_evidence": [grounding],
        "language_evidence": [grounding],
        "entities": [],
        "topics": [evidence],
        "claims": [],
        "questions": [],
        "requests": [],
        "commitments": [],
        "events": [],
        "dates": [],
        "locations": [],
        "sentiment": [],
        "tone": [],
        "intent_hypotheses": [],
        "style_observations": [],
        "contradictions": [],
        "uncertainties": [],
        "language_cues": [],
    }


def vision_response():
    return {
        "description": "A literal scene description.",
        "objects": [],
        "actions": [],
        "people_count": 0,
        "visible_appearance": [],
        "logos": [],
        "landmarks": [],
        "visible_text": [],
        "setting": "Unknown",
        "time_weather_hypotheses": [],
        "anomalies": [],
        "uncertainties": [],
    }


def transcription_response():
    return {
        "success": True,
        "result": {
            "results": {
                "channels": [
                    {
                        "detected_language": "en",
                        "language_confidence": 0.98,
                        "alternatives": [
                            {
                                "transcript": "Hello there.",
                                "confidence": 0.91,
                                "words": [
                                    {
                                        "word": "hello",
                                        "punctuated_word": "Hello",
                                        "start": 0.1,
                                        "end": 0.4,
                                        "confidence": 0.92,
                                        "speaker": 0,
                                        "speaker_confidence": 0.97,
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "utterances": [
                    {
                        "start": 0.1,
                        "end": 1.2,
                        "transcript": "Hello there.",
                        "confidence": 0.91,
                        "channel": 0,
                        "speaker": 0,
                        "words": [
                            {
                                "word": "hello",
                                "punctuated_word": "Hello",
                                "start": 0.1,
                                "end": 0.4,
                                "confidence": 0.92,
                                "speaker": 0,
                                "speaker_confidence": 0.97,
                            }
                        ],
                    }
                ],
            }
        },
    }


class RecordingGateway:
    def __init__(self):
        self.calls = []
        self.semantic_result = semantic_response()
        self.vision_result = vision_response()
        self.transcription_result = transcription_response()

    def analyze_text(self, source, *, metadata=None):
        self.calls.append(("semantic", source, metadata))
        return self.semantic_result

    def describe_image(self, content, *, metadata=None):
        self.calls.append(("image", content, metadata))
        return self.vision_result

    def transcribe_audio(self, content, *, mime_type, metadata=None):
        self.calls.append(("transcription", content, mime_type, metadata))
        return self.transcription_result


class MetadataResponse(dict):
    def __init__(self, value, request_metadata):
        super().__init__(value)
        self.request_metadata = request_metadata


@pytest.mark.django_db
def test_consent_creation_is_exact_immutable_and_commit_bound(
    django_capture_on_commit_callbacks,
):
    item = make_text_item()
    plan = build_remote_plan(item)

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        run = create_consented_remote_run(
            item,
            expected_manifest_hash=plan.manifest.sha256,
        )

    assert len(callbacks) == 1
    assert run.scope == ProcessingRun.Scope.REMOTE
    consent = run.remote_consents.get()
    assert consent.payload_manifest == plan.manifest.value
    assert consent.payload_manifest_hash == plan.manifest.sha256
    assert consent.source_fingerprint == plan.source_sha256
    assert consent.planned_request_count == 1
    assert consent.model_ids == plan.model_ids
    assert consent.prompt_versions == plan.prompt_versions
    assert run.configuration["prompt_hashes"] == plan.prompt_hashes
    stage = run.stages.get()
    assert stage.max_attempts == 1
    assert stage.status == ProcessingStage.Status.PENDING

    consent.notice_version = "changed"
    with pytest.raises(TypeError, match="append-only"):
        consent.save()

    with patch(
        "pandect.processing.remote.workflows._dispatch_remote_stage_task",
        return_value=SimpleNamespace(id="opaque-task-id"),
    ) as dispatch:
        callbacks[0]()
    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.QUEUED
    dispatch.assert_called_once_with(stage.pk)


@pytest.mark.django_db
def test_committed_remote_consent_survives_post_commit_dispatch_failure(
    django_capture_on_commit_callbacks,
    caplog,
):
    item = make_text_item()
    plan = build_remote_plan(item)

    with patch(
        "pandect.processing.remote.workflows.enqueue_remote_run",
        side_effect=RuntimeError("sensitive gateway queue detail"),
    ):
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            run = create_consented_remote_run(
                item,
                expected_manifest_hash=plan.manifest.sha256,
            )
        callbacks[0]()

    assert ProcessingRun.objects.filter(pk=run.pk).exists()
    assert run.remote_consents.count() == 1
    assert run.stages.filter(status=ProcessingStage.Status.PENDING).exists()
    assert "sensitive gateway queue detail" not in caplog.text
    assert str(run.pk) in caplog.text


@pytest.mark.django_db
def test_recovery_bulk_queues_every_remote_unit_before_dispatch():
    item, first_artifact = make_audio_item_with_chunk(b"first-chunk")
    second_bytes = b"second-chunk"
    second_artifact = DerivedArtifact.objects.create(
        stage=first_artifact.stage,
        catalog_item=item,
        file=SimpleUploadedFile("second.flac", second_bytes),
        role=DerivedArtifact.Role.AUDIO_CHUNK,
        mime_type="audio/flac",
        sha256=sha256_bytes(second_bytes),
        byte_size=len(second_bytes),
        source_start_seconds="5.000000",
        source_end_seconds="10.000000",
    )
    artifact_ids = [first_artifact.pk, second_artifact.pk]
    plan = build_remote_plan(item, artifact_ids=artifact_ids)
    run = create_consented_remote_run(
        item,
        artifact_ids=artifact_ids,
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )

    def assert_all_queued(_stage_id):
        assert set(run.stages.values_list("status", flat=True)) == {
            ProcessingStage.Status.QUEUED
        }
        return SimpleNamespace(id="task")

    with patch(
        "pandect.processing.remote.workflows._dispatch_remote_stage_task",
        side_effect=assert_all_queued,
    ) as dispatch:
        assert enqueue_ready_stages(run.pk) == 2

    assert dispatch.call_count == 2


@pytest.mark.django_db
def test_changed_plan_hash_is_rejected_without_creating_a_run():
    item = make_text_item()

    with pytest.raises(RemoteConsentInvalidatedError):
        create_consented_remote_run(
            item,
            expected_manifest_hash="0" * 64,
            enqueue=False,
        )

    assert not ProcessingRun.objects.filter(scope=ProcessingRun.Scope.REMOTE).exists()


@pytest.mark.django_db
def test_missing_consent_prevents_gateway_construction_and_transfer():
    item = make_text_item()
    fingerprint = source_fingerprint(item)
    run = ProcessingRun.objects.create(
        catalog_item=item,
        trigger=ProcessingRun.Trigger.MANUAL,
        scope=ProcessingRun.Scope.REMOTE,
        source_fingerprint=fingerprint.sha256,
        source_size=fingerprint.size_bytes,
        configuration={},
    )
    stage = ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.REMOTE_SEMANTIC,
        max_attempts=1,
        status=ProcessingStage.Status.QUEUED,
    )

    with patch(
        "pandect.processing.remote.workflows.CloudflareGateway"
    ) as gateway_constructor:
        run_remote_processing_stage(str(stage.pk))

    gateway_constructor.assert_not_called()
    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.FAILED
    assert stage.failure_code == "remote_consent_required"
    assert stage.attempts == 1


@pytest.mark.django_db
def test_semantic_task_persists_hypothesis_and_span_validation_warning():
    item = make_text_item()
    plan = build_remote_plan(item)
    run = create_consented_remote_run(
        item,
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    stage = run.stages.get()
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    gateway = RecordingGateway()
    gateway.semantic_result = semantic_response(invalid_span=True)

    with patch(
        "pandect.processing.remote.workflows.CloudflareGateway",
        return_value=gateway,
    ):
        run_remote_processing_stage(str(stage.pk))

    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.SUCCEEDED
    assert stage.attempts == 1
    analysis = stage.results.get(result_kind=ProcessingResult.Kind.SEMANTIC_ANALYSIS)
    warning = stage.results.get(result_kind=ProcessingResult.Kind.VALIDATION_WARNING)
    assert analysis.evidence_tier == ProcessingResult.EvidenceTier.MODEL_GENERATED
    assert analysis.text == "A meeting is proposed."
    assert analysis.provenance["consent_id"] == str(run.remote_consents.get().pk)
    assert warning.data["warnings"] == [
        {"field": "topics", "index": 0, "code": "invalid_span"}
    ]
    assert gateway.calls[0][0:2] == ("semantic", "Meet at noon.")


@pytest.mark.django_db
def test_semantic_preflight_ignores_a_cached_message_after_body_change():
    item = make_text_item()
    plan = build_remote_plan(item)
    run = create_consented_remote_run(
        item,
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    stage = run.stages.get()
    assert stage.run.catalog_item.message.body == "Meet at noon."
    Message.objects.filter(catalog_item_id=item.pk).update(body="Meet tomorrow.")
    gateway = RecordingGateway()

    with pytest.raises(RemoteSourceChangedError):
        execute_remote_stage(stage, gateway=gateway)

    assert gateway.calls == []


@pytest.mark.django_db
def test_semantic_task_supersedes_result_when_body_changes_during_request():
    item = make_text_item()
    plan = build_remote_plan(item)
    run = create_consented_remote_run(
        item,
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    stage = run.stages.get()
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )

    class BodyChangingGateway(RecordingGateway):
        def analyze_text(self, source, *, metadata=None):
            result = super().analyze_text(source, metadata=metadata)
            Message.objects.filter(catalog_item_id=item.pk).update(
                body="Changed while the remote request was in flight."
            )
            return result

    gateway = BodyChangingGateway()
    with patch(
        "pandect.processing.remote.workflows.CloudflareGateway",
        return_value=gateway,
    ):
        run_remote_processing_stage(str(stage.pk))

    stage.refresh_from_db()
    run.refresh_from_db()
    assert gateway.calls[0][0:2] == ("semantic", "Meet at noon.")
    assert stage.status == ProcessingStage.Status.SUPERSEDED
    assert run.status == ProcessingRun.Status.SUPERSEDED
    assert not stage.results.exists()


@pytest.mark.django_db
def test_image_stage_sends_exact_original_bytes_including_metadata():
    original = b"jpeg-prefix\x00EXIF private tags\xff"
    item = make_image_item(original)
    plan = build_remote_plan(item)
    run = create_consented_remote_run(
        item,
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    stage = run.stages.get()
    gateway = RecordingGateway()

    outcome = execute_remote_stage(stage, gateway=gateway)

    assert gateway.calls[0][0] == "image"
    assert gateway.calls[0][1] == original
    assert plan.payloads[0]["embedded_metadata_included"] is True
    assert outcome.results[0].result_kind == ProcessingResult.Kind.IMAGE_DESCRIPTION


@pytest.mark.django_db
def test_oversized_exact_image_plan_fails_before_source_bytes_are_read(monkeypatch):
    item = make_image_item(b"small fixture")
    monkeypatch.setattr(
        "pandect.processing.remote.workflows.source_fingerprint",
        lambda _item: SimpleNamespace(
            sha256="a" * 64,
            size_bytes=MAX_VISION_IMAGE_BYTES + 1,
        ),
    )
    monkeypatch.setattr(
        item.image.file,
        "open",
        lambda *_args, **_kwargs: pytest.fail("original must not be read"),
    )

    with pytest.raises(RemotePlanError, match="10 MiB"):
        build_remote_plan(item)


@pytest.mark.django_db
def test_result_retains_allowlisted_gateway_response_metadata():
    item = make_text_item()
    plan = build_remote_plan(item)
    run = create_consented_remote_run(
        item,
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    gateway = RecordingGateway()
    gateway.semantic_result = MetadataResponse(
        semantic_response(),
        {
            "http_status": 200,
            "response_headers": {"cf-ray": "ray", "server": "ignored"},
            "gateway": {"requestId": "request", "privatePayload": "ignored"},
        },
    )

    outcome = execute_remote_stage(run.stages.get(), gateway=gateway)

    assert outcome.results[0].remote_request_metadata["http_status"] == 200
    assert outcome.results[0].remote_request_metadata["response_headers"] == {
        "cf-ray": "ray"
    }
    assert outcome.results[0].remote_request_metadata["gateway"] == {
        "requestId": "request"
    }


@pytest.mark.django_db
def test_audio_plan_and_stage_bind_and_send_only_selected_chunk_bytes():
    item, artifact = make_audio_item_with_chunk()
    plan = build_remote_plan(item, artifact_ids=[artifact.pk])
    run = create_consented_remote_run(
        item,
        artifact_ids=[artifact.pk],
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    stage = run.stages.get()
    gateway = RecordingGateway()

    outcome = execute_remote_stage(stage, gateway=gateway)

    assert gateway.calls[0][0:3] == (
        "transcription",
        b"normalized-flac",
        "audio/flac",
    )
    assert plan.payloads[0]["artifact_id"] == str(artifact.pk)
    assert plan.payloads[0]["source_start_seconds"] == "0.000000"
    assert plan.payloads[0]["payload_limit_bytes"] > artifact.byte_size
    assert plan.payloads[0]["transfer_parameters"] == {
        "channels": 1,
        "downmix": "mono",
        "overlap_before_seconds": 0,
        "resampling": "16000_hz",
        "sample_rate": 16000,
    }
    result = outcome.results[0]
    assert result.text == "Hello there."
    assert result.confidence == 0.91


@pytest.mark.django_db
def test_transcription_consent_hash_binds_the_strict_provider_schema():
    item, artifact = make_audio_item_with_chunk()
    plan = build_remote_plan(item, artifact_ids=[artifact.pk])
    changed_schema = {
        **cloudflare_adapter.TRANSCRIPTION_SCHEMA,
        "$comment": "consent-bound revision",
    }

    with patch.object(cloudflare_adapter, "TRANSCRIPTION_SCHEMA", changed_schema):
        changed = build_remote_plan(item, artifact_ids=[artifact.pk])

    assert (
        changed.prompt_hashes[TRANSCRIPTION_OPERATION]
        != plan.prompt_hashes[TRANSCRIPTION_OPERATION]
    )
    assert changed.manifest.sha256 != plan.manifest.sha256


@pytest.mark.django_db
@pytest.mark.parametrize(
    "missing_path",
    (
        ("channels", 0, "detected_language"),
        ("channels", 0, "language_confidence"),
        ("channels", 0, "alternatives", 0, "confidence"),
        ("channels", 0, "alternatives", 0, "words", 0, "start"),
        ("channels", 0, "alternatives", 0, "words", 0, "confidence"),
        ("channels", 0, "alternatives", 0, "words", 0, "speaker"),
        ("utterances", 0, "start"),
        ("utterances", 0, "confidence"),
        ("utterances", 0, "speaker"),
        ("utterances", 0, "words"),
    ),
)
def test_nova_response_requires_requested_language_timing_confidence_and_speakers(
    missing_path,
):
    item, artifact = make_audio_item_with_chunk()
    plan = build_remote_plan(item, artifact_ids=[artifact.pk])
    run = create_consented_remote_run(
        item,
        artifact_ids=[artifact.pk],
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    response = deepcopy(transcription_response())
    current = response["result"]["results"]
    for part in missing_path[:-1]:
        current = current[part]
    del current[missing_path[-1]]
    gateway = RecordingGateway()
    gateway.transcription_result = response

    with pytest.raises(RemoteResponseValidationError, match="failed validation"):
        execute_remote_stage(run.stages.get(), gateway=gateway)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("value_path", "invalid_value"),
    (
        (("channels", 0, "language_confidence"), float("nan")),
        (("channels", 0, "alternatives", 0, "words", 0, "end"), 0.05),
        (("utterances", 0, "end"), 0.05),
    ),
)
def test_nova_response_rejects_nonfinite_confidence_and_reversed_times(
    value_path,
    invalid_value,
):
    item, artifact = make_audio_item_with_chunk()
    plan = build_remote_plan(item, artifact_ids=[artifact.pk])
    run = create_consented_remote_run(
        item,
        artifact_ids=[artifact.pk],
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    response = deepcopy(transcription_response())
    current = response["result"]["results"]
    for part in value_path[:-1]:
        current = current[part]
    current[value_path[-1]] = invalid_value
    gateway = RecordingGateway()
    gateway.transcription_result = response

    with pytest.raises(RemoteResponseValidationError, match="failed validation"):
        execute_remote_stage(run.stages.get(), gateway=gateway)


@pytest.mark.django_db
def test_audio_plan_refuses_over_limit_chunk_before_consent(monkeypatch):
    item, artifact = make_audio_item_with_chunk(b"12345")
    monkeypatch.setattr(
        "pandect.processing.remote.workflows.MAX_TRANSCRIPTION_AUDIO_BYTES", 4
    )

    with pytest.raises(RemotePlanError, match="payload limit"):
        build_remote_plan(item, artifact_ids=[artifact.pk])


@pytest.mark.django_db
def test_audio_default_plan_hash_binds_artwork_and_transcription_separately():
    item, chunk = make_audio_item_with_chunk()
    artwork = add_audio_artwork(item, chunk.stage)

    plan = build_remote_plan(item)

    assert {payload["transfer"] for payload in plan.payloads} == {
        "exact_embedded_artwork",
        "normalized_audio_chunk",
    }
    artwork_payload = next(
        payload
        for payload in plan.payloads
        if payload["transfer"] == "exact_embedded_artwork"
    )
    assert artwork_payload["artifact_id"] == str(artwork.pk)
    assert artwork_payload["embedded_metadata_included"] is True
    assert artwork_payload["sha256"] == artwork.sha256
    assert plan.manifest.value["planned_request_count"] == 2


@pytest.mark.django_db
def test_consented_audio_artwork_sends_only_the_selected_derivative():
    item, chunk = make_audio_item_with_chunk()
    artwork = add_audio_artwork(item, chunk.stage)
    plan = build_remote_plan(item, artifact_ids=[artwork.pk])
    run = create_consented_remote_run(
        item,
        artifact_ids=[artwork.pk],
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    gateway = RecordingGateway()

    outcome = execute_remote_stage(run.stages.get(), gateway=gateway)

    assert gateway.calls[0][0:2] == ("image", b"embedded-cover-art")
    assert outcome.results[0].provenance["transfer"] == "exact_embedded_artwork"


@pytest.mark.django_db
def test_video_plan_caps_keyframes_and_includes_audio_without_original_video():
    item, original, frames, chunk = make_video_item_with_derivatives(frame_count=14)

    plan = build_remote_plan(item)

    image_payloads = [
        value
        for value in plan.payloads
        if value["operation"] == IMAGE_DESCRIPTION_OPERATION
    ]
    audio_payloads = [
        value
        for value in plan.payloads
        if value["operation"] == TRANSCRIPTION_OPERATION
    ]
    assert len(image_payloads) == 12
    assert len(audio_payloads) == 1
    assert plan.keyframe_count == 12
    assert plan.audio_chunk_count == 1
    assert plan.total_payload_bytes == sum(
        value["byte_size"] for value in plan.payloads
    )
    assert plan.total_audio_duration_seconds == 15
    assert image_payloads[0]["artifact_id"] == str(frames[0].pk)
    assert image_payloads[-1]["artifact_id"] == str(frames[-1].pk)
    assert audio_payloads[0]["artifact_id"] == str(chunk.pk)
    assert all(value["sha256"] != sha256_bytes(original) for value in plan.payloads)
    assert all(value["transfer"] != "exact_original_video" for value in plan.payloads)
    assert set(plan.operations) == {
        IMAGE_DESCRIPTION_OPERATION,
        TRANSCRIPTION_OPERATION,
    }
    assert "Deepgram Nova-3 model service" in plan.recipients
    assert plan.manifest.value["payloads"] == list(plan.payloads)
    assert plan.manifest.value["planned_request_count"] == 13


@pytest.mark.django_db
def test_video_image_stage_reads_only_consent_bound_derived_keyframe():
    item, original, frames, _ = make_video_item_with_derivatives(frame_count=2)
    plan = build_remote_plan(
        item,
        operations=[IMAGE_DESCRIPTION_OPERATION],
        artifact_ids=[frames[1].pk],
    )
    run = create_consented_remote_run(
        item,
        operations=[IMAGE_DESCRIPTION_OPERATION],
        artifact_ids=[frames[1].pk],
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    stage = run.stages.get()
    gateway = RecordingGateway()

    outcome = execute_remote_stage(stage, gateway=gateway)

    with frames[1].file.open("rb") as stream:
        selected_frame = stream.read()
    assert gateway.calls == [
        (
            "image",
            selected_frame,
            {
                "run_id": str(run.pk),
                "stage_id": str(stage.pk),
                "operation": IMAGE_DESCRIPTION_OPERATION,
            },
        )
    ]
    assert selected_frame != original
    result = outcome.results[0]
    assert result.provenance["artifact_id"] == str(frames[1].pk)
    assert result.provenance["frame_index"] == 1
    assert result.provenance["source_start_seconds"] == "5.000000"
    assert result.provenance["transfer"] == "metadata_stripped_video_keyframe"
    assert "No cross-frame or transcript synthesis" in result.uncertainty


@pytest.mark.django_db
def test_video_without_audio_consents_only_to_available_keyframes():
    item, _, frames, _ = make_video_item_with_derivatives(
        frame_count=2,
        include_audio=False,
    )

    plan = build_remote_plan(item)

    assert plan.operations == (IMAGE_DESCRIPTION_OPERATION,)
    assert plan.artifact_ids == tuple(str(frame.pk) for frame in frames)
    assert plan.audio_chunk_count == 0
    assert TRANSCRIPTION_OPERATION not in plan.model_ids
    assert "Deepgram Nova-3 model service" not in plan.recipients


@pytest.mark.django_db
def test_video_plan_rejects_keyframe_not_recorded_as_metadata_stripped():
    item, _, _, _ = make_video_item_with_derivatives(
        frame_count=1,
        include_audio=False,
        metadata_stripped=False,
    )

    with pytest.raises(RemotePlanError, match="metadata-stripped"):
        build_remote_plan(item)


@pytest.mark.django_db
def test_video_plan_never_selects_derivatives_from_an_old_source_fingerprint():
    item, _, _, _ = make_video_item_with_derivatives(
        frame_count=1,
        include_audio=False,
    )
    with item.video.file.open("wb") as stream:
        stream.write(b"replacement-canonical-video")

    with pytest.raises(RemotePlanError, match="No metadata-stripped"):
        build_remote_plan(item)


@pytest.mark.django_db
def test_changed_video_keyframe_invalidates_consent_before_gateway_call():
    item, _, frames, _ = make_video_item_with_derivatives(
        frame_count=1,
        include_audio=False,
    )
    plan = build_remote_plan(item)
    run = create_consented_remote_run(
        item,
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    stage = run.stages.get()
    with frames[0].file.open("wb") as stream:
        stream.write(b"changed-keyframe")
    gateway = RecordingGateway()

    with pytest.raises(RemoteConsentInvalidatedError):
        execute_remote_stage(stage, gateway=gateway)

    assert gateway.calls == []


@pytest.mark.django_db
def test_model_and_prompt_changes_invalidate_consent_before_gateway_call(settings):
    item = make_text_item()
    plan = build_remote_plan(item)
    run = create_consented_remote_run(
        item,
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    stage = run.stages.get()
    gateway = RecordingGateway()

    settings.CLOUDFLARE_TEXT_MODEL = "@cf/deepgram/nova-3"
    with pytest.raises(RemoteConsentInvalidatedError):
        execute_remote_stage(stage, gateway=gateway)
    assert gateway.calls == []

    settings.CLOUDFLARE_TEXT_MODEL = "@cf/meta/llama-3.2-11b-vision-instruct"
    with patch.dict(
        PROMPT_VERSIONS,
        {SEMANTIC_OPERATION: "semantic-schema-v2"},
    ):
        with pytest.raises(RemoteConsentInvalidatedError):
            execute_remote_stage(stage, gateway=gateway)
    assert gateway.calls == []

    with patch.object(
        cloudflare_adapter,
        "SEMANTIC_EVIDENCE_PROMPT",
        "A materially changed prompt containing {source}",
    ):
        with pytest.raises(RemoteConsentInvalidatedError):
            execute_remote_stage(stage, gateway=gateway)
    assert gateway.calls == []


@pytest.mark.django_db
def test_replaced_chunk_invalidates_consent_before_gateway_call():
    item, artifact = make_audio_item_with_chunk()
    plan = build_remote_plan(item, artifact_ids=[artifact.pk])
    run = create_consented_remote_run(
        item,
        artifact_ids=[artifact.pk],
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    stage = run.stages.get()
    with artifact.file.open("wb") as stream:
        stream.write(b"different-bytes")
    gateway = RecordingGateway()

    with pytest.raises(RemoteConsentInvalidatedError):
        execute_remote_stage(stage, gateway=gateway)

    assert gateway.calls == []


@pytest.mark.django_db
def test_invalid_remote_response_fails_once_without_huey_retry():
    item, artifact = make_audio_item_with_chunk()
    plan = build_remote_plan(item, artifact_ids=[artifact.pk])
    run = create_consented_remote_run(
        item,
        artifact_ids=[artifact.pk],
        expected_manifest_hash=plan.manifest.sha256,
        enqueue=False,
    )
    stage = run.stages.get()
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    gateway = RecordingGateway()
    gateway.transcription_result = {"success": True, "result": {"unexpected": []}}

    with patch(
        "pandect.processing.remote.workflows.CloudflareGateway",
        return_value=gateway,
    ):
        run_remote_processing_stage(str(stage.pk))
        run_remote_processing_stage(str(stage.pk))

    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.FAILED
    assert stage.failure_code == "remote_response_invalid"
    assert stage.attempts == 1
    assert len(gateway.calls) == 1
