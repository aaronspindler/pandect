"""Case-owner review of recipients, disclosed material, and prior exact approvals."""

from uuid import uuid4

from django.conf import settings
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from core.analysis.execution import (
    GEOGRAPHIC,
    case_execution_grant,
    configured_external_executions,
    execution_confirmation,
    execution_destination,
    transfer_scope,
)
from core.analysis.model_catalog import PROVIDER_NAMES

TRANSFER_CONTENT = {
    "text": (
        "Text and source references for summaries, claims, links, synthesis and "
        "Evidence chat. This includes relevant case history, chat questions, "
        "necessary conversation context and evidence excerpts."
    ),
    "vision": "Metadata-free images, image crops and scanned PDF pages for OCR.",
    "transcription": "Audio chunks from retained recordings for transcription.",
    "geocode": "Addresses or coordinates",
    "map_context": "Coordinates and search areas",
}


def hosted_destinations():
    search = getattr(settings, "PANDECT_CLOUDFLARE_SEARCH", {})
    agent = getattr(settings, "PANDECT_CLOUDFLARE_AGENT", {})
    return {
        "hosted_search": {
            "account": search.get("account", ""),
            "namespace": search.get("namespace", "production"),
        },
        "agent_url": agent.get("url", ""),
    }


def clear_ai_grants(case):
    """Keep separately approved geographic services when external AI is disabled."""
    approval = case.external_analysis_approval
    case.external_analysis_approval = {
        key: value
        for key, value in approval.items()
        if key not in {"hosted_search", "agent_url"}
    } | {
        "grants": [
            grant
            for grant in approval.get("grants", [])
            if grant["scope"].get("operation") in GEOGRAPHIC
        ],
        "executions": {
            op: route
            for op, route in approval.get("executions", {}).items()
            if op in GEOGRAPHIC
        },
    }


def case_approval_confirmation(case, actor, action, proposal):
    return execution_confirmation(
        "analysis.case.execution",
        [
            str(case.pk),
            str(actor.pk),
            action,
            str(case.external_ai_revision),
            case.external_analysis_approval,
        ],
        proposal,
    )


def scope_description(scope):
    if "operation" in scope:
        execution = scope["execution"]
        return {
            "name": execution["name"],
            "details": f"{execution_destination(execution)} · {execution['version']}",
            "material": TRANSFER_CONTENT[execution["purpose"]],
            "retention": "The destination’s retention policy applies.",
        }
    name = PROVIDER_NAMES.get(scope["provider"], scope["provider"])
    if scope["url"]:
        details = f"Direct connection · {scope['url']}"
    else:
        details = f"Cloudflare AI Gateway · {scope['account']} / {scope['gateway']}"
    if scope["key_alias"]:
        details += f" · Stored key {scope['key_alias']}"
    elif scope["billing"] == "gateway-credits":
        details += " · Cloudflare credits enabled"
    return {
        "name": name,
        "details": details,
        "material": TRANSFER_CONTENT[scope["purpose"]],
        "retention": (
            "The provider’s retention policy applies."
            if scope["retention"] == "provider-policy"
            else scope["retention"]
        ),
    }


def pending_scopes(case, executions):
    scopes = []
    for operation, execution in executions.items():
        scope = transfer_scope(operation, execution)
        if (
            scope
            and scope not in scopes
            and not case_execution_grant(case, operation, execution)
        ):
            scopes.append(scope)
    return scopes


def _saved_scopes(case):
    approval = case.external_analysis_approval
    if approval.get("approved_by") != str(case.owner_id):
        return []
    scopes = [grant["scope"] for grant in approval.get("grants", [])]
    for operation, execution in approval.get("executions", {}).items():
        scope = transfer_scope(operation, execution)
        if scope and scope not in scopes:
            scopes.append(scope)
    return scopes


def case_approval_context(case, actor):
    executions = configured_external_executions(case=case)
    geographic = {op: route for op, route in executions.items() if op in GEOGRAPHIC}
    approval = case.external_analysis_approval
    destinations = []
    approved_names = set()
    saved = [
        scope
        for scope in _saved_scopes(case)
        if case.external_ai_enabled or scope.get("operation") in GEOGRAPHIC
    ]
    for scope in saved:
        row = scope_description(scope)
        if scope not in [grant["scope"] for grant in approval.get("grants", [])]:
            legacy = [
                (op, route)
                for op, route in approval.get("executions", {}).items()
                if transfer_scope(op, route) == scope
            ]
            if "purpose" in scope:
                row["material"] = (
                    "Previously approved for specific models and tasks only."
                )
                row["details"] += " · " + ", ".join(
                    f"{op.replace('_', ' ')} ({route['model']})" for op, route in legacy
                )
        if "purpose" in scope:
            approved_names.add(row["name"])
        destinations.append(
            row
            | {
                "approved": True,
                "revoke_confirmation": case_approval_confirmation(
                    case, actor, "revoke_destination", scope
                ),
            }
        )
    return {
        "destinations": destinations,
        "approved_names": sorted(approved_names),
        "gateway_summary": bool(approved_names)
        and all(scope.get("gateway") for scope in saved if "purpose" in scope),
        "pending_ai": (
            case.external_ai_enabled
            and any(
                approval.get(key) != value
                for key, value in hosted_destinations().items()
            )
        )
        or any(
            not case_execution_grant(case, op, route)
            for op, route in executions.items()
            if op not in GEOGRAPHIC
        ),
        "active": bool(destinations),
        "geographic_destinations": [
            scope_description(scope) for scope in pending_scopes(case, geographic)
        ],
        "approve_confirmation": case_approval_confirmation(
            case, actor, "approve", geographic
        ),
        "revoke_confirmation": case_approval_confirmation(case, actor, "revoke", {}),
    }


def approve_scopes(case, owner, scopes):
    """Append reviewed grants, retaining each earlier receipt on pending work."""
    approval = case.external_analysis_approval.copy()
    timestamp = timezone.now().isoformat()
    if approval.get("approved_by") != str(owner.pk):
        approval = {
            "revision": str(uuid4()),
            "approved_at": timestamp,
            "approved_by": str(owner.pk),
        }
    grants = list(approval.get("grants", []))
    for scope in scopes:
        if any(grant["scope"] == scope for grant in grants):
            continue
        grants.append(
            {
                "scope": scope,
                "revision": str(uuid4()),
                "approved_by": str(owner.pk),
                "approved_at": timestamp,
            }
        )
    case.external_analysis_approval = approval | {"grants": grants}
    case.save(update_fields=("external_analysis_approval",))


def revoke_destination(case, actor, confirmation):
    for scope in _saved_scopes(case):
        if constant_time_compare(
            case_approval_confirmation(case, actor, "revoke_destination", scope),
            confirmation,
        ):
            approval = case.external_analysis_approval
            case.external_analysis_approval = approval | {
                "grants": [
                    grant
                    for grant in approval.get("grants", [])
                    if grant["scope"] != scope
                ],
                "executions": {
                    op: route
                    for op, route in approval.get("executions", {}).items()
                    if transfer_scope(op, route) != scope
                },
            }
            case.save(update_fields=("external_analysis_approval",))
            return scope
    return None


def revoke_case_execution(case):
    case.external_analysis_approval = {"revision": str(uuid4())}
    case.save(update_fields=("external_analysis_approval",))
