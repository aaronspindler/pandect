"""Queue new attempts for retained processing checks."""

from uuid import uuid5

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.http import Http404

from cases.permissions import get_case
from core.analysis.bulk import start_case_analysis
from core.analysis.errors import AnalysisError, error_message
from core.analysis.execution import available_executions, requires_inference
from core.analysis.manual import start_available_run
from core.analysis.operations import OPERATIONS
from core.models import AnalysisRun, CaseAnalysisBatch

RETRIES_PER_STEP = 10


def retry_route(case, run):
    parameters = run.parameters
    if requires_inference(run.operation, parameters):
        if "backend" not in parameters and run.external_processing:
            parameters = parameters | {"backend": "gateway"}
        backend = parameters.get("backend", "local")
    else:
        backend = "external" if run.external_processing else "local"
    available = available_executions(case, run.operation, parameters)
    if backend not in available:
        raise AnalysisError("original_route_unavailable")
    return parameters, available[backend]


def retry_run(actor, case, prior, *, submission_id, require_retained_inputs=False):
    choices = [
        {
            "kind": source["kind"],
            "id": source["evidence_id"],
            **{
                key: source[key]
                for key in ("attachment_id", "result_id", "geocode_candidate")
                if source.get(key) is not None
            },
        }
        for source in prior.inputs
    ]
    parameters, execution = retry_route(case, prior)
    return start_available_run(
        actor,
        case,
        prior.operation,
        choices,
        parameters,
        execution,
        previous_run=prior,
        submission_id=submission_id,
        require_retained_inputs=require_retained_inputs,
    )


def failed_checks(case):
    runs = AnalysisRun.objects.filter(case=case)
    return (
        runs.filter(status=AnalysisRun.Status.FAILED, operation__in=OPERATIONS)
        .filter(Q(previous_run__isnull=True) | Q(previous_run__case=case))
        .filter(Q(batch__isnull=True) | Q(batch__case=case))
        .filter(~Exists(runs.filter(previous_run=OuterRef("pk"))))
    )


def latest_failed_retry(case):
    return (
        CaseAnalysisBatch.objects.filter(case=case, scope__type="failed_retries")
        .only("status")
        .first()
    )


@transaction.atomic
def start_failed_retries(actor, case_id, *, submission_id, after_batch_id):
    case = get_case(actor, case_id, edit=True, lock=True)
    existing = CaseAnalysisBatch.objects.filter(pk=submission_id).first()
    if existing is not None:
        if (
            existing.case_id != case.pk
            or existing.requested_by_id != actor.pk
            or existing.scope.get("type") != "failed_retries"
        ):
            raise ValidationError(
                "This submission was already used for another analysis."
            )
        return existing, False
    latest = latest_failed_retry(case)
    # A form predating a concurrent request returns that batch even after it finishes.
    if latest is not None and (
        latest.status == CaseAnalysisBatch.Status.QUEUED or latest.pk != after_batch_id
    ):
        return latest, False
    if latest is None and after_batch_id is not None:
        raise ValidationError("Reload Processing to try again.")
    if not failed_checks(case).exists():
        return None, False
    return start_case_analysis(
        actor,
        case.pk,
        submission_id=submission_id,
        scope={
            "type": "failed_retries",
            "external_grant_revision": case.external_analysis_approval.get("revision"),
        },
    )


def prepare_failed_retries(batch, case):
    checks = (
        failed_checks(case)
        .filter(created_at__lte=batch.created_at)
        .filter(Q(finished_at__isnull=True) | Q(finished_at__lte=batch.created_at))
    )
    if batch.source_cursor:
        checks = checks.filter(pk__gt=batch.source_cursor)
    page = list(checks.order_by("pk")[: RETRIES_PER_STEP + 1])
    for prior in page[:RETRIES_PER_STEP]:
        try:
            if prior.external_processing and (
                batch.scope["external_grant_revision"]
                != case.external_analysis_approval.get("revision")
            ):
                raise AnalysisError("retry_approval_required")
            _, created = retry_run(
                batch.requested_by,
                case,
                prior,
                submission_id=uuid5(batch.pk, str(prior.pk)),
                require_retained_inputs=True,
            )
        except (AnalysisError, Http404, ValidationError) as error:
            code = (
                error.code if isinstance(error, AnalysisError) else "source_unavailable"
            )
            batch.unavailable_sources += 1
            if len(batch.preparation_errors) < 100:
                batch.preparation_errors.append(
                    {
                        "run_id": str(prior.pk),
                        "operation": prior.operation,
                        "message": error_message(
                            "retry_external_route_unavailable"
                            if code == "original_route_unavailable"
                            and prior.external_processing
                            else code
                        ),
                    }
                )
        else:
            batch.queued_runs += created
        batch.processed_sources += 1
        batch.source_cursor = prior.pk
    return len(page) > RETRIES_PER_STEP
