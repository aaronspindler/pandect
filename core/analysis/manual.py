"""Queue the methods and destinations available for one investigator selection."""

from uuid import uuid5

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from cases.permissions import get_case
from core.analysis.execution import (
    available_executions,
    requires_inference,
    selected_ai_executions,
)
from core.analysis.services import start_run
from core.models import CaseAnalysisBatch

OPTIONAL_AI_PARAMETERS = {
    "text_topics": "include_summary",
    "conversation_link": "semantic_links",
}


def selected_analysis_variants(case, operation, parameters):
    """Use separate deterministic and model results when a method has both."""
    optional = OPTIONAL_AI_PARAMETERS.get(operation)
    if optional:
        yield parameters | {optional: False}, None
        parameters = parameters | {optional: True, "_suggestions_only": True}
    ai = requires_inference(operation, parameters)
    executions = (
        selected_ai_executions(case, operation, parameters)
        if ai and operation != "claim_comparison"
        else available_executions(case, operation, parameters)
    )
    for backend, execution in executions.items():
        yield (
            parameters | {"backend": backend} if ai else parameters,
            execution,
        )


def start_available_run(
    actor, case, operation, sources, parameters, execution, **kwargs
):
    """Only server-selected routes receive a standing-grant revision."""
    grant = (
        case.external_analysis_approval.get("revision")
        if execution and execution["location"] == "external"
        else None
    )
    return start_run(
        actor,
        case.pk,
        operation,
        sources,
        parameters=parameters,
        approved_execution=execution,
        external_grant_revision=grant,
        require_configured_inference=True,
        **kwargs,
    )


@transaction.atomic
def start_selected_analysis(
    actor, case_id, operation, sources, parameters, *, submission_id
):
    case = get_case(actor, case_id, edit=True, lock=True)
    scope = {
        "type": "selected_analysis",
        "operation": operation,
        "sources": sources,
        "parameters": parameters,
    }
    existing = CaseAnalysisBatch.objects.filter(pk=submission_id).first()
    if existing is not None:
        if (
            existing.case_id != case.pk
            or existing.requested_by_id != actor.pk
            or existing.scope != scope
        ):
            raise ValidationError(
                "This submission was already used for another analysis."
            )
        runs = list(existing.runs.filter(case=case, previous_run__isnull=True))
        return existing, runs, False

    choices = list(selected_analysis_variants(case, operation, parameters))
    if not choices:
        raise ValidationError(
            "No configured or approved service can run this analysis."
        )
    if (
        len(choices) == 1
        and choices[0][1] is None
        and operation not in OPTIONAL_AI_PARAMETERS
    ):
        run, created = start_available_run(
            actor, case, operation, sources, *choices[0], submission_id=submission_id
        )
        return None, [run], created

    try:
        with transaction.atomic():
            batch = CaseAnalysisBatch.objects.create(
                pk=submission_id,
                case=case,
                requested_by=actor,
                scope=scope,
                status=CaseAnalysisBatch.Status.COMPLETE,
                processed_sources=len(sources),
                queued_runs=len(choices),
            )
    except IntegrityError:
        raise ValidationError(
            "This submission was already used for another analysis."
        ) from None

    runs = []
    for index, (parameters, execution) in enumerate(choices):
        run, _ = start_available_run(
            actor,
            case,
            operation,
            sources,
            parameters,
            execution,
            submission_id=uuid5(batch.pk, str(index)),
            batch=batch,
        )
        runs.append(run)
    return batch, runs, True
