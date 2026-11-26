"""Render Case Settings and handle case details and processing approvals."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from config.access import product_login_required
from core.analysis.approval import case_approval_context, scope_description
from core.case_search.approval import search_approval_context
from socialmedia import instagram_connector, monitor_jobs

from .analysis_settings import change_ai_settings, change_external_approval
from .forms import CaseAISettingsForm, CaseForm
from .permissions import get_case
from .responses import render_case
from .search_settings import rerank_approval_context


@product_login_required
@require_http_methods(["GET", "POST"])
def case_settings(request, case_id):
    status = 200
    latest_details = None
    if request.method == "POST":
        with transaction.atomic():
            case = get_case(request.user, case_id, owner=True, lock=True)
            saved_updated_at = case.updated_at.isoformat()
            saved_details = {"name": case.name}
            form = CaseForm(request.POST, instance=case)
            if form.is_valid():
                if form.cleaned_data["expected_updated_at"] != saved_updated_at:
                    latest_details = saved_details
                    form.data = form.data.copy()
                    form.data["expected_updated_at"] = saved_updated_at
                    form.add_error(
                        None,
                        "This case changed while you were editing. Your proposed "
                        "values are preserved below. Review the latest saved "
                        "details, then save your reviewed changes.",
                    )
                    # ModelForm validation mutates its instance; keep case context
                    # on the saved name while bound fields retain the user's draft.
                    case.refresh_from_db()
                    status = 409
                else:
                    form.save()
                    messages.success(request, "Case details saved.")
                    return redirect("cases:case_settings", case_id=case.pk)
    else:
        case = get_case(request.user, case_id, owner=True)
        form = CaseForm(instance=case)
    return _render_case_settings(
        request, case, status=status, form=form, latest_details=latest_details
    )


def _render_case_settings(
    request,
    case,
    *,
    status=200,
    form=None,
    latest_details=None,
    ai_settings_form=None,
):
    from core.case_search.generation import settings_rows
    from core.evidence_chat.decisions_approval import approval_context

    review_search = request.GET.get("review_search") == "1"
    invitations = list(case.invitations.all())
    return render_case(
        request,
        "cases/settings.html",
        case,
        status=status,
        form=form if form is not None else CaseForm(instance=case),
        ai_settings_form=ai_settings_form
        if ai_settings_form is not None
        else CaseAISettingsForm(case=case),
        latest_details=latest_details,
        memberships=case.memberships.select_related("user").order_by("user__username"),
        pending_invitations=[item for item in invitations if item.is_pending],
        invitation_history=[item for item in invitations if not item.is_pending],
        external_analysis=case_approval_context(case, request.user),
        search_approval=search_approval_context(case, request.user),
        search_backlog_review=review_search,
        search_generation=settings_rows(case, request.user, estimate=True)
        if review_search
        else (),
        rerank_approval=rerank_approval_context(case, request.user),
        chat_routing=approval_context(case, request.user),
        instagram_scheduling_available=instagram_connector.scheduling_available(),
        instagram_target_approved=monitor_jobs.target_approved(case.pk),
        instagram_target=instagram_connector.PILOT_TARGET,
    )


@product_login_required
@require_POST
def case_ai_settings(request, case_id):
    change = change_ai_settings(request.user, case_id, request.POST)
    case, form, outcome = change.case, change.form, change.outcome
    if outcome in {"invalid", "stale"}:
        return _render_case_settings(
            request,
            case,
            status=409 if outcome == "stale" else 200,
            ai_settings_form=form,
        )
    if outcome == "no_review":
        return redirect(
            reverse(
                "cases:case_settings", args=(case.pk,), fragment="external-analysis"
            )
        )
    if outcome in {"review", "select_destinations"}:
        return render_case(
            request,
            "cases/ai_review.html",
            case,
            destinations=[
                scope_description(scope) | {"scope_id": str(i)}
                for i, scope in enumerate(change.scopes)
            ],
            proposal=[
                (name, form.cleaned_data[name])
                for name in form.fields
                if name in form.data
            ],
            review_confirmation=change.confirmation,
            error="Select the destinations you want to approve."
            if outcome == "select_destinations"
            else "",
            selected_scope_ids=change.selected_scope_ids,
        )
    messages.success(request, "AI processing settings saved.")
    return redirect(
        reverse("cases:case_settings", args=(case.pk,), fragment="ai-processing")
    )


@product_login_required
@require_POST
def case_external_analysis(request, case_id):
    outcome = change_external_approval(
        request.user,
        case_id,
        request.POST.get("action"),
        request.POST.get("confirmation", ""),
    )
    if outcome == "stale":
        messages.error(request, "Settings changed. Review the current destinations.")
    elif outcome == "destination_revoked":
        messages.success(request, "Destination approval revoked for this case.")
    elif outcome == "revoked":
        messages.success(request, "External analysis approval revoked for this case.")
    elif outcome == "geographic_approved":
        messages.success(request, "Geographic destinations approved for this case.")
    return redirect(
        reverse("cases:case_settings", args=(case_id,), fragment="ai-processing")
    )


@product_login_required
@require_POST
def case_instagram_monitoring(request, case_id):
    action = request.POST.get("action")
    if action not in {"approve", "revoke"}:
        raise Http404("Case action not found.")
    try:
        monitor_jobs.set_target_approval(
            request.user, case_id, approved=action == "approve"
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        message = (
            "Synthetic Instagram monitoring approved for this case."
            if action == "approve"
            else "Approval revoked. Scheduled checks have paused."
        )
        messages.success(request, message)
    return redirect(
        reverse("cases:case_settings", args=(case_id,), fragment="instagram-monitoring")
    )
