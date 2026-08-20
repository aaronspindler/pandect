"""UUID-only Huey entry point for explicitly consented remote stages."""

import logging

from huey import crontab
from huey.contrib.djhuey import db_periodic_task, db_task

from pandect.processing.models import ProcessingRun, ProcessingStage
from pandect.processing.remote.aggregation import (
    execute_remote_aggregate_stage,
    finalize_ready_remote_aggregates,
    finalize_remote_aggregate,
)
from pandect.processing.remote.cloudflare import CloudflareAIError
from pandect.processing.remote.followup import (
    FOLLOWUP_WORKFLOW,
    execute_followup_stage,
    validate_followup_consent,
)
from pandect.processing.remote.workflows import (
    RemoteSourceChangedError,
    RemoteWorkflowError,
    execute_remote_stage,
)
from pandect.processing.services import (
    SourceUnavailableError,
    StageProcessingError,
    claim_stage,
    complete_stage,
    fail_stage,
    mark_run_superseded,
    source_fingerprint,
)

logger = logging.getLogger(__name__)


@db_task()
def run_remote_processing_stage(stage_id: str) -> None:
    """Execute one consent-bound request unit or its local-only aggregate."""
    is_remote = ProcessingStage.objects.filter(
        pk=stage_id,
        run__scope=ProcessingRun.Scope.REMOTE,
    ).exists()
    if not is_remote:
        return
    stage = claim_stage(stage_id)
    if stage is None:
        return

    is_followup = False
    try:
        configuration = (
            stage.run.configuration if isinstance(stage.run.configuration, dict) else {}
        )
        is_followup = configuration.get("workflow") == FOLLOWUP_WORKFLOW
        if is_followup:
            outcome = execute_followup_stage(stage)
        elif stage.kind == ProcessingStage.Kind.REMOTE_AGGREGATE:
            outcome = execute_remote_aggregate_stage(stage)
        else:
            outcome = execute_remote_stage(stage)
        after = source_fingerprint(stage.run.catalog_item_id)
        if (
            after.sha256 != stage.run.source_fingerprint
            or after.size_bytes != stage.run.source_size
        ):
            mark_run_superseded(stage.run_id)
            return
        if is_followup:
            # Fence the small interval between adapter return and result commit.
            validate_followup_consent(stage)
        complete_stage(
            stage.pk,
            outcome,
            expected_attempt=stage.attempts,
            observed_source_fingerprint=after.sha256,
        )
    except RemoteSourceChangedError:
        mark_run_superseded(stage.run_id)
    except RemoteWorkflowError as error:
        fail_stage(
            stage.pk,
            error.code,
            error.public_message,
            expected_attempt=stage.attempts,
            retryable=False,
        )
    except CloudflareAIError as error:
        fail_stage(
            stage.pk,
            error.code,
            str(error),
            expected_attempt=stage.attempts,
            retryable=False,
        )
    except SourceUnavailableError:
        mark_run_superseded(stage.run_id)
    except StageProcessingError as error:
        fail_stage(
            stage.pk,
            error.code,
            error.public_message,
            expected_attempt=stage.attempts,
            retryable=error.retryable,
        )
    except Exception as error:  # noqa: BLE001 - persist sanitized task failure.
        logger.error(
            "Remote processing stage failed unexpectedly: stage_id=%s "
            "exception_type=%s",
            stage.pk,
            type(error).__name__,
        )
        fail_stage(
            stage.pk,
            "unexpected_remote_error",
            "The remote processing stage failed unexpectedly.",
            expected_attempt=stage.attempts,
            retryable=False,
        )
    finally:
        try:
            if stage.kind != ProcessingStage.Kind.REMOTE_AGGREGATE and not is_followup:
                finalize_remote_aggregate(stage.run_id)
        except Exception as error:  # noqa: BLE001 - reconciliation retries locally.
            logger.error(
                "Remote result aggregation failed unexpectedly: run_id=%s "
                "exception_type=%s",
                stage.run_id,
                type(error).__name__,
            )


@db_periodic_task(crontab(minute="*/5"))
def reconcile_remote_aggregates() -> None:
    """Recover a crash after the last remote result committed locally."""
    completed = finalize_ready_remote_aggregates()
    if completed:
        logger.info("Scheduled remote result aggregates: count=%d", completed)
