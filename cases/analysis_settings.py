"""Case-owner model settings and external analysis approvals."""

from copy import copy
from dataclasses import dataclass, field

from django.db import transaction
from django.http import Http404
from django.utils.crypto import constant_time_compare

from core.analysis.approval import (
    approve_scopes,
    case_approval_confirmation,
    pending_scopes,
    revoke_case_execution,
    revoke_destination,
)
from core.analysis.execution import GEOGRAPHIC, configured_external_executions

from .forms import ANALYSIS_MODEL_LABELS, CaseAISettingsForm
from .models import Case
from .permissions import get_case


@dataclass
class AISettingsChange:
    case: Case
    form: CaseAISettingsForm
    outcome: str
    scopes: list[dict] = field(default_factory=list)
    confirmation: str = ""
    selected_scope_ids: set[str] = field(default_factory=set)


@transaction.atomic
def change_ai_settings(actor, case_id, data):
    case = get_case(actor, case_id, owner=True, lock=True)
    action = data.get("action", "save")
    if action not in {"save", "review_current", "approve"}:
        raise Http404("Case action not found.")
    if action == "review_current":
        initial = CaseAISettingsForm(case=case)
        form = CaseAISettingsForm(initial.initial, case=case)
    else:
        form = CaseAISettingsForm(data, case=case)
    if not form.is_valid():
        return AISettingsChange(case, form, "stale" if form.stale else "invalid")

    backend = form.cleaned_data["ai_backend"]
    models = form.proposed_models()
    routes = {}
    if backend == Case.AIBackend.GATEWAY:
        proposed = copy(case)
        proposed.external_analysis_models = models
        routes = {
            op: route
            for op, route in configured_external_executions(proposed).items()
            if op in ANALYSIS_MODEL_LABELS
        }
        if not routes:
            form.add_error(
                "ai_backend",
                "External AI needs at least one configured model destination.",
            )
            return AISettingsChange(case, form, "invalid")
    changed = {
        op
        for op in ANALYSIS_MODEL_LABELS
        if op in form.data and form.cleaned_data[op] != form.initial[op]
    }
    # Manage can review declined destinations even when every assignment is unchanged.
    if backend == case.ai_backend and (changed or action == "save"):
        routes = {op: route for op, route in routes.items() if op in changed}
    scopes = pending_scopes(case, routes)
    if action == "review_current" and not scopes:
        return AISettingsChange(case, form, "no_review")

    if action == "approve" or scopes:
        confirmation = case_approval_confirmation(
            case, actor, "save", [form.expected_confirmation, backend, models, scopes]
        )
        selected = set(data.getlist("allow"))
        if action != "approve":
            return AISettingsChange(
                case, form, "review", scopes, confirmation, selected
            )
        if not constant_time_compare(data.get("review_confirmation", ""), confirmation):
            form.add_error(
                None, "The review changed. Check your settings and save again."
            )
            return AISettingsChange(case, form, "stale")
        ids = {str(i) for i in range(len(scopes))}
        if not selected or not selected <= ids:
            return AISettingsChange(
                case, form, "select_destinations", scopes, confirmation, selected
            )
        approve_scopes(
            case, actor, [scope for i, scope in enumerate(scopes) if str(i) in selected]
        )

    case.ai_backend = backend
    case.external_analysis_models = models
    case.save(update_fields=("ai_backend", "external_analysis_models"))
    return AISettingsChange(case, form, "saved")


@transaction.atomic
def change_external_approval(actor, case_id, action, confirmation):
    case = get_case(actor, case_id, owner=True, lock=True)
    if action == "revoke_destination":
        return (
            "destination_revoked"
            if revoke_destination(case, actor, confirmation)
            else "stale"
        )
    if action not in {"approve", "revoke"}:
        raise Http404("Case action not found.")
    executions = (
        {
            op: route
            for op, route in configured_external_executions(case=case).items()
            if op in GEOGRAPHIC
        }
        if action == "approve"
        else {}
    )
    if not constant_time_compare(
        confirmation, case_approval_confirmation(case, actor, action, executions)
    ):
        return "stale"
    if action == "revoke":
        revoke_case_execution(case)
        return "revoked"
    if scopes := pending_scopes(case, executions):
        approve_scopes(case, actor, scopes)
        return "geographic_approved"
    return None
