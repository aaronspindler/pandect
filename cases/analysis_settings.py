"""Case-owner model settings and external analysis approvals."""

from copy import copy
from dataclasses import dataclass, field
from uuid import uuid4

from django.db import transaction
from django.http import Http404
from django.utils.crypto import constant_time_compare

from core.analysis.approval import (
    approve_scopes,
    case_approval_confirmation,
    clear_ai_grants,
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


def _publish_change(case):
    from core.case_search.hosted import case_setting_changed

    case.external_ai_revision = uuid4()
    case.save(
        update_fields=(
            "external_ai_revision",
            "external_ai_enabled",
            "external_analysis_approval",
            "external_analysis_models",
            "external_analysis_efforts",
        )
    )
    case_setting_changed(case)


@transaction.atomic
def change_ai_settings(actor, case_id, data):
    case = get_case(actor, case_id, owner=True, lock=True)
    action = data.get("action", "save")
    if action not in {"enable", "disable", "save", "review_current", "approve"}:
        raise Http404("Case action not found.")
    # Turning off transfers only needs the current signed settings revision.
    form_data = (
        {"confirmation": data.get("confirmation", "")} if action == "disable" else data
    )
    form = CaseAISettingsForm(form_data, case=case)
    if not form.is_valid():
        return AISettingsChange(case, form, "stale" if form.stale else "invalid")
    was_enabled = case.external_ai_enabled
    if action == "disable":
        if was_enabled:
            case.external_ai_enabled = False
            clear_ai_grants(case)
            _publish_change(case)
        return AISettingsChange(case, form, "saved")

    enabled = was_enabled or action in {"enable", "approve"}
    models = form.proposed_models()
    efforts = form.proposed_efforts()
    previous = case.external_analysis_approval.copy()
    proposed = copy(case)
    proposed.external_analysis_models = models
    proposed.external_analysis_efforts = efforts
    proposed.external_ai_enabled = enabled
    scopes = []
    if enabled:
        if not was_enabled:
            # Earlier versions did not disclose the hosted index or agent.
            clear_ai_grants(proposed)
        routes = {
            op: route
            for op, route in configured_external_executions(proposed).items()
            if op in ANALYSIS_MODEL_LABELS
        }
        review_action = data.get("review_action") if action == "approve" else action
        if (
            was_enabled
            and review_action == "save"
            and all(
                previous.get(key) == value for key, value in form.destinations.items()
            )
        ):
            routes = {
                op: route
                for op, route in routes.items()
                if models.get(op) != case.external_analysis_models.get(op)
                or efforts.get(op) != case.external_analysis_efforts.get(op)
            }
        scopes = pending_scopes(proposed, routes)
    hosting_changed = enabled and (
        not was_enabled
        or any(previous.get(key) != value for key, value in form.destinations.items())
    )
    if scopes or hosting_changed or action == "approve":
        confirmation = case_approval_confirmation(
            case,
            actor,
            "enable",
            [
                form.expected_confirmation,
                models,
                efforts,
                scopes,
                form.destinations,
                review_action,
            ],
        )
        selected = set(data.getlist("allow"))
        if action != "approve":
            return AISettingsChange(
                case, form, "review", scopes, confirmation, selected
            )
        if not constant_time_compare(data.get("review_confirmation", ""), confirmation):
            form.add_error(
                None, "The destinations changed. Review the current settings."
            )
            return AISettingsChange(case, form, "stale")
        if not selected <= {str(i) for i in range(len(scopes))}:
            return AISettingsChange(
                case, form, "select_destinations", scopes, confirmation, selected
            )
        case.external_analysis_approval = proposed.external_analysis_approval
        approve_scopes(
            case, actor, [scope for i, scope in enumerate(scopes) if str(i) in selected]
        )
        case.external_analysis_approval = (
            case.external_analysis_approval | form.destinations
        )
    elif action == "review_current":
        return AISettingsChange(case, form, "no_review")
    if (
        enabled != was_enabled
        or previous != case.external_analysis_approval
        or models != case.external_analysis_models
        or efforts != case.external_analysis_efforts
    ):
        case.external_ai_enabled = enabled
        case.external_analysis_models = models
        case.external_analysis_efforts = efforts
        _publish_change(case)
    return AISettingsChange(case, form, "saved")


@transaction.atomic
def change_external_approval(actor, case_id, action, confirmation):
    case = get_case(actor, case_id, owner=True, lock=True)
    if action == "revoke_destination":
        removed = revoke_destination(case, actor, confirmation)
        if not removed:
            return "stale"
        if case.external_ai_enabled and removed.get("operation") not in GEOGRAPHIC:
            _publish_change(case)
        return "destination_revoked"
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
        if case.external_ai_enabled:
            case.external_ai_enabled = False
            _publish_change(case)
        return "revoked"
    if scopes := pending_scopes(case, executions):
        approve_scopes(case, actor, scopes)
        return "geographic_approved"
    return None
