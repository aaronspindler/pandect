"""Investigator-facing case and collaboration workflows."""

import hashlib
import secrets
from datetime import timedelta

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.mail import EmailMessage
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case as CaseExpression
from django.db.models import Count, OuterRef, Q, Subquery, Value, When
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.http import require_http_methods, require_POST

from config.access import product_login_required
from core.analysis.approval import (
    approve_case_execution,
    case_approval_confirmation,
    case_approval_context,
    revoke_case_execution,
)
from core.analysis.execution import configured_external_executions
from core.evidence.types import KINDS
from geo.models import PointOfInterest
from socialmedia import instagram_connector, monitor_jobs
from socialmedia.models import SocialAccount
from ui.live import render_live

from .forms import (
    CaseAIBackendForm,
    CaseForm,
    ConfirmForm,
    InvitationForm,
    InvitationRoleForm,
    MembershipRoleForm,
)
from .models import Case, CaseInvitation, CaseMembership
from .permissions import accessible_cases, case_context, get_case


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
    request, case, *, status=200, form=None, latest_details=None, ai_backend_form=None
):
    invitations = list(case.invitations.all())
    return _render_case(
        request,
        "cases/settings.html",
        case,
        status=status,
        form=form if form is not None else CaseForm(instance=case),
        ai_backend_form=(
            ai_backend_form
            if ai_backend_form is not None
            else CaseAIBackendForm(case=case)
        ),
        latest_details=latest_details,
        memberships=case.memberships.select_related("user").order_by("user__username"),
        pending_invitations=[item for item in invitations if item.is_pending],
        invitation_history=[item for item in invitations if not item.is_pending],
        external_analysis=case_approval_context(case, request.user),
        instagram_scheduling_available=instagram_connector.scheduling_available(),
        instagram_target_approved=monitor_jobs.target_approved(case.pk),
        instagram_target=instagram_connector.PILOT_TARGET,
    )


@product_login_required
@require_POST
def case_ai_backend(request, case_id):
    with transaction.atomic():
        case = get_case(request.user, case_id, owner=True, lock=True)
        form = CaseAIBackendForm(request.POST, case=case)
        if form.is_valid():
            backend = form.cleaned_data["ai_backend"]
            if case.ai_backend != backend:
                case.ai_backend = backend
                case.save(update_fields=("ai_backend",))
            messages.success(request, "AI backend saved.")
            return redirect(
                reverse(
                    "cases:case_settings", args=(case.pk,), fragment="ai-processing"
                )
            )
    return _render_case_settings(request, case, ai_backend_form=form)


@product_login_required
@require_POST
@transaction.atomic
def case_external_analysis(request, case_id):
    case = get_case(request.user, case_id, owner=True, lock=True)
    action = request.POST.get("action")
    if action not in {"approve", "revoke"}:
        raise Http404("Case action not found.")
    executions = configured_external_executions() if action == "approve" else {}
    if not constant_time_compare(
        request.POST.get("confirmation", ""),
        case_approval_confirmation(case, request.user, action, executions),
    ):
        messages.error(request, "Settings changed. Review the current destinations.")
    elif action == "revoke" and case.external_analysis_approval.get("executions"):
        revoke_case_execution(case)
        messages.success(request, "External analysis approval revoked for this case.")
    elif executions and case.external_analysis_approval.get("executions") != executions:
        approve_case_execution(case, request.user, executions)
        messages.success(request, "External analysis approved for this case.")
    return redirect(
        reverse("cases:case_settings", args=(case.pk,), fragment="external-analysis")
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
    if request.method == "POST":
        with transaction.atomic():
            case = get_case(request.user, case_id, owner=True, lock=True)
            if form.is_valid():
                case.delete()
                messages.success(request, "Case deleted. Your account is unchanged.")
                return redirect("cases:case_list")
    else:
        case = get_case(request.user, case_id, owner=True)
    # Deletion removes every case record, including inconsistent relationships
    # hidden from readers. Count those records without retrieving their details.
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
    }
    return _render_case(request, "cases/delete.html", case, form=form, **counts)


def _issue_token(invitation):
    token = secrets.token_urlsafe(32)
    invitation.token_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    invitation.expires_at = timezone.now() + timedelta(
        days=settings.PANDECT_INVITATION_DAYS
    )
    invitation.delivery_failed = False
    invitation.last_sent_at = None
    return token


def _deliver_invitation(request, invitation, token):
    link = settings.PANDECT_PUBLIC_ORIGIN + reverse("invitation_accept", args=(token,))
    try:
        email = EmailMessage(
            subject=f"Invitation to {invitation.case.name}",
            body=(
                f"You have been invited to the Pandect case {invitation.case.name} "
                f"with {invitation.get_role_display()} access.\n\n"
                f"Sign in or create an account, verify this email address, then "
                f"accept the invitation:\n{link}\n\n"
                f"This link expires on {invitation.expires_at:%d %b %Y at %H:%M UTC}.\n"
                "If you did not expect this invitation, you can ignore it."
            ),
            to=[invitation.email],
        )
        if not email.send():
            raise OSError("The mailer did not accept the invitation.")
    except OSError, ImproperlyConfigured, ValueError:
        CaseInvitation.objects.filter(
            pk=invitation.pk, token_digest=invitation.token_digest
        ).update(delivery_failed=True)
        messages.error(
            request,
            "Invitation saved, but the email could not be sent. Check email "
            "configuration and use Resend invitation to try again.",
        )
    else:
        CaseInvitation.objects.filter(
            pk=invitation.pk, token_digest=invitation.token_digest
        ).update(delivery_failed=False, last_sent_at=timezone.now())
        messages.success(request, "Invitation sent.")


@product_login_required
@require_http_methods(["GET", "POST"])
def invitation_create(request, case_id):
    if request.method == "POST":
        with transaction.atomic():
            case = get_case(request.user, case_id, owner=True, lock=True)
            form = InvitationForm(request.POST, case=case)
            if form.is_valid():
                now = timezone.now()
                case.invitations.filter(
                    email__iexact=form.cleaned_data["email"],
                    accepted_at__isnull=True,
                    revoked_at__isnull=True,
                    expires_at__lte=now,
                ).update(revoked_at=now)
                invitation = form.save(commit=False)
                invitation.case = case
                invitation.invited_by = request.user
                token = _issue_token(invitation)
                invitation.save()
            else:
                invitation = None
        if invitation:
            _deliver_invitation(request, invitation, token)
            return redirect("cases:case_settings", case_id=case.pk)
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
        with transaction.atomic():
            case = get_case(request.user, case_id, owner=True, lock=True)
            invitation = get_object_or_404(
                case.invitations.select_for_update(), pk=invitation_id
            )
            if not invitation.is_pending:
                raise Http404("Pending invitation not found.")
            form = InvitationRoleForm(request.POST, instance=invitation)
            if form.is_valid():
                token = _issue_token(invitation)
                form.save()
            else:
                token = None
        if token:
            _deliver_invitation(request, invitation, token)
            return redirect("cases:case_settings", case_id=case.pk)
    else:
        case = get_case(request.user, case_id, owner=True)
        invitation = get_object_or_404(case.invitations, pk=invitation_id)
        if not invitation.is_pending:
            raise Http404("Pending invitation not found.")
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
    with transaction.atomic():
        case = get_case(request.user, case_id, owner=True, lock=True)
        invitation = get_object_or_404(
            case.invitations.select_for_update(), pk=invitation_id
        )
        if not invitation.is_pending:
            raise Http404("Pending invitation not found.")
        token = _issue_token(invitation)
        invitation.save()
    _deliver_invitation(request, invitation, token)
    return redirect("cases:case_settings", case_id=case.pk)


@product_login_required
@require_http_methods(["GET", "POST"])
def invitation_revoke(request, case_id, invitation_id):
    form = ConfirmForm(request.POST if request.method == "POST" else None)
    with transaction.atomic():
        case = get_case(
            request.user, case_id, owner=True, lock=request.method == "POST"
        )
        invitation = get_object_or_404(case.invitations, pk=invitation_id)
        if request.method == "POST" and form.is_valid():
            if invitation.is_pending:
                invitation.revoked_at = timezone.now()
                invitation.save(update_fields=["revoked_at"])
            messages.success(request, "Invitation revoked.")
            return redirect("cases:case_settings", case_id=case.pk)
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


def _invitation_unavailable(request, reason, status=400, *, account_help=False):
    return render(
        request,
        "cases/invitation_unavailable.html",
        {"reason": reason, "account_help": account_help},
        status=status,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def invitation_accept(request, token):
    is_post = request.method == "POST"
    if len(token) > 128:
        return _invitation_unavailable(
            request, "This invitation link is not valid.", 404
        )
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    candidate = (
        CaseInvitation.objects.filter(token_digest=digest).values("case_id").first()
    )
    if not candidate:
        return _invitation_unavailable(
            request, "This invitation link is not valid.", 404
        )
    with transaction.atomic():
        # Acceptance cannot use get_case: the invitee does not have access yet.
        cases = Case.objects.all()
        invitations = CaseInvitation.objects.all()
        if is_post:
            cases = cases.select_for_update()
            invitations = invitations.select_for_update()
        case = get_object_or_404(cases, pk=candidate["case_id"])
        invitation = get_object_or_404(invitations, token_digest=digest, case=case)
        recipient_matches = EmailAddress.objects.filter(
            user=request.user, email__iexact=invitation.email, verified=True
        ).exists()
        if not recipient_matches:
            request.session["pending_invitation_path"] = request.path
            return _invitation_unavailable(
                request,
                "This invitation is for a different verified email address. "
                "Sign in with the invited account, or verify the invited email "
                "in your account settings.",
                403,
                account_help=True,
            )
        if invitation.revoked_at:
            return _invitation_unavailable(
                request,
                "This invitation was revoked. Ask the case owner for a new invitation.",
            )
        existing = CaseMembership.objects.filter(case=case, user=request.user).first()
        if invitation.accepted_at:
            if invitation.accepted_by_id == request.user.pk and existing:
                return redirect("core:analysis:index", case_id=case.pk)
            return _invitation_unavailable(
                request,
                "This invitation was already used. "
                "Ask the case owner if you need access.",
            )
        if invitation.expires_at <= timezone.now():
            return _invitation_unavailable(
                request, "This invitation expired. Ask the case owner to resend it."
            )
        if request.user.pk == case.owner_id:
            return _invitation_unavailable(
                request, "The owner already has access to this case."
            )
        if is_post:
            if not existing:
                # The case lock serializes product mutations. get_or_create also
                # tolerates a concurrent authorized admin insert without changing
                # the role chosen by that operation.
                CaseMembership.objects.get_or_create(
                    case=case,
                    user=request.user,
                    defaults={"role": invitation.role},
                )
            invitation.accepted_at = timezone.now()
            invitation.accepted_by = request.user
            invitation.save(update_fields=["accepted_at", "accepted_by"])
            messages.success(request, "Invitation accepted. You can now open the case.")
            return redirect("core:analysis:index", case_id=case.pk)
    return render(request, "cases/invitation_accept.html", {"invitation": invitation})
