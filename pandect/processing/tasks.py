"""Huey entry points for durable processing stages and reconciliation."""

import logging
import stat
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.db import DatabaseError
from huey import crontab
from huey.contrib.djhuey import db_periodic_task, db_task

from pandect.processing.execution import execute_stage
from pandect.processing.extractors.errors import (
    ExtractorError,
    ToolTimedOutError,
    UnsupportedPixelFormatError,
)
from pandect.processing.extractors.hashing import fingerprint_file
from pandect.processing.models import (
    ArtifactPublication,
    DerivedArtifact,
    ProcessingStage,
)
from pandect.processing.services import (
    SourceUnavailableError,
    StageOutcome,
    StageProcessingError,
    claim_stage,
    clean_attempt_workspace,
    complete_stage,
    fail_stage,
    reconcile_stages,
    skip_stage,
    source_fingerprint,
    supersede_and_replace_local_run,
)

logger = logging.getLogger(__name__)


def _uncommitted_artifact_path(stage, file_name: str) -> Path | None:
    """Resolve only the exact flat derived path owned by this stage."""
    normalized = str(file_name).replace("\\", "/")
    relative = PurePosixPath(normalized)
    expected_prefix = (
        "derived",
        str(stage.run.catalog_item_id),
        str(stage.run_id),
    )
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or len(relative.parts) != 4
        or relative.parts[:3] != expected_prefix
        or not relative.name.startswith(f"{stage.pk}-")
    ):
        return None
    media_root = Path(settings.MEDIA_ROOT).resolve()
    candidate = media_root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None
    if resolved == media_root or media_root not in resolved.parents:
        return None
    return candidate


def _clean_uncommitted_artifacts(stage, outcome: StageOutcome) -> None:
    """Remove only exact files newly published by this rejected attempt."""
    try:
        current_attempt, current_status = ProcessingStage.objects.values_list(
            "attempts",
            "status",
        ).get(pk=stage.pk)
    except ProcessingStage.DoesNotExist:
        return
    except DatabaseError:
        logger.warning(
            "Skipped uncommitted artifact cleanup because durable state was "
            "unavailable: stage_id=%s",
            stage.pk,
        )
        return
    # A later or still-active claim can be reproducing or adopting the same
    # deterministic final path. Only the same durably terminal attempt is safe
    # to clean: terminal stages cannot be claimed again.
    cleanup_statuses = {
        ProcessingStage.Status.FAILED,
        ProcessingStage.Status.SKIPPED,
        ProcessingStage.Status.SUPERSEDED,
    }
    if current_attempt != stage.attempts or current_status not in cleanup_statuses:
        return

    removed = 0
    for artifact in outcome.artifacts:
        if not artifact.publication_created:
            continue
        normalized = str(artifact.file_name).replace("\\", "/")
        try:
            if DerivedArtifact.objects.filter(file=normalized).exists():
                continue
        except DatabaseError:
            logger.warning(
                "Skipped uncommitted artifact cleanup because durable state "
                "was unavailable: stage_id=%s",
                stage.pk,
            )
            return

        candidate = _uncommitted_artifact_path(stage, normalized)
        if candidate is None or candidate.is_symlink():
            continue
        try:
            before = candidate.stat(follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                continue
            fingerprint = fingerprint_file(candidate)
            after = candidate.stat(follow_symlinks=False)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or fingerprint.sha256 != artifact.sha256
                or fingerprint.size_bytes != artifact.byte_size
            ):
                continue
            candidate.unlink()
            removed += 1
        except (FileNotFoundError, OSError):
            continue
    if removed:
        logger.info(
            "Removed uncommitted processing artifacts: stage_id=%s count=%d",
            stage.pk,
            removed,
        )


def _publication_paths(publication: ArtifactPublication) -> tuple[Path, Path] | None:
    """Resolve one journal row without accepting traversal or foreign paths."""
    final_relative = PurePosixPath(publication.file_name.replace("\\", "/"))
    temporary_relative = PurePosixPath(publication.temporary_name.replace("\\", "/"))
    expected_final = (
        "derived",
        str(publication.stage.run.catalog_item_id),
        str(publication.stage.run_id),
    )
    expected_temporary = (
        str(publication.stage.run_id),
        str(publication.stage_id),
        f"attempt-{publication.attempt}",
    )
    if (
        final_relative.is_absolute()
        or temporary_relative.is_absolute()
        or ".." in final_relative.parts
        or ".." in temporary_relative.parts
        or len(final_relative.parts) != 4
        or len(temporary_relative.parts) != 4
        or final_relative.parts[:3] != expected_final
        or temporary_relative.parts[:3] != expected_temporary
        or not final_relative.name.startswith(f"{publication.stage_id}-")
    ):
        return None
    final = Path(settings.MEDIA_ROOT).resolve().joinpath(*final_relative.parts)
    temporary = Path(settings.WORK_ROOT).resolve().joinpath(*temporary_relative.parts)
    return final, temporary


def _clean_publication(publication: ArtifactPublication) -> bool:
    """Delete only a journal-proven hard link not represented as evidence."""
    if DerivedArtifact.objects.filter(file=publication.file_name).exists():
        publication.delete()
        return False
    paths = _publication_paths(publication)
    if paths is None:
        return False
    final, temporary = paths
    try:
        temporary_stat = temporary.stat(follow_symlinks=False)
        if (
            temporary.is_symlink()
            or not stat.S_ISREG(temporary_stat.st_mode)
            or temporary_stat.st_dev != publication.source_device
            or temporary_stat.st_ino != publication.source_inode
        ):
            return False
        if not final.exists() and not final.is_symlink():
            temporary_fingerprint = fingerprint_file(temporary)
            if (
                temporary_fingerprint.sha256 != publication.sha256
                or temporary_fingerprint.size_bytes != publication.byte_size
            ):
                return False
            publication.delete()
            return True

        final_stat = final.stat(follow_symlinks=False)
        if (
            final.is_symlink()
            or not stat.S_ISREG(final_stat.st_mode)
            or final_stat.st_dev != publication.source_device
            or final_stat.st_ino != publication.source_inode
        ):
            return False
        fingerprint = fingerprint_file(final)
        after = final.stat(follow_symlinks=False)
        if (
            after.st_dev != final_stat.st_dev
            or after.st_ino != final_stat.st_ino
            or after.st_size != final_stat.st_size
            or after.st_mtime_ns != final_stat.st_mtime_ns
            or fingerprint.sha256 != publication.sha256
            or fingerprint.size_bytes != publication.byte_size
        ):
            return False
        final.unlink()
        publication.delete()
        return True
    except (FileNotFoundError, OSError):
        return False


def _clean_attempt_publications(stage, *, terminal_only: bool = True) -> int:
    """Bounded cleanup for publications owned by ``stage.attempts``.

    A stale task object may name an attempt older than the row currently in the
    database. Older attempts are cleaned only after the stage is terminal; a
    newer active attempt may still be reading or adopting the same stable
    destination. ``terminal_only=False`` is reserved for reconciliation while
    it owns the transaction that fenced the current attempt as pending.
    """
    return _clean_publication_attempt(
        stage,
        stage.attempts,
        current_attempt_fenced=not terminal_only,
    )


def _clean_publication_attempt(
    stage,
    attempt: int,
    *,
    current_attempt_fenced: bool = False,
) -> int:
    """Clean one journaled attempt without crossing an active-owner fence."""
    try:
        current_attempt, current_status = ProcessingStage.objects.values_list(
            "attempts",
            "status",
        ).get(pk=stage.pk)
    except (ProcessingStage.DoesNotExist, DatabaseError):
        return 0
    if attempt > current_attempt:
        return 0
    active = {
        ProcessingStage.Status.PENDING,
        ProcessingStage.Status.QUEUED,
        ProcessingStage.Status.RUNNING,
    }
    if current_status in active and not (
        current_attempt_fenced
        and attempt == current_attempt
        and current_status == ProcessingStage.Status.PENDING
    ):
        return 0
    publications = list(
        ArtifactPublication.objects.select_related("stage__run__catalog_item")
        .filter(stage=stage.pk, attempt=attempt)
        .order_by("published_at", "pk")[:256]
    )
    cleaned = sum(_clean_publication(publication) for publication in publications)
    if not ArtifactPublication.objects.filter(stage=stage.pk, attempt=attempt).exists():
        clean_attempt_workspace(stage.run_id, stage.pk, attempt)
    return cleaned


def _clean_terminal_stage_publications(stage) -> int:
    """Clean current and prior attempt journals after the stage is terminal."""
    try:
        current_attempt, current_status = ProcessingStage.objects.values_list(
            "attempts",
            "status",
        ).get(pk=stage.pk)
    except (ProcessingStage.DoesNotExist, DatabaseError):
        return 0
    if current_status in {
        ProcessingStage.Status.PENDING,
        ProcessingStage.Status.QUEUED,
        ProcessingStage.Status.RUNNING,
    }:
        return 0
    # Attempts are bounded to two in the current workflows. Keep the cleanup
    # independently bounded even if a malformed historical row reports a much
    # larger counter, while also removing publication-free scratch directories.
    first_attempt = max(1, current_attempt - 255)
    attempts = range(first_attempt, current_attempt + 1)
    return sum(_clean_publication_attempt(stage, attempt) for attempt in attempts)


def _clean_stage_workspace(run_id, stage_id, attempt: int) -> None:
    """Remove only the private ephemeral directory for this completed attempt."""
    clean_attempt_workspace(run_id, stage_id, attempt)


def _skip_unsupported_pixel_stage(stage: ProcessingStage) -> None:
    """Persist a decoder-capability skip only while the source is unchanged."""
    try:
        after = source_fingerprint(stage.run.catalog_item_id)
    except SourceUnavailableError:
        fail_stage(
            stage.pk,
            "source_unavailable",
            "The canonical source is unavailable.",
            expected_attempt=stage.attempts,
        )
        return
    if (
        after.sha256 != stage.run.source_fingerprint
        or after.size_bytes != stage.run.source_size
    ):
        supersede_and_replace_local_run(stage.run_id)
        return
    skip_stage(
        stage.pk,
        "unsupported_pixel_format",
        expected_attempt=stage.attempts,
    )


@db_task()
def run_processing_stage(stage_id: str) -> None:
    """Claim and execute one UUID-addressed stage without returning evidence."""
    stage = claim_stage(stage_id)
    if stage is None:
        return

    outcome = None
    outputs_committed = False
    try:
        # Resolve by ID on every check so reverse one-to-one message relations
        # cannot serve a body cached before the processor ran.
        before = source_fingerprint(stage.run.catalog_item_id)
        if (
            before.sha256 != stage.run.source_fingerprint
            or before.size_bytes != stage.run.source_size
        ):
            supersede_and_replace_local_run(stage.run_id)
            return

        outcome = execute_stage(stage)

        after = source_fingerprint(stage.run.catalog_item_id)
        if (
            after.sha256 != stage.run.source_fingerprint
            or after.size_bytes != stage.run.source_size
        ):
            supersede_and_replace_local_run(stage.run_id)
            return
        outputs_committed = complete_stage(
            stage.pk,
            outcome,
            expected_attempt=stage.attempts,
            observed_source_fingerprint=after.sha256,
        )
    except SourceUnavailableError:
        fail_stage(
            stage.pk,
            "source_unavailable",
            "The canonical source is unavailable.",
            expected_attempt=stage.attempts,
        )
    except StageProcessingError as error:
        fail_stage(
            stage.pk,
            error.code,
            error.public_message,
            expected_attempt=stage.attempts,
            retryable=error.retryable,
        )
    except UnsupportedPixelFormatError:
        _skip_unsupported_pixel_stage(stage)
    except ExtractorError as error:
        fail_stage(
            stage.pk,
            error.code,
            error.public_message,
            expected_attempt=stage.attempts,
            retryable=isinstance(error, ToolTimedOutError),
        )
    except Exception as error:  # noqa: BLE001 - task boundary must persist failure.
        logger.error(
            "Processing stage failed unexpectedly: stage_id=%s exception_type=%s",
            stage.pk,
            type(error).__name__,
        )
        fail_stage(
            stage.pk,
            "unexpected_error",
            "The local processor failed unexpectedly.",
            expected_attempt=stage.attempts,
        )
    finally:
        if outcome is not None and not outputs_committed:
            _clean_uncommitted_artifacts(stage, outcome)
        _clean_terminal_stage_publications(stage)
        if not ArtifactPublication.objects.filter(
            stage=stage.pk,
            attempt=stage.attempts,
        ).exists():
            _clean_stage_workspace(stage.run_id, stage.pk, stage.attempts)


@db_task()
def run_identity_gallery_rematch_batch(job_id: str) -> None:
    """Advance one UUID-addressed, bounded gallery-rematch cursor."""
    from pandect.processing.identity_rematch import process_gallery_rematch_batch

    try:
        process_gallery_rematch_batch(job_id)
    except Exception as error:  # noqa: BLE001 - reconciliation owns recovery.
        logger.error(
            "Gallery-rematch batch failed: job_id=%s exception_type=%s",
            job_id,
            type(error).__name__,
        )


@db_periodic_task(crontab(minute="*/5"))
def reconcile_processing_stages() -> None:
    """Repair abandoned queued/running stages on the conservative worker."""
    from pandect.processing.identity_rematch import reconcile_gallery_rematch_jobs

    stale_publications = list(
        ArtifactPublication.objects.select_related("stage__run__catalog_item")
        .exclude(
            stage__status__in=(
                ProcessingStage.Status.RUNNING,
                ProcessingStage.Status.QUEUED,
                ProcessingStage.Status.PENDING,
            )
        )
        .order_by("published_at", "pk")[:256]
    )
    for publication in stale_publications:
        _clean_publication_attempt(publication.stage, publication.attempt)
    report = reconcile_stages()
    gallery_jobs = reconcile_gallery_rematch_jobs()
    if (
        report.queued_recovered
        or report.running_recovered
        or report.running_failed
        or report.replacements_repaired
        or gallery_jobs
    ):
        logger.info(
            "Reconciled processing stages: queued=%d running=%d failed=%d "
            "replacements=%d gallery_jobs=%d",
            report.queued_recovered,
            report.running_recovered,
            report.running_failed,
            report.replacements_repaired,
            gallery_jobs,
        )
