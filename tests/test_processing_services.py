import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.db.models.query import QuerySet
from django.test import override_settings
from django.utils import timezone

from pandect.catalog.models import CatalogItem
from pandect.identities.models import IdentityCandidate, IdentityObservation, Subject
from pandect.identities.services import encode_embedding
from pandect.messages.models import Message, TextMessage
from pandect.photos.models import Image
from pandect.processing import services as processing_services
from pandect.processing.execution import execute_stage
from pandect.processing.extractors.hashing import FileFingerprint, sha256_bytes
from pandect.processing.models import (
    DerivedArtifact,
    ProcessingResult,
    ProcessingRun,
    ProcessingStage,
)
from pandect.processing.services import (
    ArtifactSpec,
    ConsumedArtifactSpec,
    IdentityCandidateSpec,
    IdentityObservationSpec,
    ResultSpec,
    StageOutcome,
    StageProcessingError,
    claim_stage,
    clean_attempt_workspace,
    complete_stage,
    create_local_run,
    enqueue_ready_stages,
    enqueue_stage,
    fail_stage,
    heartbeat_stage,
    local_processing_configuration_snapshot,
    materialize_unit_stages,
    recompute_run_status,
    reconcile_stages,
    skip_stage,
    source_fingerprint,
    supersede_and_replace_local_run,
)
from pandect.videos.models import Video


def make_text_item(body: str = "Hello, exact bytes.") -> CatalogItem:
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.TEXT_MESSAGE)
    message = Message.objects.create(catalog_item=item, body=body)
    TextMessage.objects.create(message=message)
    return item


def make_video_item() -> CatalogItem:
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.VIDEO)
    Video.objects.create(
        catalog_item=item,
        file=SimpleUploadedFile("evidence.mp4", b"video-source"),
    )
    return item


def keyframe_plan_outcome(run: ProcessingRun, *, frame_count: int = 2) -> StageOutcome:
    frames = []
    for index in range(frame_count):
        reason = "first_frame" if index == 0 else "last_frame"
        frames.append(
            {
                "unit_key": f"frame:{index:03d}",
                "selection_index": index,
                "timestamp_seconds": float(index),
                "source_stream_index": 0,
                "selection_reason": reason,
                "scene_change_score": None,
            }
        )
    return StageOutcome(
        results=(
            ResultSpec(
                result_kind=ProcessingResult.Kind.KEYFRAMES,
                evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
                data={
                    "phase": "plan",
                    "frames": frames,
                    "maximum": 120,
                    "selected_frame_count": frame_count,
                    "duration_seconds": float(max(1, frame_count)),
                    "selection_policy": {
                        "endpoints": True,
                        "scene_changes": True,
                        "even_coverage_interval_seconds": 60,
                        "scene_change_threshold": 0.3,
                        "scene_score_field": "lavfi.scene_score",
                        "scene_candidate_order": (
                            "score_descending_timestamp_ascending"
                        ),
                    },
                    "scene_candidates": [],
                    "video_streams": [{"index": 0, "codec_type": "video"}],
                },
                processor_name="ffprobe+ffmpeg-scene-plan",
                processor_version="1",
                input_hashes={"canonical": run.source_fingerprint},
            ),
        )
    )


def make_writing_observation(
    item: CatalogItem,
    *,
    source_sha256: str,
) -> IdentityObservation:
    return IdentityObservation.objects.create(
        catalog_item=item,
        modality=IdentityObservation.Modality.WRITING,
        unit_key=f"body:{uuid.uuid4()}",
        source_location={"start": 0, "end": 5},
        source_sha256=source_sha256,
        embedding=encode_embedding((1.0, 0.0)),
        embedding_dimension=2,
        feature_space="writing-style-v1",
        processor_name="pandect.writing",
        processor_version="1",
        preprocessing_version="1",
        provenance={"source": "test"},
        quality_status=IdentityObservation.QualityStatus.APPROVED,
        quality_score=0.9,
        quality_details={"signal": "clear"},
        sample_word_count=2,
    )


def make_claimed_identity_stage(body: str = "Candidate source"):
    run = create_local_run(make_text_item(body), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.WRITING_IDENTITY)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    claimed = claim_stage(stage.pk)
    assert claimed is not None
    return run, claimed


@pytest.mark.django_db
def test_source_fingerprint_hashes_exact_message_utf8_bytes():
    body = "Line one\r\nEmoji: 🧭"
    item = make_text_item(body)

    fingerprint = source_fingerprint(item)

    encoded = body.encode("utf-8")
    assert fingerprint.sha256 == sha256_bytes(encoded)
    assert fingerprint.size_bytes == len(encoded)


@pytest.mark.django_db
def test_source_fingerprint_streams_original_media_without_changing_it():
    original = b"not decoded or normalized\x00\xff"
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.IMAGE)
    image = Image.objects.create(
        catalog_item=item,
        file=SimpleUploadedFile("evidence.bin", original),
    )

    fingerprint = source_fingerprint(item)

    assert fingerprint.sha256 == sha256_bytes(original)
    assert fingerprint.size_bytes == len(original)
    with image.file.open("rb") as stream:
        assert stream.read() == original


@pytest.mark.django_db
def test_recompute_run_status_cannot_resurrect_concurrently_superseded_run():
    run = create_local_run(make_text_item(), enqueue=False)
    run.stages.update(status=ProcessingStage.Status.SUCCEEDED)
    original_update = QuerySet.update
    superseded_at = timezone.now() - timedelta(minutes=2)
    injected_supersede = False

    def update_with_supersede_race(queryset, **updates):
        nonlocal injected_supersede
        if (
            queryset.model is ProcessingRun
            and updates.get("status") == ProcessingRun.Status.SUCCEEDED
            and not injected_supersede
        ):
            injected_supersede = True
            original_update(
                ProcessingRun.objects.filter(pk=run.pk),
                status=ProcessingRun.Status.SUPERSEDED,
                finished_at=superseded_at,
            )
        return original_update(queryset, **updates)

    with patch.object(
        QuerySet, "update", autospec=True, side_effect=update_with_supersede_race
    ):
        status = recompute_run_status(run.pk)

    run.refresh_from_db()
    assert injected_supersede is True
    assert status == ProcessingRun.Status.SUPERSEDED
    assert run.status == ProcessingRun.Status.SUPERSEDED
    assert run.finished_at == superseded_at


@pytest.mark.django_db
def test_recompute_run_status_cannot_overwrite_a_concurrent_stage_claim():
    run = create_local_run(make_text_item(), enqueue=False)
    run.stages.update(status=ProcessingStage.Status.SUCCEEDED)
    original_update = QuerySet.update
    claimed_at = timezone.now() - timedelta(minutes=1)
    injected_claim = False

    def update_with_claim_race(queryset, **updates):
        nonlocal injected_claim
        if (
            queryset.model is ProcessingRun
            and updates.get("status") == ProcessingRun.Status.SUCCEEDED
            and not injected_claim
        ):
            injected_claim = True
            original_update(
                ProcessingRun.objects.filter(pk=run.pk),
                status=ProcessingRun.Status.RUNNING,
                started_at=claimed_at,
                updated_at=claimed_at,
            )
        return original_update(queryset, **updates)

    with patch.object(
        QuerySet, "update", autospec=True, side_effect=update_with_claim_race
    ):
        status = recompute_run_status(run.pk)

    run.refresh_from_db()
    assert injected_claim is True
    assert status == ProcessingRun.Status.RUNNING
    assert run.status == ProcessingRun.Status.RUNNING
    assert run.started_at == claimed_at
    assert run.finished_at is None


@pytest.mark.django_db
def test_recompute_run_status_preserves_terminal_finished_timestamp():
    run = create_local_run(make_text_item(), enqueue=False)
    run.stages.update(status=ProcessingStage.Status.SUCCEEDED)
    finished_at = timezone.now() - timedelta(days=1)
    ProcessingRun.objects.filter(pk=run.pk).update(
        status=ProcessingRun.Status.SUCCEEDED,
        finished_at=finished_at,
    )

    assert recompute_run_status(run.pk) == ProcessingRun.Status.SUCCEEDED
    assert recompute_run_status(run.pk) == ProcessingRun.Status.SUCCEEDED

    run.refresh_from_db()
    assert run.finished_at == finished_at


@pytest.mark.django_db
def test_create_local_run_defers_uuid_only_enqueue_until_commit(
    django_capture_on_commit_callbacks,
):
    item = make_text_item()

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        run = create_local_run(item)

    assert len(callbacks) == 1
    assert list(run.stages.values_list("kind", "status")) == [
        (ProcessingStage.Kind.TEXT_EXTRACTION, ProcessingStage.Status.PENDING),
        (ProcessingStage.Kind.WRITING_STYLE, ProcessingStage.Status.PENDING),
        (ProcessingStage.Kind.WRITING_IDENTITY, ProcessingStage.Status.PENDING),
    ]

    with patch(
        "pandect.processing.services._dispatch_stage_task",
        return_value=SimpleNamespace(id="task-id"),
    ) as dispatch:
        callbacks[0]()

    first = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    assert first.status == ProcessingStage.Status.QUEUED
    dispatch.assert_called_once_with(first.pk)


@pytest.mark.django_db
def test_dispatch_inverts_persisted_priority_for_huey_ordering():
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(priority=21)

    with patch(
        "pandect.processing.tasks.run_processing_stage",
        return_value=SimpleNamespace(id="priority-task"),
    ) as task:
        processing_services._dispatch_stage_task(stage.pk)

    task.assert_called_once_with(str(stage.pk), priority=-21)


@pytest.mark.django_db
def test_local_run_snapshots_effective_configuration_and_preserves_caller_values():
    item = make_text_item()
    with override_settings(
        PROCESSING_MAX_IMAGE_PIXELS=123_456,
        PROCESSING_MAX_IMAGE_FRAMES=7,
        PROCESSING_MAX_VIDEO_FRAMES=19,
        PROCESSING_SUBPROCESS_OUTPUT_LIMIT=987_654,
    ):
        run = create_local_run(
            item,
            configuration={"manual_reason": "processor_upgrade"},
            enqueue=False,
        )

    assert run.configuration["manual_reason"] == "processor_upgrade"
    snapshot = run.configuration["local_processing_snapshot"]
    assert snapshot["item_type"] == CatalogItem.ItemType.TEXT_MESSAGE
    assert snapshot["processor_version"] == run.processor_version
    assert [stage["kind"] for stage in snapshot["stages"]] == [
        ProcessingStage.Kind.TEXT_EXTRACTION,
        ProcessingStage.Kind.WRITING_STYLE,
        ProcessingStage.Kind.WRITING_IDENTITY,
    ]
    assert snapshot["resource_limits"]["image_max_decoded_pixels"] == 123_456
    assert snapshot["resource_limits"]["image_max_decoded_frames"] == 7
    assert snapshot["resource_limits"]["video_max_local_frames"] == 19
    assert snapshot["resource_limits"]["subprocess_configured_output_bytes"] == (
        987_654
    )
    assert snapshot["resource_limits"]["native_inference_threads"] == 1
    assert snapshot["resource_limits"]["ffmpeg_decoder_threads"] == 1
    assert snapshot["resource_limits"]["ffmpeg_encoder_threads"] == 1
    assert snapshot["resource_limits"]["ffmpeg_filter_threads"] == 1
    assert snapshot["resource_limits"]["ffmpeg_filter_complex_threads"] == 1
    assert snapshot["models"] == []
    assert "PANDECT_" not in repr(snapshot)


def test_local_snapshot_retains_digest_pinned_model_provenance():
    snapshot = local_processing_configuration_snapshot(CatalogItem.ItemType.IMAGE)

    models = {model["filename"]: model for model in snapshot["models"]}
    assert snapshot["schema_version"] == 3
    assert models["face_detection_yunet_2023mar.onnx"]["sha256"] == (
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
    )
    assert models["age-gender-recognition-retail-0013.xml"]["revision"] == (
        "Open Model Zoo 2021.4 / IR v10 / FP16"
    )
    assert models["face_detection_yunet_2023mar.onnx"]["source"].startswith(
        "https://github.com/opencv/opencv_zoo/"
    )
    assert models["age-gender-recognition-retail-0013.xml"]["note"].startswith(
        "Paired with age-gender-recognition-retail-0013.bin"
    )
    assert (
        "complete training-corpus manifest"
        in models["face_recognition_sface_2021dec.onnx"]["training_corpus_limitation"]
    )
    assert models["face_recognition_sface_2021dec.onnx"]["legal_references"][0][
        "archive"
    ].startswith("legal/")
    assert (
        len(
            models["face_recognition_sface_2021dec.onnx"]["legal_references"][0][
                "sha256"
            ]
        )
        == 64
    )
    assert all("path" not in model for model in models.values())


@pytest.mark.django_db
def test_local_run_persists_applicable_model_provenance():
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.IMAGE)
    Image.objects.create(
        catalog_item=item,
        file=SimpleUploadedFile("evidence.jpg", b"image-source"),
    )

    run = create_local_run(item, enqueue=False)

    models = {
        model["filename"]: model
        for model in run.configuration["local_processing_snapshot"]["models"]
    }
    assert models["face_recognition_sface_2021dec.onnx"]["source"].startswith(
        "https://github.com/opencv/opencv_zoo/"
    )
    assert models["face_recognition_sface_2021dec.onnx"]["training_corpus_limitation"]


@pytest.mark.django_db
def test_rolled_back_run_never_dispatches(django_capture_on_commit_callbacks):
    item = make_text_item()

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                create_local_run(item)
                raise RuntimeError("roll back")

    assert callbacks == []
    assert not ProcessingRun.objects.filter(catalog_item=item).exists()


@pytest.mark.django_db
def test_claim_is_idempotent_and_completion_appends_results():
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    with patch(
        "pandect.processing.services._dispatch_stage_task",
        return_value=SimpleNamespace(id="one"),
    ):
        assert enqueue_stage(stage.pk)

    claimed = claim_stage(stage.pk)
    assert claimed is not None
    assert claimed.attempts == 1
    assert claim_stage(stage.pk) is None

    outcome = StageOutcome(
        results=(
            ResultSpec(
                result_kind=ProcessingResult.Kind.MESSAGE_TEXT_PROFILE,
                evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
                data={"counts": {"words": 3}},
                processor_name="pandect.text",
                processor_version="1",
                input_hashes={"source": run.source_fingerprint},
            ),
        )
    )
    assert complete_stage(
        stage.pk,
        outcome,
        expected_attempt=claimed.attempts,
        observed_source_fingerprint=run.source_fingerprint,
    )
    assert not complete_stage(
        stage.pk,
        outcome,
        expected_attempt=claimed.attempts,
    )

    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.SUCCEEDED
    assert stage.results.get().data == {"counts": {"words": 3}}


@pytest.mark.django_db
def test_completion_rehashes_consumed_artifact_after_execution(tmp_path):
    with override_settings(MEDIA_ROOT=tmp_path):
        run = create_local_run(make_video_item(), enqueue=False)
        producer = run.stages.get(kind=ProcessingStage.Kind.VIDEO_KEYFRAMES)
        consumer = run.stages.get(
            kind=ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
            unit_key="aggregate",
        )
        ProcessingStage.objects.filter(pk=consumer.pk).update(
            status=ProcessingStage.Status.QUEUED
        )
        consumer = claim_stage(consumer.pk)
        assert consumer is not None

        relative = f"derived/{run.catalog_item_id}/{run.pk}/{producer.pk}-frame-000.jpg"
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        original = b"recorded-frame"
        path.write_bytes(original)
        artifact = DerivedArtifact.objects.create(
            stage=producer,
            catalog_item=run.catalog_item,
            file=relative,
            role=DerivedArtifact.Role.KEYFRAME,
            mime_type="image/jpeg",
            sha256=sha256_bytes(original),
            byte_size=len(original),
            frame_index=0,
            metadata={"metadata_stripped": True},
        )
        outcome = StageOutcome(
            results=(
                ResultSpec(
                    result_kind=ProcessingResult.Kind.IMAGE_VISUAL,
                    evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
                    processor_name="test",
                    processor_version="1",
                ),
            ),
            consumed_artifacts=(
                ConsumedArtifactSpec(
                    artifact_id=artifact.pk,
                    sha256=artifact.sha256,
                    byte_size=artifact.byte_size,
                ),
            ),
        )

        # Simulate mutation after execute_stage returned but before complete_stage.
        path.write_bytes(b"changed-after-execution")
        with pytest.raises(StageProcessingError) as failure:
            complete_stage(
                consumer.pk,
                outcome,
                expected_attempt=consumer.attempts,
                observed_source_fingerprint=run.source_fingerprint,
            )

    assert failure.value.code == "derived_artifact_integrity"
    consumer.refresh_from_db()
    assert consumer.status == ProcessingStage.Status.RUNNING
    assert not consumer.results.exists()


@pytest.mark.django_db
def test_producer_completion_materializes_independent_frame_units(
    tmp_path, django_capture_on_commit_callbacks
):
    with override_settings(MEDIA_ROOT=tmp_path):
        run = create_local_run(make_video_item(), enqueue=False)
        plan = run.stages.get(kind=ProcessingStage.Kind.VIDEO_KEYFRAMES)
        assert plan.unit_key == "plan"
        ProcessingStage.objects.filter(pk=plan.pk).update(
            status=ProcessingStage.Status.QUEUED
        )
        plan = claim_stage(plan.pk)
        assert plan is not None

        with django_capture_on_commit_callbacks(execute=False):
            assert complete_stage(
                plan.pk,
                keyframe_plan_outcome(run),
                expected_attempt=plan.attempts,
                observed_source_fingerprint=run.source_fingerprint,
            )

        keyframe_stages = run.stages.filter(kind=ProcessingStage.Kind.VIDEO_KEYFRAMES)
        assert set(keyframe_stages.values_list("unit_key", flat=True)) == {
            "plan",
            "aggregate",
            "frame:000",
            "frame:001",
        }
        assert not materialize_unit_stages(plan)

        for index in range(2):
            unit = keyframe_stages.get(unit_key=f"frame:{index:03d}")
            ProcessingStage.objects.filter(pk=unit.pk).update(
                status=ProcessingStage.Status.QUEUED
            )
            unit = claim_stage(unit.pk)
            assert unit is not None
            relative = (
                f"derived/{run.catalog_item_id}/{run.pk}/"
                f"{unit.pk}-frame-{index:03d}.jpg"
            )
            payload = f"frame-{index}".encode()
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            with django_capture_on_commit_callbacks(execute=False):
                assert complete_stage(
                    unit.pk,
                    StageOutcome(
                        artifacts=(
                            ArtifactSpec(
                                file_name=relative,
                                role=DerivedArtifact.Role.KEYFRAME,
                                mime_type="image/jpeg",
                                sha256=sha256_bytes(payload),
                                byte_size=len(payload),
                                stream_index=0,
                                frame_index=index,
                                source_start_seconds=index,
                                source_end_seconds=index,
                                metadata={"metadata_stripped": True},
                            ),
                        )
                    ),
                    expected_attempt=unit.attempts,
                    observed_source_fingerprint=run.source_fingerprint,
                )

    stages = run.stages.filter(kind=ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS)
    assert set(stages.values_list("unit_key", flat=True)) == {
        "aggregate",
        "frame:000",
        "frame:001",
    }
    assert keyframe_stages.get(unit_key="frame:000").priority == 20
    assert stages.get(unit_key="frame:000").priority == 21
    assert keyframe_stages.get(unit_key="frame:001").priority == 22
    assert stages.get(unit_key="frame:001").priority == 23


@pytest.mark.django_db
def test_failed_unit_does_not_block_successful_sibling_or_aggregate():
    run = create_local_run(make_video_item(), enqueue=False)
    producer = run.stages.get(kind=ProcessingStage.Kind.VIDEO_KEYFRAMES)
    ProcessingStage.objects.filter(pk=producer.pk).update(
        status=ProcessingStage.Status.SUCCEEDED
    )
    ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
        unit_key="aggregate",
        status=ProcessingStage.Status.SUCCEEDED,
        finished_at=timezone.now(),
        priority=25,
    )
    aggregate = run.stages.get(
        kind=ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
        unit_key="aggregate",
    )
    failed = ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
        unit_key="frame:000",
        status=ProcessingStage.Status.FAILED,
        failure_code="corrupt_frame",
        finished_at=timezone.now(),
        priority=aggregate.priority,
    )
    succeeded = ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
        unit_key="frame:001",
        status=ProcessingStage.Status.SUCCEEDED,
        finished_at=timezone.now(),
        priority=aggregate.priority,
    )
    with patch(
        "pandect.processing.services._dispatch_stage_task",
        return_value=SimpleNamespace(id="aggregate-task"),
    ) as dispatch:
        assert enqueue_ready_stages(run.pk) >= 1

    aggregate.refresh_from_db()
    assert aggregate.status == ProcessingStage.Status.QUEUED
    assert any(call.args == (aggregate.pk,) for call in dispatch.call_args_list)
    assert failed.status == ProcessingStage.Status.FAILED
    assert succeeded.status == ProcessingStage.Status.SUCCEEDED
    run.refresh_from_db()
    assert run.status == ProcessingRun.Status.RUNNING


@pytest.mark.django_db
def test_keyframe_aggregate_waits_and_preserves_success_when_one_unit_fails(
    tmp_path, django_capture_on_commit_callbacks
):
    with override_settings(MEDIA_ROOT=tmp_path):
        run = create_local_run(make_video_item(), enqueue=False)
        metadata_stage = run.stages.get(kind=ProcessingStage.Kind.VIDEO_METADATA)
        ProcessingStage.objects.filter(pk=metadata_stage.pk).update(
            status=ProcessingStage.Status.SUCCEEDED,
            finished_at=timezone.now(),
        )
        plan = run.stages.get(kind=ProcessingStage.Kind.VIDEO_KEYFRAMES)
        ProcessingStage.objects.filter(pk=plan.pk).update(
            status=ProcessingStage.Status.QUEUED
        )
        plan = claim_stage(plan.pk)
        assert plan is not None
        with django_capture_on_commit_callbacks(execute=False):
            assert complete_stage(
                plan.pk,
                keyframe_plan_outcome(run),
                expected_attempt=plan.attempts,
                observed_source_fingerprint=run.source_fingerprint,
            )

        successful = run.stages.get(
            kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
            unit_key="frame:000",
        )
        ProcessingStage.objects.filter(pk=successful.pk).update(
            status=ProcessingStage.Status.QUEUED
        )
        successful = claim_stage(successful.pk)
        assert successful is not None
        relative = (
            f"derived/{run.catalog_item_id}/{run.pk}/{successful.pk}-frame-000.jpg"
        )
        payload = b"successful-frame"
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        with django_capture_on_commit_callbacks(execute=False):
            assert complete_stage(
                successful.pk,
                StageOutcome(
                    artifacts=(
                        ArtifactSpec(
                            file_name=relative,
                            role=DerivedArtifact.Role.KEYFRAME,
                            mime_type="image/jpeg",
                            sha256=sha256_bytes(payload),
                            byte_size=len(payload),
                            stream_index=0,
                            frame_index=0,
                            source_start_seconds=0,
                            source_end_seconds=0,
                            metadata={
                                "metadata_stripped": True,
                                "selection_reason": "first_frame",
                                "scene_change_score": None,
                            },
                        ),
                    )
                ),
                expected_attempt=successful.attempts,
                observed_source_fingerprint=run.source_fingerprint,
            )

        aggregate = run.stages.get(
            kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
            unit_key="aggregate",
        )
        with patch(
            "pandect.processing.services._dispatch_stage_task",
            return_value=SimpleNamespace(id="queued-unit"),
        ):
            enqueue_ready_stages(run.pk)
        aggregate.refresh_from_db()
        assert aggregate.status == ProcessingStage.Status.PENDING

        failed = run.stages.get(
            kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
            unit_key="frame:001",
        )
        ProcessingStage.objects.filter(pk=failed.pk).update(
            status=ProcessingStage.Status.QUEUED
        )
        failed = claim_stage(failed.pk)
        assert failed is not None
        with django_capture_on_commit_callbacks(execute=False):
            assert fail_stage(
                failed.pk,
                "corrupt_frame",
                expected_attempt=failed.attempts,
            )

        with patch(
            "pandect.processing.services._dispatch_stage_task",
            return_value=SimpleNamespace(id="aggregate-task"),
        ):
            enqueue_ready_stages(run.pk)
        aggregate.refresh_from_db()
        assert aggregate.status == ProcessingStage.Status.QUEUED
        aggregate = claim_stage(aggregate.pk)
        assert aggregate is not None
        outcome = execute_stage(aggregate)
        assert outcome.results[0].data["status"] == "partial"
        assert outcome.results[0].data["successful_units"] == ["frame:000"]
        assert outcome.results[0].data["failed_units"] == [
            {
                "unit_key": "frame:001",
                "status": ProcessingStage.Status.FAILED,
                "failure_code": "corrupt_frame",
            }
        ]
        assert outcome.results[0].data["frames"][0]["sha256"] == sha256_bytes(payload)
        with django_capture_on_commit_callbacks(execute=False):
            assert complete_stage(
                aggregate.pk,
                outcome,
                expected_attempt=aggregate.attempts,
                observed_source_fingerprint=run.source_fingerprint,
            )

    successful.refresh_from_db()
    failed.refresh_from_db()
    assert successful.status == ProcessingStage.Status.SUCCEEDED
    assert failed.status == ProcessingStage.Status.FAILED
    assert DerivedArtifact.objects.filter(stage=successful).count() == 1


@pytest.mark.django_db
def test_reclaimed_attempt_fences_stale_heartbeat_completion_and_failure():
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    stale = claim_stage(stage.pk, lease_seconds=1)
    assert stale is not None
    reconciliation_time = timezone.now()
    ProcessingStage.objects.filter(pk=stage.pk).update(
        lease_expires_at=reconciliation_time - timedelta(seconds=1)
    )
    with patch(
        "pandect.processing.services._dispatch_stage_task",
        return_value=SimpleNamespace(id="replacement-delivery"),
    ):
        report = reconcile_stages(now=reconciliation_time)
    assert report.running_recovered == 1

    current = claim_stage(stage.pk)
    assert current is not None
    assert current.attempts == stale.attempts + 1
    current_lease = current.lease_expires_at
    outcome = StageOutcome(
        results=(
            ResultSpec(
                result_kind=ProcessingResult.Kind.MESSAGE_TEXT_PROFILE,
                evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
                data={"attempt": "current"},
                processor_name="pandect.text",
                processor_version="1",
            ),
        )
    )

    assert not heartbeat_stage(stage.pk, expected_attempt=stale.attempts)
    assert not complete_stage(
        stage.pk,
        outcome,
        expected_attempt=stale.attempts,
    )
    assert not fail_stage(
        stage.pk,
        "stale_worker_failure",
        expected_attempt=stale.attempts,
    )
    assert not skip_stage(
        stage.pk,
        "unsupported_pixel_format",
        expected_attempt=stale.attempts,
    )
    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.RUNNING
    assert stage.attempts == current.attempts
    assert stage.lease_expires_at == current_lease
    assert not stage.results.exists()

    assert heartbeat_stage(stage.pk, expected_attempt=current.attempts)
    assert complete_stage(
        stage.pk,
        outcome,
        expected_attempt=current.attempts,
    )
    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.SUCCEEDED
    assert stage.results.get().data == {"attempt": "current"}


@pytest.mark.django_db
def test_reconciliation_cleans_only_the_crashed_attempt_workspace(tmp_path):
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    stale = claim_stage(stage.pk, lease_seconds=1)
    assert stale is not None
    stage_root = tmp_path / str(run.pk) / str(stage.pk)
    crashed_workspace = stage_root / f"attempt-{stale.attempts}"
    crashed_workspace.mkdir(parents=True)
    (crashed_workspace / "rendered.png").write_bytes(b"private scratch")
    newer_workspace = stage_root / f"attempt-{stale.attempts + 1}"
    newer_workspace.mkdir()
    (newer_workspace / "waveform.wav").write_bytes(b"newer attempt")
    reconciliation_time = timezone.now()
    ProcessingStage.objects.filter(pk=stage.pk).update(
        lease_expires_at=reconciliation_time - timedelta(seconds=1)
    )

    with (
        override_settings(WORK_ROOT=tmp_path),
        patch(
            "pandect.processing.services._dispatch_stage_task",
            return_value=SimpleNamespace(id="replacement-delivery"),
        ),
    ):
        report = reconcile_stages(now=reconciliation_time)

    assert report.running_recovered == 1
    assert not crashed_workspace.exists()
    assert (newer_workspace / "waveform.wav").read_bytes() == b"newer attempt"
    current = claim_stage(stage.pk)
    assert current is not None
    assert current.attempts == stale.attempts + 1


@pytest.mark.django_db
def test_reconciliation_cleans_terminal_expired_attempt_workspace(tmp_path):
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED,
        max_attempts=1,
    )
    terminal = claim_stage(stage.pk, lease_seconds=1)
    assert terminal is not None
    workspace = tmp_path / str(run.pk) / str(stage.pk) / f"attempt-{terminal.attempts}"
    workspace.mkdir(parents=True)
    (workspace / "partial.wav").write_bytes(b"private scratch")
    reconciliation_time = timezone.now()
    ProcessingStage.objects.filter(pk=stage.pk).update(
        lease_expires_at=reconciliation_time - timedelta(seconds=1)
    )

    with override_settings(WORK_ROOT=tmp_path):
        report = reconcile_stages(now=reconciliation_time)

    assert report.running_failed == 1
    assert not workspace.exists()
    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.FAILED


def test_attempt_cleanup_rejects_symlink_to_newer_workspace(tmp_path):
    run_id = uuid.uuid4()
    stage_id = uuid.uuid4()
    stage_root = tmp_path / str(run_id) / str(stage_id)
    newer_workspace = stage_root / "attempt-2"
    newer_workspace.mkdir(parents=True)
    (newer_workspace / "live.wav").write_bytes(b"live")
    stale_link = stage_root / "attempt-1"
    stale_link.symlink_to(newer_workspace, target_is_directory=True)

    with override_settings(WORK_ROOT=tmp_path):
        assert not clean_attempt_workspace(run_id, stage_id, 1)

    assert stale_link.is_symlink()
    assert (newer_workspace / "live.wav").read_bytes() == b"live"


@pytest.mark.django_db
def test_completion_persists_identity_observations_in_the_same_transition():
    run = create_local_run(make_text_item("Writing sample"), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    claimed = claim_stage(stage.pk)
    assert claimed is not None
    embedding = encode_embedding((1.0, 0.0))

    assert complete_stage(
        stage.pk,
        StageOutcome(
            identity_observations=(
                IdentityObservationSpec(
                    modality=IdentityObservation.Modality.WRITING,
                    unit_key="body:0-14",
                    source_location={"start": 0, "end": 14},
                    source_sha256=run.source_fingerprint,
                    embedding=embedding,
                    embedding_dimension=2,
                    feature_space="writing-style-v1",
                    processor_name="pandect.writing",
                    processor_version="1",
                    preprocessing_version="1",
                    sample_word_count=2,
                ),
            )
        ),
        expected_attempt=claimed.attempts,
        observed_source_fingerprint=run.source_fingerprint,
    )

    observation = IdentityObservation.objects.get(catalog_item=run.catalog_item)
    assert bytes(observation.embedding) == embedding
    assert observation.source_sha256 == run.source_fingerprint
    assert observation.provenance == {
        "processing_run_id": str(run.pk),
        "processing_stage_id": str(stage.pk),
    }


@pytest.mark.django_db
def test_completion_persists_ranked_identity_candidates_atomically():
    run, stage = make_claimed_identity_stage()
    observation = make_writing_observation(
        run.catalog_item,
        source_sha256=run.source_fingerprint,
    )
    first = Subject.objects.create(label="First candidate")
    second = Subject.objects.create(label="Second candidate")
    candidate_set_id = uuid.uuid4()

    assert complete_stage(
        stage.pk,
        StageOutcome(
            identity_candidates=(
                IdentityCandidateSpec(
                    observation_id=observation.pk,
                    subject_id=first.pk,
                    candidate_set_id=candidate_set_id,
                    rank=1,
                    similarity_score=0.82,
                    gallery_fingerprint="c" * 64,
                    reference_count=4,
                    calibration_revision="raw-cosine-v1",
                ),
                IdentityCandidateSpec(
                    observation_id=observation.pk,
                    subject_id=second.pk,
                    candidate_set_id=candidate_set_id,
                    rank=2,
                    similarity_score=0.61,
                    gallery_fingerprint="c" * 64,
                    reference_count=3,
                    calibration_revision="raw-cosine-v1",
                ),
            )
        ),
        expected_attempt=stage.attempts,
        observed_source_fingerprint=run.source_fingerprint,
    )

    candidates = list(IdentityCandidate.objects.order_by("rank"))
    assert [candidate.subject_id for candidate in candidates] == [first.pk, second.pk]
    assert [candidate.similarity_score for candidate in candidates] == [0.82, 0.61]
    assert all(candidate.modality == observation.modality for candidate in candidates)
    assert all(
        candidate.feature_space == observation.feature_space for candidate in candidates
    )
    assert all(
        candidate.decision == IdentityCandidate.Decision.SUGGESTED
        for candidate in candidates
    )
    assert all(candidate.producing_run_id == run.pk for candidate in candidates)
    assert all(candidate.producing_stage_id == stage.pk for candidate in candidates)


@pytest.mark.django_db
def test_wrong_item_candidate_rolls_back_all_stage_outputs():
    run, stage = make_claimed_identity_stage("Run item")
    other_item = make_text_item("Other item")
    observation = make_writing_observation(
        other_item,
        source_sha256="d" * 64,
    )
    subject = Subject.objects.create(label="Candidate")
    outcome = StageOutcome(
        results=(
            ResultSpec(
                result_kind=ProcessingResult.Kind.AUTHORSHIP_CANDIDATES,
                evidence_tier=ProcessingResult.EvidenceTier.MODEL_GENERATED,
                data={"candidate_count": 1},
                processor_name="pandect.writing",
                processor_version="1",
            ),
        ),
        identity_candidates=(
            IdentityCandidateSpec(
                observation_id=observation.pk,
                subject_id=subject.pk,
                candidate_set_id=uuid.uuid4(),
                rank=1,
                similarity_score=0.75,
                gallery_fingerprint="e" * 64,
                reference_count=3,
                calibration_revision="raw-cosine-v1",
            ),
        ),
    )

    with pytest.raises(ValidationError, match="must belong to the processing run"):
        complete_stage(stage.pk, outcome, expected_attempt=stage.attempts)

    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.RUNNING
    assert not ProcessingResult.objects.filter(stage=stage).exists()
    assert not IdentityCandidate.objects.exists()


@pytest.mark.django_db
def test_duplicate_candidate_rank_rolls_back_the_candidate_set():
    run, stage = make_claimed_identity_stage()
    observation = make_writing_observation(
        run.catalog_item,
        source_sha256=run.source_fingerprint,
    )
    first = Subject.objects.create(label="First")
    second = Subject.objects.create(label="Second")
    candidate_set_id = uuid.uuid4()
    outcome = StageOutcome(
        identity_candidates=tuple(
            IdentityCandidateSpec(
                observation_id=observation.pk,
                subject_id=subject.pk,
                candidate_set_id=candidate_set_id,
                rank=1,
                similarity_score=score,
                gallery_fingerprint="f" * 64,
                reference_count=2,
                calibration_revision="raw-cosine-v1",
            )
            for subject, score in ((first, 0.8), (second, 0.7))
        )
    )

    with pytest.raises(IntegrityError):
        complete_stage(stage.pk, outcome, expected_attempt=stage.attempts)

    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.RUNNING
    assert not IdentityCandidate.objects.exists()


@pytest.mark.django_db
def test_candidate_rank_outside_top_five_is_rejected_atomically():
    run, stage = make_claimed_identity_stage()
    observation = make_writing_observation(
        run.catalog_item,
        source_sha256=run.source_fingerprint,
    )
    subject = Subject.objects.create(label="Candidate")
    outcome = StageOutcome(
        identity_candidates=(
            IdentityCandidateSpec(
                observation_id=observation.pk,
                subject_id=subject.pk,
                candidate_set_id=uuid.uuid4(),
                rank=6,
                similarity_score=0.5,
                gallery_fingerprint="a" * 64,
                reference_count=2,
                calibration_revision="raw-cosine-v1",
            ),
        )
    )

    with pytest.raises(ValidationError):
        complete_stage(stage.pk, outcome, expected_attempt=stage.attempts)

    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.RUNNING
    assert not IdentityCandidate.objects.exists()


@pytest.mark.django_db
def test_retryable_failure_is_bounded_and_failure_text_is_sanitized():
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    claimed = claim_stage(stage.pk)
    assert claimed is not None

    assert fail_stage(
        stage.pk,
        "tool_timed_out",
        "Safe\x00detail" + "x" * 600,
        expected_attempt=claimed.attempts,
        retryable=True,
    )
    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.PENDING
    assert stage.attempts == 1
    assert "\x00" not in stage.failure_detail
    assert len(stage.failure_detail) == 512


@pytest.mark.django_db
def test_failed_dependency_is_skipped_and_run_is_failed():
    run = create_local_run(make_text_item(), enqueue=False)
    extraction = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=extraction.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    claimed = claim_stage(extraction.pk)
    assert claimed is not None
    assert fail_stage(
        extraction.pk,
        "invalid_input",
        expected_attempt=claimed.attempts,
    )

    enqueue_ready_stages(run.pk)

    statuses = dict(run.stages.values_list("kind", "status"))
    assert statuses == {
        ProcessingStage.Kind.TEXT_EXTRACTION: ProcessingStage.Status.FAILED,
        ProcessingStage.Kind.WRITING_STYLE: ProcessingStage.Status.SKIPPED,
        ProcessingStage.Kind.WRITING_IDENTITY: ProcessingStage.Status.SKIPPED,
    }
    run.refresh_from_db()
    assert run.status == ProcessingRun.Status.FAILED


@pytest.mark.django_db
def test_new_fingerprint_supersedes_unfinished_run():
    item = make_text_item("before")
    first = create_local_run(item, enqueue=False)
    item.message.body = "after"
    item.message.save()

    second = create_local_run(item, enqueue=False)

    first.refresh_from_db()
    assert first.status == ProcessingRun.Status.SUPERSEDED
    assert first.stages.filter(status=ProcessingStage.Status.SUPERSEDED).count() == 3
    assert second.source_fingerprint != first.source_fingerprint


@pytest.mark.django_db
def test_active_and_replacement_runs_bind_digest_and_byte_length(monkeypatch):
    item = make_text_item("same digest, original length")
    original = create_local_run(item, enqueue=False)
    collision = FileFingerprint(
        sha256=original.source_fingerprint,
        size_bytes=original.source_size + 1,
    )
    monkeypatch.setattr(
        "pandect.processing.services.source_fingerprint",
        lambda _item: collision,
    )

    colliding_run = create_local_run(item, enqueue=False)

    original.refresh_from_db()
    assert original.status == ProcessingRun.Status.SUPERSEDED
    assert colliding_run.source_fingerprint == original.source_fingerprint
    assert colliding_run.source_size == original.source_size + 1

    replacement = supersede_and_replace_local_run(original)

    assert replacement is not None
    assert replacement.pk == colliding_run.pk


@pytest.mark.django_db
def test_source_change_supersession_rolls_back_if_replacement_rows_fail():
    item = make_text_item("source before replacement")
    original = create_local_run(item, enqueue=False)
    item.message.body = "source after replacement"
    item.message.save(update_fields={"body"})

    with (
        patch.object(
            QuerySet,
            "bulk_create",
            autospec=True,
            side_effect=RuntimeError("simulated replacement write failure"),
        ),
        pytest.raises(RuntimeError, match="replacement write failure"),
    ):
        supersede_and_replace_local_run(original.pk)

    original.refresh_from_db()
    assert original.status == ProcessingRun.Status.PENDING
    assert original.finished_at is None
    assert set(original.stages.values_list("status", flat=True)) == {
        ProcessingStage.Status.PENDING
    }
    assert item.processing_runs.count() == 1


@pytest.mark.django_db
def test_reconcile_repairs_legacy_superseded_run_without_replacement(
    django_capture_on_commit_callbacks,
):
    item = make_text_item("legacy source")
    original = create_local_run(item, enqueue=False)
    item.message.body = "current source"
    item.message.save(update_fields={"body"})
    processing_services.mark_run_superseded(original.pk)

    with (
        django_capture_on_commit_callbacks(execute=True),
        patch(
            "pandect.processing.services._dispatch_stage_task",
            return_value=SimpleNamespace(id="replacement-delivery"),
        ),
    ):
        report = reconcile_stages(now=timezone.now(), limit=10)

    replacement = item.processing_runs.exclude(pk=original.pk).get()
    assert report.replacements_repaired == 1
    assert replacement.configuration["replacement_for_run"] == str(original.pk)
    assert replacement.configuration["reason"] == "source_changed_during_processing"
    assert replacement.source_fingerprint != original.source_fingerprint
    assert replacement.stages.filter(status=ProcessingStage.Status.QUEUED).count() == 1

    with patch(
        "pandect.processing.services._dispatch_stage_task",
        return_value=SimpleNamespace(id="unused-delivery"),
    ):
        second_report = reconcile_stages(now=timezone.now(), limit=10)
    assert second_report.replacements_repaired == 0
    assert item.processing_runs.count() == 2


@pytest.mark.django_db
def test_reconcile_advances_past_legacy_rows_with_an_existing_replacement(
    django_capture_on_commit_callbacks,
):
    gap_item = make_text_item("unrepaired source")
    gap_run = create_local_run(gap_item, enqueue=False)
    gap_item.message.body = "unrepaired current source"
    gap_item.message.save(update_fields={"body"})
    processing_services.mark_run_superseded(gap_run.pk)

    satisfied_item = make_text_item("already handled source")
    satisfied_run = create_local_run(satisfied_item, enqueue=False)
    satisfied_item.message.body = "already handled current source"
    satisfied_item.message.save(update_fields={"body"})
    current_run = create_local_run(satisfied_item, enqueue=False)
    ProcessingStage.objects.filter(
        run=satisfied_run,
        status=ProcessingStage.Status.SUPERSEDED,
    ).update(failure_code=processing_services.SOURCE_CHANGED_FAILURE_CODE)
    ordering_anchor = timezone.now()
    ProcessingRun.objects.filter(pk=gap_run.pk).update(
        updated_at=ordering_anchor - timedelta(days=1)
    )
    ProcessingRun.objects.filter(pk=satisfied_run.pk).update(updated_at=ordering_anchor)

    first = reconcile_stages(now=timezone.now(), limit=1)

    assert first.replacements_repaired == 0
    assert satisfied_item.processing_runs.count() == 2
    assert current_run.status == ProcessingRun.Status.PENDING
    assert not satisfied_run.stages.filter(
        failure_code=processing_services.SOURCE_CHANGED_FAILURE_CODE
    ).exists()

    with (
        django_capture_on_commit_callbacks(execute=True),
        patch(
            "pandect.processing.services._dispatch_stage_task",
            return_value=SimpleNamespace(id="legacy-gap-delivery"),
        ),
    ):
        second = reconcile_stages(now=timezone.now(), limit=1)

    assert second.replacements_repaired == 1
    assert gap_item.processing_runs.count() == 2


@pytest.mark.django_db
def test_reconcile_recovers_stale_queue_and_exhausted_worker_lease():
    first = create_local_run(make_text_item("queued"), enqueue=False)
    queued = first.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    stale = timezone.now() - timedelta(hours=1)
    ProcessingStage.objects.filter(pk=queued.pk).update(
        status=ProcessingStage.Status.QUEUED,
        queued_at=stale,
    )

    second = create_local_run(make_text_item("running"), enqueue=False)
    running = second.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=running.pk).update(
        status=ProcessingStage.Status.RUNNING,
        attempts=2,
        max_attempts=2,
        started_at=stale,
        lease_expires_at=stale,
    )

    with patch(
        "pandect.processing.services._dispatch_stage_task",
        return_value=SimpleNamespace(id="recovered"),
    ):
        report = reconcile_stages(now=timezone.now(), limit=10)

    queued.refresh_from_db()
    running.refresh_from_db()
    assert report.queued_recovered == 1
    assert report.running_failed == 1
    assert queued.status == ProcessingStage.Status.QUEUED
    assert running.status == ProcessingStage.Status.FAILED


@pytest.mark.django_db
def test_reconcile_requeues_only_stale_keyframe_unit_not_successful_sibling():
    run = create_local_run(make_video_item(), enqueue=False)
    now = timezone.now()
    stale = now - timedelta(hours=1)
    metadata_stage = run.stages.get(kind=ProcessingStage.Kind.VIDEO_METADATA)
    plan = run.stages.get(kind=ProcessingStage.Kind.VIDEO_KEYFRAMES)
    ProcessingStage.objects.filter(pk=metadata_stage.pk).update(
        status=ProcessingStage.Status.SUCCEEDED,
        finished_at=stale,
    )
    ProcessingStage.objects.filter(pk=plan.pk).update(
        status=ProcessingStage.Status.SUCCEEDED,
        finished_at=stale,
    )
    successful = ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
        unit_key="frame:000",
        status=ProcessingStage.Status.SUCCEEDED,
        priority=20,
        attempts=1,
        finished_at=stale,
    )
    stale_unit = ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
        unit_key="frame:001",
        status=ProcessingStage.Status.QUEUED,
        priority=22,
        attempts=1,
        queued_at=stale,
    )
    ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
        unit_key="aggregate",
        priority=25,
    )

    with patch(
        "pandect.processing.services._dispatch_stage_task",
        return_value=SimpleNamespace(id="recovered-keyframe"),
    ) as dispatch:
        report = reconcile_stages(now=now, limit=10)

    successful.refresh_from_db()
    stale_unit.refresh_from_db()
    assert report.queued_recovered == 1
    assert successful.status == ProcessingStage.Status.SUCCEEDED
    assert successful.attempts == 1
    assert stale_unit.status == ProcessingStage.Status.QUEUED
    assert stale_unit.attempts == 1
    assert any(call.args == (stale_unit.pk,) for call in dispatch.call_args_list)
