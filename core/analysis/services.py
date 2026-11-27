"""Submit and cancel case analysis runs under the current case lock."""

import json
from uuid import UUID, uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone

from cases.permissions import get_case
from core.analysis.errors import AnalysisError
from core.analysis.execution import (
    inference_configured,
    requires_inference,
    snapshot_execution,
    validate_case_execution_grant,
)
from core.analysis.operations import PAIR_MESSAGE_OPERATIONS, get_operation
from core.analysis.source_limits import check_operation_source_limits
from core.analysis.source_resolution import (
    SourceResolutionCache,
    resolve_processing_inputs,
    resolve_snapshots,
)
from core.analysis.source_selection import selection_uuid, snapshot_source_references
from core.analysis.transcripts import follow_up_transcription
from core.models import AnalysisRun
from text.analysis.attributed_sources import snapshot_messages, writing_scope

INHERIT_BATCH = object()
FINISHED = set(AnalysisRun.Status.values) - {"queued", "running"}


def case_run(case, run_id):
    try:
        return (
            AnalysisRun.objects.select_related("requested_by", "previous_run")
            .filter(Q(previous_run__isnull=True) | Q(previous_run__case=case))
            .get(case=case, pk=run_id)
        )
    except AnalysisRun.DoesNotExist:
        raise Http404("Processing not found.") from None


@transaction.atomic
def start_run(
    actor,
    case_id,
    operation,
    selections,
    *,
    parameters=None,
    submission_id=None,
    previous_run=None,
    batch=INHERIT_BATCH,
    approved_execution=None,
    external_grant_revision=None,
    require_configured_inference=False,
    require_retained_inputs=False,
    reuse_published_extraction=False,
):
    case = get_case(actor, case_id, edit=True, lock=True)
    if previous_run and previous_run.case_id != case.pk:
        raise Http404("Processing not found.")
    if batch is INHERIT_BATCH:
        batch = previous_run.batch if previous_run else None
    if batch and (
        batch.case_id != case.pk
        or (
            batch.requested_by_id != actor.pk
            and not (previous_run and previous_run.batch_id == batch.pk)
        )
    ):
        raise ValidationError("Choose an analysis batch requested by you in this case.")
    specification = get_operation(operation)
    parameters = {} if parameters is None else parameters
    try:
        valid_parameters = isinstance(parameters, dict) and len(
            json.dumps(parameters, allow_nan=False).encode()
        ) <= (65536 if operation == "question_synthesis" else 32768)
    except ValueError, TypeError:
        valid_parameters = False
    if not valid_parameters:
        raise ValidationError("Analysis settings exceed the supported limit.")
    # Resolve trusted execution settings independently of form or rerun data.
    retained_keys = {"_execution", "_external_approval"}
    parameters = {
        key: value for key, value in parameters.items() if key not in retained_keys
    }
    execution = snapshot_execution(operation, parameters, case=case)
    external = bool(execution and execution["location"] == "external")
    if approved_execution is not None and approved_execution != execution:
        raise AnalysisError("execution_changed")
    if external:
        if approved_execution is None:
            raise AnalysisError("external_processing_not_approved")
        approval = validate_case_execution_grant(
            case, operation, execution, external_grant_revision
        )
    if operation == "claim_comparison":
        from text.analysis.claim_comparison import MAX_SOURCES, validate_selection

        if not 2 <= len(selections) <= MAX_SOURCES:
            raise ValidationError(
                "Select one claim source and one to five other sources."
            )
    attributed = writing_scope(operation, parameters)
    if attributed:
        if ("period_split" in attributed) != (operation == "style_change"):
            raise ValidationError("Choose the required message periods.")
        snapshots = snapshot_messages(case, attributed, selections)
    else:
        check_operation_source_limits(case, operation, parameters, selections)
        snapshots = snapshot_source_references(
            case,
            selections,
            allowed_kinds=specification.kinds,
            direct_messages=operation in PAIR_MESSAGE_OPERATIONS,
        )
    if (
        previous_run
        and (
            attributed
            or require_retained_inputs
            or operation in {"claim_comparison", "journey_claims", "sighting_group"}
        )
        and (previous_run.operation != operation or previous_run.inputs != snapshots)
    ):
        raise AnalysisError("source_changed")
    submission_id = UUID(selection_uuid(submission_id or uuid4()))
    submissions = AnalysisRun.objects.filter(
        case=case, requested_by=actor, submission_id=submission_id
    )
    existing = submissions.first()
    if existing:

        def comparable(values):
            return [{k: v for k, v in x.items() if k != "id"} for x in values]

        if (
            existing.operation != operation
            or {
                key: value
                for key, value in existing.parameters.items()
                if key not in retained_keys
            }
            != parameters
            or comparable(existing.inputs) != comparable(snapshots)
            or existing.external_processing != external
            or existing.previous_run_id != (previous_run.pk if previous_run else None)
            or existing.batch_id != (batch.pk if batch else None)
        ):
            raise ValidationError(
                "This submission was already used for different analysis settings."
            )
        if attributed:
            return existing, False
        if (
            parameters.get("_search_generation") == "briefs"
            and operation == "text_topics"
        ):
            resolve_processing_inputs(existing, case)
        else:
            resolve_snapshots(
                snapshots, case, cache=SourceResolutionCache(case, active_only=True)
            )
        return existing, False
    if (
        (require_configured_inference or (operation == "transcription" and external))
        and requires_inference(operation, parameters)
        and not inference_configured(execution)
    ):
        if operation == "transcription":
            raise AnalysisError("gateway_transcription_not_configured")
        raise AnalysisError("gateway_not_configured")
    if execution is not None:
        parameters = parameters | {"_execution": execution}
    if external:
        parameters["_external_approval"] = {
            key: approval[key] for key in ("revision", "approved_by", "approved_at")
        }
    if len(json.dumps(parameters, allow_nan=False).encode()) > (
        65536 if operation == "question_synthesis" else 32768
    ):
        raise ValidationError("Analysis settings exceed the supported limit.")
    run = AnalysisRun(
        case=case,
        requested_by=actor,
        operation=operation,
        method_version=specification.version,
        parameters=parameters,
        inputs=snapshots,
        submission_id=submission_id,
        external_processing=external,
        previous_run=previous_run,
        batch=batch,
    )
    if not attributed:
        resolved = resolve_processing_inputs(run, case, material_checked=True)
        from .extraction_reuse import reuse_run

        if reused := reuse_run(run, resolved, published=reuse_published_extraction):
            return reused, False
        if operation == "claim_comparison":
            validate_selection(resolved, run.parameters)
        elif operation == "question_synthesis":
            from text.analysis.synthesis import records_for_selection

            records_for_selection(resolved, run.parameters)
    try:
        with transaction.atomic():
            run.save()
    except IntegrityError:
        if submissions.exists():
            raise ValidationError(
                "This submission was already used for different analysis settings."
            ) from None
        raise
    from core.analysis.tasks import process_analysis

    process_analysis.enqueue(str(run.pk))
    return run, True


@transaction.atomic
def cancel_run(actor, case_id, run_id):
    case = get_case(actor, case_id, edit=True, lock=True)
    run = AnalysisRun.objects.select_for_update().filter(case=case, pk=run_id).first()
    if run is None:
        raise Http404("Processing not found.")
    if run.status not in FINISHED:
        run.status = "cancelled"
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "finished_at"])
        follow_up_transcription(run)
        if run.operation == "native_text":
            from core.evidence_chat.proposals import run_finished

            run_finished(run)
    return run
