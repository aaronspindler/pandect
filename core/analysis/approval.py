"""Bind a case owner's external approval to the destinations they reviewed."""

from uuid import uuid4

from django.utils import timezone

from core.analysis.execution import (
    configured_external_executions,
    execution_confirmation,
    execution_destination,
)

TRANSFER_CONTENT = {
    "text": "text and source references",
    "transcription": "recordings",
    "geocode": "addresses or coordinates",
    "map_context": "coordinates and search areas",
}


def case_approval_confirmation(case, actor, action, executions):
    return execution_confirmation(
        "analysis.case.execution",
        [
            str(case.pk),
            str(actor.pk),
            action,
            case.external_analysis_approval.get("revision", ""),
        ],
        executions,
    )


def case_approval_context(case, actor):
    executions = configured_external_executions()
    approval = case.external_analysis_approval
    approved = approval.get("executions", {})
    destinations = {}
    for operation, execution in executions.items():
        name = execution_destination(execution)
        if execution.get("key_alias"):
            name += f" · Key alias {execution['key_alias']}"
        if execution.get("name"):
            name = f"{execution['name']} · {name} · {execution['version']}"
        key = TRANSFER_CONTENT[execution["purpose"]].capitalize(), name
        destinations[key] = destinations.get(key, True) and execution == approved.get(
            operation
        )
    return {
        "destinations": [
            {"material": material, "name": name, "approved": is_approved}
            for (material, name), is_approved in destinations.items()
        ],
        "active": bool(approved),
        "can_approve": bool(executions and approved != executions),
        "approve_confirmation": case_approval_confirmation(
            case, actor, "approve", executions
        ),
        "revoke_confirmation": case_approval_confirmation(case, actor, "revoke", {}),
    }


def approve_case_execution(case, owner, executions):
    """Record the request's locked, verified case-owner approval."""
    case.external_analysis_approval = {
        "revision": str(uuid4()),
        "approved_by": str(owner.pk),
        "approved_at": timezone.now().isoformat(),
        "executions": executions,
    }
    case.save(update_fields=("external_analysis_approval",))


def revoke_case_execution(case):
    """Replace the revision even when approval returns to the inactive state."""
    case.external_analysis_approval = {"revision": str(uuid4())}
    case.save(update_fields=("external_analysis_approval",))
