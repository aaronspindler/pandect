"""Investigator-facing case and collaboration workflows."""

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case as CaseExpression
from django.db.models import Count, OuterRef, Q, Subquery, Value, When
from django.db.models.deletion import ProtectedError, RestrictedError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from config.access import product_login_required
from core.analysis.approval import case_approval_context, scope_description
from core.case_search.approval import search_approval_context
from core.evidence.types import KINDS
from core.models import EvidenceChat, ImportantItem, ImportantNote
from geo.models import PointOfInterest
from socialmedia import instagram_connector, monitor_jobs
from socialmedia.models import SocialAccount
from ui.live import render_live

from .analysis_settings import change_ai_settings, change_external_approval
from .deletion import CASCADE_CONFLICT, CaseDeletionBlocked, delete_case_record
from .forms import (
    ANALYSIS_MODEL_LABELS,
    CaseAISettingsForm,
    CaseForm,
    ConfirmForm,
    InvitationForm,
    InvitationRoleForm,
    MembershipRoleForm,
)
from .invitations import (
    InvitationUnavailable,
    create_invitation,
    edit_invitation,
    get_pending_invitation,
    resend_invitation,
    resolve_invitation,
    revoke_invitation,
)
from .models import CaseMembership
from .permissions import accessible_cases, case_context, get_case
from .search_settings import change_meaning_search, rerank_approval_context


def _render_case(request, template, case, *, status=200, **context):
    return render(
        request,
        template,
        {
            **case_context(case, request.user),
            **context,
        },
        status=status,
    )


def _case_record_counts(case):
    return {
        "people_count": case.people.count(),
        "vehicles_count": case.vehicles.count(),
        "collaborator_count": case.memberships.count(),
        "pending_invitation_count": case.invitations.filter(
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).count(),
    }


def _archive_counts(queryset):
    return queryset.aggregate(
        active=Count("pk", filter=Q(is_archived=False)),
        archived=Count("pk", filter=Q(is_archived=True)),
    )


@product_login_required
@require_http_methods(["GET"])
def case_list(request):
    query = request.GET.get("q", "")[:200].strip()
    ownership = request.GET.get("ownership", "all")
    membership_role = CaseMembership.objects.filter(
        case_id=OuterRef("pk"), user=request.user
    ).values("role")[:1]
    queryset = (
        accessible_cases(request.user)
        .select_related("owner")
        .annotate(
            current_role=CaseExpression(
                When(owner=request.user, then=Value("owner")),
                default=Subquery(membership_role),
            )
        )
    )
    if query:
        queryset = queryset.filter(
            Q(name__icontains=query) | Q(description__icontains=query)
        )
    if ownership == "owned":
        queryset = queryset.filter(owner=request.user)
    elif ownership == "shared":
        queryset = queryset.exclude(owner=request.user)
    else:
        ownership = "all"
    page = Paginator(
        queryset.order_by("-updated_at", "-created_at", "pk"), 25
    ).get_page(request.GET.get("page"))
    return render_live(
        request,
        "cases/list.html",
        {"page_obj": page, "query": query, "ownership": ownership},
        collection=True,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def case_create(request):
    form = CaseForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        case = form.save(commit=False)
        case.owner = request.user
        case.save()
        messages.success(
            request, "Case created. Add evidence to begin your investigation."
        )
        return redirect("core:analysis:index", case_id=case.pk)
    return render(request, "cases/form.html", {"form": form, "creating": True})


@product_login_required
@require_http_methods(["GET"])
def case_detail(request, case_id):
    case = get_case(request.user, case_id)
    return redirect("core:analysis:index", case_id=case.pk)


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
    return _render_case(
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
        proposal = {
            "ai_backend": form.cleaned_data["ai_backend"],
            "confirmation": form.expected_confirmation,
        }
        proposal.update(
            {
                op: form.cleaned_data[op] if op in form.data else form.initial[op]
                for op in ANALYSIS_MODEL_LABELS
            }
        )
        return _render_case(
            request,
            "cases/ai_review.html",
            case,
            destinations=[
                scope_description(scope) | {"scope_id": str(i)}
                for i, scope in enumerate(change.scopes)
            ],
            proposal=proposal.items(),
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
def case_search_approval(request, case_id):
    outcome = change_meaning_search(
        request.user,
        case_id,
        request.POST.get("action"),
        request.POST.get("confirmation", ""),
    )
    if outcome == "stale":
        messages.error(
            request, "Settings changed. Review the meaning search destination."
        )
    elif outcome == "queue_failed":
        messages.error(
            request,
            "Indexing could not be queued. Review the destination and try again.",
        )
    elif outcome == "revoke":
        messages.success(request, "Meaning search approval revoked for this case.")
    elif outcome == "enable_chat":
        messages.success(
            request, "Hosted meaning search enabled for new chat questions."
        )
    elif outcome == "disable_chat":
        messages.success(request, "Hosted meaning search disabled for Evidence chat.")
    elif outcome == "approve":
        messages.success(request, "Meaning search approved. Indexing is queued.")
        return redirect(
            reverse(
                "core:analysis:processing",
                args=(case_id,),
                fragment="case-search-index",
            )
        )
    return redirect(
        reverse("cases:case_settings", args=(case_id,), fragment="meaning-search")
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


@product_login_required
@require_http_methods(["GET", "POST"])
def case_delete(request, case_id):
    form = ConfirmForm(request.POST if request.method == "POST" else None)
    status = 200
    if request.method == "POST":
        with transaction.atomic():
            case = get_case(request.user, case_id, owner=True, lock=True)
            if form.is_valid():
                try:
                    delete_case_record(case, case.pk)
                except CaseDeletionBlocked, ProtectedError, RestrictedError:
                    form.add_error(None, CASCADE_CONFLICT)
                    status = 409
                else:
                    messages.success(
                        request, "Case deleted. Your account is unchanged."
                    )
                    return redirect("cases:case_list")
    else:
        case = get_case(request.user, case_id, owner=True)
    evidence_counts = [
        _archive_counts(model.objects.filter(case=case)) for model in KINDS.values()
    ]
    counts = {
        **_case_record_counts(case),
        "social_account_count": SocialAccount.objects.filter(person__case=case).count(),
        "instagram_monitor_count": case.instagram_monitors.count(),
        "poi_count": PointOfInterest.objects.filter(person__case=case).count(),
        "active_evidence_count": sum(item["active"] for item in evidence_counts),
        "archived_evidence_count": sum(item["archived"] for item in evidence_counts),
        "chat_count": EvidenceChat.objects.filter(case=case).count(),
        "important_item_count": ImportantItem.objects.filter(case=case).count(),
        "important_note_count": ImportantNote.objects.filter(item__case=case).count(),
    }
    return _render_case(
        request, "cases/delete.html", case, form=form, status=status, **counts
    )


def _invitation_sent(request, case_id, delivered):
    if delivered:
        messages.success(request, "Invitation sent.")
    else:
        messages.error(
            request,
            "Invitation saved, but the email could not be sent. Check email "
            "configuration and use Resend invitation to try again.",
        )
    return redirect("cases:case_settings", case_id=case_id)


@product_login_required
@require_http_methods(["GET", "POST"])
def invitation_create(request, case_id):
    if request.method == "POST":
        case, form, delivered = create_invitation(request.user, case_id, request.POST)
        if delivered is not None:
            return _invitation_sent(request, case.pk, delivered)
    else:
        case = get_case(request.user, case_id, owner=True)
        form = InvitationForm(case=case)
    return _render_case(
        request,
        "cases/invitation_form.html",
        case,
        form=form,
        creating=True,
        invitation_days=settings.PANDECT_INVITATION_DAYS,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def invitation_edit(request, case_id, invitation_id):
    if request.method == "POST":
        case, invitation, form, delivered = edit_invitation(
            request.user, case_id, invitation_id, request.POST
        )
        if delivered is not None:
            return _invitation_sent(request, case.pk, delivered)
    else:
        case = get_case(request.user, case_id, owner=True)
        invitation = get_pending_invitation(case, invitation_id)
        form = InvitationRoleForm(instance=invitation)
    return _render_case(
        request,
        "cases/invitation_form.html",
        case,
        form=form,
        invitation=invitation,
        creating=False,
    )


@product_login_required
@require_http_methods(["POST"])
def invitation_resend(request, case_id, invitation_id):
    delivered = resend_invitation(request.user, case_id, invitation_id)
    return _invitation_sent(request, case_id, delivered)


@product_login_required
@require_http_methods(["GET", "POST"])
def invitation_revoke(request, case_id, invitation_id):
    form = ConfirmForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        revoke_invitation(request.user, case_id, invitation_id)
        messages.success(request, "Invitation revoked.")
        return redirect("cases:case_settings", case_id=case_id)
    case = get_case(request.user, case_id, owner=True)
    invitation = get_object_or_404(case.invitations, pk=invitation_id)
    return _render_case(
        request,
        "cases/access_confirm.html",
        case,
        form=form,
        action="Revoke invitation",
        target=invitation.email,
        explanation="This invitation link will no longer grant access.",
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def membership_edit(request, case_id, membership_id):
    with transaction.atomic():
        case = get_case(
            request.user, case_id, owner=True, lock=request.method == "POST"
        )
        membership = get_object_or_404(
            case.memberships.select_related("user"), pk=membership_id
        )
        form = MembershipRoleForm(
            request.POST if request.method == "POST" else None, instance=membership
        )
        if request.method == "POST" and form.is_valid():
            form.save()
            messages.success(request, "Collaborator access updated.")
            return redirect("cases:case_settings", case_id=case.pk)
    return _render_case(
        request,
        "cases/membership_form.html",
        case,
        form=form,
        membership=membership,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def membership_remove(request, case_id, membership_id):
    form = ConfirmForm(request.POST if request.method == "POST" else None)
    with transaction.atomic():
        case = get_case(
            request.user, case_id, owner=True, lock=request.method == "POST"
        )
        membership = get_object_or_404(
            case.memberships.select_related("user"), pk=membership_id
        )
        target = membership.user.get_username()
        if request.method == "POST" and form.is_valid():
            membership.delete()
            messages.success(
                request, "Collaborator removed. Their evidence is retained."
            )
            return redirect("cases:case_settings", case_id=case.pk)
    return _render_case(
        request,
        "cases/access_confirm.html",
        case,
        form=form,
        action="Remove collaborator",
        target=target,
        explanation=(
            "This person will lose access on subsequent requests. Their uploaded "
            "evidence and attribution remain with the case."
        ),
    )


_INVITATION_UNAVAILABLE = {
    "invalid": "This invitation link is not valid.",
    "wrong_email": (
        "This invitation is for a different verified email address. "
        "Sign in with the invited account, or verify the invited email "
        "in your account settings."
    ),
    "revoked": "This invitation was revoked. Ask the case owner for a new invitation.",
    "used": "This invitation was already used. Ask the case owner if you need access.",
    "expired": "This invitation expired. Ask the case owner to resend it.",
    "owner": "The owner already has access to this case.",
}


@product_login_required
@require_http_methods(["GET", "POST"])
def invitation_accept(request, token):
    try:
        invitation, accepted = resolve_invitation(
            request.user, token, accept=request.method == "POST"
        )
    except InvitationUnavailable as exc:
        account_help = exc.reason == "wrong_email"
        if account_help:
            request.session["pending_invitation_path"] = request.path
        return render(
            request,
            "cases/invitation_unavailable.html",
            {
                "reason": _INVITATION_UNAVAILABLE[exc.reason],
                "account_help": account_help,
            },
            status={"invalid": 404, "wrong_email": 403}.get(exc.reason, 400),
        )
    if accepted:
        messages.success(request, "Invitation accepted. You can now open the case.")
    if invitation.accepted_at:
        return redirect("core:analysis:index", case_id=invitation.case_id)
    return render(request, "cases/invitation_accept.html", {"invitation": invitation})
