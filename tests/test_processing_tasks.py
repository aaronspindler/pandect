import hashlib
from datetime import timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from pandect.catalog.models import CatalogItem
from pandect.messages.models import Message, TextMessage
from pandect.photos.models import Image
from pandect.processing import execution
from pandect.processing.execution import _publish_artifact, _storage_paths
from pandect.processing.extractors.errors import (
    ToolFailedError,
    UnsupportedPixelFormatError,
)
from pandect.processing.models import ProcessingResult, ProcessingRun, ProcessingStage
from pandect.processing.services import (
    ArtifactSpec,
    ResultSpec,
    StageOutcome,
    claim_stage,
    complete_stage,
    create_local_run,
    fail_stage,
    reconcile_stages,
)
from pandect.processing.tasks import _clean_uncommitted_artifacts, run_processing_stage


def make_text_item(body: str = "Task input") -> CatalogItem:
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.TEXT_MESSAGE)
    message = Message.objects.create(catalog_item=item, body=body)
    TextMessage.objects.create(message=message)
    return item


@pytest.mark.django_db
def test_huey_stage_task_uses_durable_state_and_duplicate_delivery_is_safe():
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    outcome = StageOutcome(
        results=(
            ResultSpec(
                result_kind=ProcessingResult.Kind.MESSAGE_TEXT_PROFILE,
                evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
                data={"counts": {"words": 2}},
                processor_name="pandect.text",
                processor_version="1",
            ),
        )
    )

    with patch("pandect.processing.tasks.execute_stage", return_value=outcome):
        run_processing_stage(str(stage.pk))
        run_processing_stage(str(stage.pk))

    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.SUCCEEDED
    assert stage.attempts == 1
    assert stage.results.count() == 1


@pytest.mark.django_db
def test_handler_exception_after_first_publication_uses_durable_journal(tmp_path):
    payload = b"first-of-several-derived-files"
    digest = hashlib.sha256(payload).hexdigest()
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    published = {}

    def processor(claimed_stage):
        temporary, final, _ = _storage_paths(claimed_stage, "first.bin")
        temporary.write_bytes(payload)
        assert _publish_artifact(
            temporary,
            final,
            stage=claimed_stage,
            expected_sha256=digest,
            expected_size_bytes=len(payload),
        )
        published["final"] = final
        raise RuntimeError("second artifact extraction failed")

    with (
        override_settings(
            MEDIA_ROOT=tmp_path / "media",
            WORK_ROOT=tmp_path / "work",
        ),
        patch("pandect.processing.tasks.execute_stage", side_effect=processor),
    ):
        run_processing_stage(str(stage.pk))

    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.FAILED
    assert not published["final"].exists()
    assert not stage.artifact_publications.exists()
    assert not (
        tmp_path / "work" / str(run.pk) / str(stage.pk) / f"attempt-{stage.attempts}"
    ).exists()


@pytest.mark.django_db
def test_reconciliation_recovers_process_death_after_hard_link(monkeypatch, tmp_path):
    payload = b"linked-before-process-death"
    digest = hashlib.sha256(payload).hexdigest()
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    claimed = claim_stage(stage.pk, lease_seconds=1)
    assert claimed is not None

    with override_settings(
        MEDIA_ROOT=tmp_path / "media",
        WORK_ROOT=tmp_path / "work",
    ):
        temporary, final, _ = _storage_paths(claimed, "crashed.bin")
        temporary.write_bytes(payload)
        real_fingerprint = execution.fingerprint_file

        def crash_after_link(path):
            if Path(path) == final:
                raise SystemExit("simulated process death")
            return real_fingerprint(path)

        monkeypatch.setattr(execution, "fingerprint_file", crash_after_link)
        with pytest.raises(SystemExit, match="simulated process death"):
            _publish_artifact(
                temporary,
                final,
                stage=claimed,
                expected_sha256=digest,
                expected_size_bytes=len(payload),
            )
        monkeypatch.setattr(execution, "fingerprint_file", real_fingerprint)

        publication = stage.artifact_publications.get(attempt=claimed.attempts)
        assert final.exists()
        assert temporary.exists()
        assert final.stat().st_ino == publication.source_inode
        assert temporary.stat().st_ino == publication.source_inode

        reconciliation_time = timezone.now()
        ProcessingStage.objects.filter(pk=stage.pk).update(
            lease_expires_at=reconciliation_time - timedelta(seconds=1)
        )

        def dispatch_after_cleanup(stage_id):
            assert stage_id == stage.pk
            assert not final.exists()
            assert not stage.artifact_publications.filter(
                attempt=claimed.attempts
            ).exists()
            return SimpleNamespace(id="recovered-delivery")

        with patch(
            "pandect.processing.services._dispatch_stage_task",
            side_effect=dispatch_after_cleanup,
        ):
            report = reconcile_stages(now=reconciliation_time)

    stage.refresh_from_db()
    assert report.running_recovered == 1
    assert stage.status == ProcessingStage.Status.QUEUED
    assert not final.exists()
    assert not stage.artifact_publications.exists()
    assert not temporary.parent.exists()


@pytest.mark.django_db
def test_terminal_retry_cleans_a_prior_attempt_publication_and_workspace(tmp_path):
    payload = b"published-by-crashed-attempt"
    digest = hashlib.sha256(payload).hexdigest()
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )

    with override_settings(
        MEDIA_ROOT=tmp_path / "media",
        WORK_ROOT=tmp_path / "work",
    ):
        crashed = claim_stage(stage.pk)
        assert crashed is not None
        temporary, final, _relative = _storage_paths(crashed, "orphan.bin")
        temporary.write_bytes(payload)
        assert _publish_artifact(
            temporary,
            final,
            stage=crashed,
            expected_sha256=digest,
            expected_size_bytes=len(payload),
        )

        # Model the durable state after a retry was exposed by an older
        # runtime, leaving attempt 1's journal behind for attempt 2 to clean.
        ProcessingStage.objects.filter(pk=stage.pk).update(
            status=ProcessingStage.Status.QUEUED,
            lease_expires_at=None,
        )
        with patch(
            "pandect.processing.tasks.execute_stage",
            side_effect=RuntimeError("attempt two terminates"),
        ):
            run_processing_stage(str(stage.pk))

    stage.refresh_from_db()
    assert stage.attempts == 2
    assert stage.status == ProcessingStage.Status.FAILED
    assert not stage.artifact_publications.exists()
    assert not final.exists()
    assert not temporary.parent.exists()


@pytest.mark.django_db
def test_transient_retry_is_dispatched_only_after_publication_cleanup(
    tmp_path,
    django_capture_on_commit_callbacks,
):
    payload = b"transient-attempt-publication"
    digest = hashlib.sha256(payload).hexdigest()
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    claimed = claim_stage(stage.pk)
    assert claimed is not None

    with override_settings(
        MEDIA_ROOT=tmp_path / "media",
        WORK_ROOT=tmp_path / "work",
    ):
        temporary, final, _relative = _storage_paths(claimed, "transient.bin")
        temporary.write_bytes(payload)
        assert _publish_artifact(
            temporary,
            final,
            stage=claimed,
            expected_sha256=digest,
            expected_size_bytes=len(payload),
        )

        def dispatch_after_cleanup(stage_id):
            assert stage_id == stage.pk
            assert not final.exists()
            assert not stage.artifact_publications.exists()
            assert not temporary.parent.exists()
            return SimpleNamespace(id="retry-delivery")

        with (
            patch(
                "pandect.processing.services._dispatch_stage_task",
                side_effect=dispatch_after_cleanup,
            ),
            django_capture_on_commit_callbacks(execute=True),
        ):
            assert fail_stage(
                stage.pk,
                "transient_tool_failure",
                expected_attempt=claimed.attempts,
                retryable=True,
            )

    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.QUEUED
    assert stage.attempts == claimed.attempts
    assert not stage.artifact_publications.exists()


@pytest.mark.django_db
def test_retry_recovers_artifact_published_before_stage_commit(
    tmp_path,
    django_capture_on_commit_callbacks,
):
    """A crash-window file becomes durable evidence on the retried attempt."""
    payload = b"deterministic-derived-evidence"
    digest = hashlib.sha256(payload).hexdigest()
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )

    with override_settings(
        MEDIA_ROOT=tmp_path / "media",
        WORK_ROOT=tmp_path / "work",
    ):
        interrupted_stage = claim_stage(stage.pk)
        assert interrupted_stage is not None
        temporary, final, relative = _storage_paths(
            interrupted_stage,
            "recovered.bin",
        )
        temporary.write_bytes(payload)
        _publish_artifact(
            temporary,
            final,
            stage=interrupted_stage,
            expected_sha256=digest,
            expected_size_bytes=len(payload),
        )

        # This is the state reconciliation produces after the worker lease from
        # the interrupted, uncommitted attempt expires.
        ProcessingStage.objects.filter(pk=stage.pk).update(
            status=ProcessingStage.Status.QUEUED,
            lease_expires_at=None,
        )

        def retry_processor(retried_stage):
            retry_temporary, retry_final, retry_relative = _storage_paths(
                retried_stage,
                "recovered.bin",
            )
            assert retry_final == final
            assert retry_relative == relative
            retry_temporary.write_bytes(payload)
            _publish_artifact(
                retry_temporary,
                retry_final,
                stage=retried_stage,
                expected_sha256=digest,
                expected_size_bytes=len(payload),
            )
            return StageOutcome(
                artifacts=(
                    ArtifactSpec(
                        file_name=retry_relative,
                        role="other",
                        mime_type="application/octet-stream",
                        sha256=digest,
                        byte_size=len(payload),
                    ),
                )
            )

        with django_capture_on_commit_callbacks(execute=True):
            with patch(
                "pandect.processing.tasks.execute_stage",
                side_effect=retry_processor,
            ):
                run_processing_stage(str(stage.pk))

    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.SUCCEEDED
    assert stage.attempts == 2
    artifact = stage.artifacts.get()
    assert artifact.file.name == relative
    assert artifact.sha256 == digest
    assert artifact.byte_size == len(payload)
    assert final.read_bytes() == payload
    assert not stage.artifact_publications.exists()
    assert not temporary.parent.exists()


@pytest.mark.django_db
def test_source_change_after_publication_removes_only_uncommitted_attempt_file(
    tmp_path,
):
    payload = b"derived-from-original-source"
    digest = hashlib.sha256(payload).hexdigest()
    item = make_text_item("original source")
    run = create_local_run(item, enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    published = {}

    def processor(claimed_stage):
        temporary, final, relative = _storage_paths(claimed_stage, "changed.bin")
        temporary.write_bytes(payload)
        publication_created = _publish_artifact(
            temporary,
            final,
            stage=claimed_stage,
            expected_sha256=digest,
            expected_size_bytes=len(payload),
        )
        published["final"] = final
        item.message.body = "changed source"
        item.message.save(update_fields=["body"])
        return StageOutcome(
            artifacts=(
                ArtifactSpec(
                    file_name=relative,
                    role="other",
                    mime_type="application/octet-stream",
                    sha256=digest,
                    byte_size=len(payload),
                    publication_created=publication_created,
                ),
            )
        )

    with (
        override_settings(
            MEDIA_ROOT=tmp_path / "media",
            WORK_ROOT=tmp_path / "work",
        ),
        patch("pandect.processing.tasks.execute_stage", side_effect=processor),
        patch(
            "pandect.processing.services._dispatch_stage_task",
            return_value=SimpleNamespace(id="replacement"),
        ),
    ):
        run_processing_stage(str(stage.pk))

    run.refresh_from_db()
    stage.refresh_from_db()
    assert run.status == ProcessingRun.Status.SUPERSEDED
    assert stage.status == ProcessingStage.Status.SUPERSEDED
    assert not published["final"].exists()
    assert not stage.artifacts.exists()


@pytest.mark.django_db
def test_rejected_attempt_preserves_preexisting_and_mismatched_artifacts(tmp_path):
    payload = b"expected-derived-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    paths = {}

    def processor(claimed_stage):
        temporary, final, relative = _storage_paths(claimed_stage, "preserved.bin")
        final.write_bytes(payload)
        temporary.write_bytes(payload)
        publication_created = _publish_artifact(
            temporary,
            final,
            stage=claimed_stage,
            expected_sha256=digest,
            expected_size_bytes=len(payload),
        )
        assert publication_created is False
        mismatch_temporary, mismatch_final, mismatch_relative = _storage_paths(
            claimed_stage,
            "mismatched.bin",
        )
        mismatch_temporary.write_bytes(payload)
        mismatch_created = _publish_artifact(
            mismatch_temporary,
            mismatch_final,
            stage=claimed_stage,
            expected_sha256=digest,
            expected_size_bytes=len(payload),
        )
        assert mismatch_created is True
        mismatch_final.write_bytes(b"changed-after-publication")
        # A stale claim makes complete_stage reject this outcome. The exact
        # preexisting file must not be treated as this attempt's publication.
        ProcessingStage.objects.filter(pk=claimed_stage.pk).update(
            status=ProcessingStage.Status.SUPERSEDED
        )
        paths["final"] = final
        paths["mismatch_final"] = mismatch_final
        return StageOutcome(
            artifacts=(
                ArtifactSpec(
                    file_name=relative,
                    role="other",
                    mime_type="application/octet-stream",
                    sha256=digest,
                    byte_size=len(payload),
                    publication_created=publication_created,
                ),
                ArtifactSpec(
                    file_name=mismatch_relative,
                    role="other",
                    mime_type="application/octet-stream",
                    sha256=digest,
                    byte_size=len(payload),
                    publication_created=mismatch_created,
                ),
            )
        )

    with (
        override_settings(
            MEDIA_ROOT=tmp_path / "media",
            WORK_ROOT=tmp_path / "work",
        ),
        patch("pandect.processing.tasks.execute_stage", side_effect=processor),
    ):
        run_processing_stage(str(stage.pk))

    assert paths["final"].read_bytes() == payload
    assert paths["mismatch_final"].read_bytes() == b"changed-after-publication"
    assert not stage.artifacts.exists()


@pytest.mark.django_db
def test_stale_attempt_never_removes_file_a_newer_attempt_may_use(tmp_path):
    payload = b"shared-deterministic-output"
    digest = hashlib.sha256(payload).hexdigest()
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    paths = {}

    def processor(claimed_stage):
        temporary, final, relative = _storage_paths(claimed_stage, "newer.bin")
        temporary.write_bytes(payload)
        publication_created = _publish_artifact(
            temporary,
            final,
            stage=claimed_stage,
            expected_sha256=digest,
            expected_size_bytes=len(payload),
        )
        assert publication_created is True
        paths["final"] = final
        ProcessingStage.objects.filter(pk=claimed_stage.pk).update(
            status=ProcessingStage.Status.RUNNING,
            attempts=claimed_stage.attempts + 1,
        )
        return StageOutcome(
            artifacts=(
                ArtifactSpec(
                    file_name=relative,
                    role="other",
                    mime_type="application/octet-stream",
                    sha256=digest,
                    byte_size=len(payload),
                    publication_created=publication_created,
                ),
            )
        )

    with (
        override_settings(
            MEDIA_ROOT=tmp_path / "media",
            WORK_ROOT=tmp_path / "work",
        ),
        patch("pandect.processing.tasks.execute_stage", side_effect=processor),
    ):
        run_processing_stage(str(stage.pk))

    stage.refresh_from_db()
    assert stage.attempts == 2
    assert stage.status == ProcessingStage.Status.RUNNING
    assert paths["final"].read_bytes() == payload
    assert not stage.artifacts.exists()


@pytest.mark.django_db
def test_succeeded_stage_never_cleans_a_durable_artifact(tmp_path):
    payload = b"committed-evidence"
    digest = hashlib.sha256(payload).hexdigest()
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    claimed = claim_stage(stage.pk)
    assert claimed is not None

    with override_settings(
        MEDIA_ROOT=tmp_path / "media",
        WORK_ROOT=tmp_path / "work",
    ):
        temporary, final, relative = _storage_paths(claimed, "committed.bin")
        temporary.write_bytes(payload)
        publication_created = _publish_artifact(
            temporary,
            final,
            stage=claimed,
            expected_sha256=digest,
            expected_size_bytes=len(payload),
        )
        outcome = StageOutcome(
            artifacts=(
                ArtifactSpec(
                    file_name=relative,
                    role="other",
                    mime_type="application/octet-stream",
                    sha256=digest,
                    byte_size=len(payload),
                    publication_created=publication_created,
                ),
            )
        )
        assert complete_stage(
            claimed.pk,
            outcome,
            expected_attempt=claimed.attempts,
            observed_source_fingerprint=run.source_fingerprint,
        )

        _clean_uncommitted_artifacts(claimed, outcome)

    assert final.read_bytes() == payload
    artifact = stage.artifacts.get()
    assert artifact.file.name == relative


@pytest.mark.django_db
def test_reconcile_management_command_reports_bounded_work():
    output = StringIO()

    with patch(
        "pandect.processing.management.commands.reconcile_processing."
        "finalize_ready_remote_aggregates",
        return_value=2,
    ) as finalize_remote:
        call_command(
            "reconcile_processing",
            limit=5,
            queued_timeout=60,
            stdout=output,
        )

    finalize_remote.assert_called_once_with(limit=5)
    assert "Processing reconciliation complete" in output.getvalue()
    assert "source replacements repaired=0" in output.getvalue()
    assert "pending runs examined=0" in output.getvalue()
    assert "remote aggregates scheduled=2" in output.getvalue()


@pytest.mark.django_db
def test_task_marks_a_deleted_canonical_source_with_a_sanitized_failure():
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.IMAGE)
    image = Image.objects.create(
        catalog_item=item,
        file=SimpleUploadedFile("sensitive-name.bin", b"canonical"),
    )
    run = create_local_run(item, enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.IMAGE_METADATA)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    image.file.delete(save=False)

    run_processing_stage(str(stage.pk))

    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.FAILED
    assert stage.failure_code == "source_unavailable"
    assert "sensitive-name" not in stage.failure_detail


@pytest.mark.django_db
def test_task_skips_unsupported_image_pixels_without_treating_them_as_failure():
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.IMAGE)
    Image.objects.create(
        catalog_item=item,
        file=SimpleUploadedFile("unsupported.heic", b"canonical-image-evidence"),
        mime_type="image/heic",
    )
    run = create_local_run(item, enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.IMAGE_OCR)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )

    with (
        patch(
            "pandect.processing.tasks.execute_stage",
            side_effect=UnsupportedPixelFormatError(),
        ),
        patch(
            "pandect.processing.services._dispatch_stage_task",
            return_value=SimpleNamespace(id="deferred-stage"),
        ),
    ):
        run_processing_stage(str(stage.pk))

    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.SKIPPED
    assert stage.failure_code == "unsupported_pixel_format"
    assert stage.failure_detail == ""
    assert stage.attempts == 1


@pytest.mark.django_db
def test_task_keeps_corrupt_image_decode_as_a_failure():
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.IMAGE)
    Image.objects.create(
        catalog_item=item,
        file=SimpleUploadedFile("corrupt.png", b"canonical-image-evidence"),
        mime_type="image/png",
    )
    run = create_local_run(item, enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.IMAGE_OCR)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )

    with (
        patch(
            "pandect.processing.tasks.execute_stage",
            side_effect=ToolFailedError(),
        ),
        patch(
            "pandect.processing.services._dispatch_stage_task",
            return_value=SimpleNamespace(id="deferred-stage"),
        ),
    ):
        run_processing_stage(str(stage.pk))

    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.FAILED
    assert stage.failure_code == "tool_failed"
    assert stage.attempts == 1


@pytest.mark.django_db
def test_task_supersedes_the_run_when_the_canonical_input_changed():
    item = make_text_item("before")
    run = create_local_run(item, enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    item.message.body = "after"
    item.message.save()

    run_processing_stage(str(stage.pk))

    run.refresh_from_db()
    stage.refresh_from_db()
    assert run.status == ProcessingRun.Status.SUPERSEDED
    assert stage.status == ProcessingStage.Status.SUPERSEDED
    replacement = item.processing_runs.exclude(pk=run.pk).get()
    assert replacement.trigger == ProcessingRun.Trigger.AUTOMATIC
    assert replacement.source_fingerprint != run.source_fingerprint
    assert replacement.configuration["replacement_for_run"] == str(run.pk)
    assert replacement.configuration["reason"] == ("source_changed_during_processing")
    assert replacement.configuration["local_processing_snapshot"]["item_type"] == (
        CatalogItem.ItemType.TEXT_MESSAGE
    )


@pytest.mark.django_db
def test_task_rechecks_both_source_digest_and_size(monkeypatch):
    item = make_text_item("before")
    run = create_local_run(item, enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    monkeypatch.setattr(
        "pandect.processing.tasks.source_fingerprint",
        lambda _item: type(
            "Fingerprint",
            (),
            {"sha256": run.source_fingerprint, "size_bytes": run.source_size + 1},
        )(),
    )

    run_processing_stage(str(stage.pk))

    run.refresh_from_db()
    assert run.status == ProcessingRun.Status.SUPERSEDED


@pytest.mark.django_db
def test_task_always_cleans_only_its_private_stage_workspace(tmp_path):
    item = make_text_item()
    run = create_local_run(item, enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    stage_root = tmp_path / str(run.pk) / str(stage.pk)
    workspace = stage_root / "attempt-1"
    workspace.mkdir(parents=True)
    (workspace / "partial.flac").write_bytes(b"partial")
    newer_workspace = stage_root / "attempt-2"
    newer_workspace.mkdir()
    (newer_workspace / "keep.flac").write_bytes(b"newer")
    sibling = tmp_path / str(run.pk) / "keep"
    sibling.mkdir()

    with (
        override_settings(WORK_ROOT=tmp_path),
        patch(
            "pandect.processing.tasks.execute_stage",
            side_effect=RuntimeError("private evidence must not leak"),
        ),
    ):
        run_processing_stage(str(stage.pk))

    assert not workspace.exists()
    assert (newer_workspace / "keep.flac").read_bytes() == b"newer"
    assert sibling.exists()
    stage.refresh_from_db()
    assert stage.failure_code == "unexpected_error"


@pytest.mark.django_db
def test_stale_task_cannot_commit_after_reconciliation_reclaims_stage():
    run = create_local_run(make_text_item(), enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.TEXT_EXTRACTION)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    current_attempts = []
    outcome = StageOutcome(
        results=(
            ResultSpec(
                result_kind=ProcessingResult.Kind.MESSAGE_TEXT_PROFILE,
                evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
                data={"executor": "stale"},
                processor_name="pandect.text",
                processor_version="1",
            ),
        )
    )

    def reclaim_during_execution(stale_stage):
        reconciliation_time = timezone.now()
        ProcessingStage.objects.filter(pk=stale_stage.pk).update(
            lease_expires_at=reconciliation_time - timedelta(seconds=1)
        )
        with patch(
            "pandect.processing.services._dispatch_stage_task",
            return_value=SimpleNamespace(id="reclaimed-delivery"),
        ):
            report = reconcile_stages(now=reconciliation_time)
        assert report.running_recovered == 1
        current = claim_stage(stale_stage.pk)
        assert current is not None
        current_attempts.append(current.attempts)
        return outcome

    with patch(
        "pandect.processing.tasks.execute_stage",
        side_effect=reclaim_during_execution,
    ):
        run_processing_stage(str(stage.pk))

    stage.refresh_from_db()
    assert current_attempts == [2]
    assert stage.status == ProcessingStage.Status.RUNNING
    assert stage.attempts == 2
    assert not stage.results.exists()
