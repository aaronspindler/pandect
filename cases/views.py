"""Investigator-facing case and collaboration workflows."""

import hashlib
import secrets
from datetime import timedelta
from smtplib import SMTPException
from urllib.parse import urlsplit

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMessage
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from config.access import product_login_required

from .forms import (
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
            "invitation_days": settings.PANDECT_INVITATION_DAYS,
            **context,
        },
        status=status,
    )


def _evidence_models():
    from evidence.models import (
        AudioEvidence,
        DocumentEvidence,
        FaceTimeEvidence,
        ImageEvidence,
        LocationEvidence,
        PersonLocationEvidence,
        PhoneCallEvidence,
        TextEvidence,
        TextMessageEvidence,
        VehicleLocationEvidence,
    )

    return (
        ("image", ImageEvidence),
        ("audio", AudioEvidence),
        ("document", DocumentEvidence),
        ("text", TextEvidence),
        ("location", LocationEvidence),
        ("person-location", PersonLocationEvidence),
        ("vehicle-location", VehicleLocationEvidence),
        ("text-message", TextMessageEvidence),
        ("phone-call", PhoneCallEvidence),
        ("facetime", FaceTimeEvidence),
    )


def _case_summary(case):
    active_count = archived_count = 0
    recent = []
    for kind, model in _evidence_models():
        queryset = model.objects.filter(case=case)
        active_count += queryset.filter(is_archived=False).count()
        archived_count += queryset.filter(is_archived=True).count()
        for item in queryset.order_by("-created_at", "-pk").values(
            "pk", "title", "created_at"
        )[:5]:
            recent.append(
                {
                    "title": item["title"],
                    "created_at": item["created_at"],
                    "kind": "FaceTime"
                    if kind == "facetime"
                    else kind.replace("-", " ").title(),
                    "url": reverse("evidence:detail", args=(case.pk, kind, item["pk"])),
                }
            )
    recent.sort(key=lambda item: (item["created_at"], item["url"]), reverse=True)
    return {
        "active_evidence_count": active_count,
        "archived_evidence_count": archived_count,
        "people_count": case.people.count(),
        "vehicles_count": case.vehicles.count(),
        "collaborator_count": case.memberships.count(),
        "pending_invitation_count": case.invitations.filter(
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).count(),
        "recent_evidence": recent[:5],
    }


@product_login_required
@require_http_methods(["GET"])
def case_list(request):
    query = request.GET.get("q", "")[:200].strip()
    ownership = request.GET.get("ownership", "all")
    queryset = accessible_cases(request.user).select_related("owner")
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
    roles = dict(
        CaseMembership.objects.filter(user=request.user).values_list("case_id", "role")
    )
    for case in page:
        case.current_role = (
            "owner" if case.owner_id == request.user.pk else roles[case.pk]
        )
    return render(
        request,
        "cases/list.html",
        {"page_obj": page, "query": query, "ownership": ownership},
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def case_create(request):
    form = CaseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        case = form.save(commit=False)
        case.owner = request.user
        case.save()
        messages.success(
            request, "Case created. Add evidence to begin your investigation."
        )
        return redirect("cases:case_detail", case_id=case.pk)
    return render(request, "cases/form.html", {"form": form, "creating": True})


@product_login_required
@require_http_methods(["GET"])
def case_detail(request, case_id):
    case = get_case(request.user, case_id)
    return _render_case(request, "cases/detail.html", case, **_case_summary(case))


@product_login_required
@require_http_methods(["GET", "POST"])
def case_settings(request, case_id):
    status = 200
    latest_details = None
    if request.method == "POST":
        with transaction.atomic():
            case = get_case(request.user, case_id, owner=True, lock=True)
            saved_updated_at = case.updated_at.isoformat()
            saved_details = {"name": case.name, "description": case.description}
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
    return _render_case(
        request,
        "cases/settings.html",
        case,
        status=status,
        form=form,
        latest_details=latest_details,
        memberships=case.memberships.select_related("user").order_by("user__username"),
        invitations=case.invitations.select_related("invited_by"),
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def case_delete(request, case_id):
    form = ConfirmForm(request.POST or None)
    if request.method == "POST":
        with transaction.atomic():
            case = get_case(request.user, case_id, owner=True, lock=True)
            if form.is_valid():
                case.delete()
                messages.success(request, "Case deleted. Your account is unchanged.")
                return redirect("cases:case_list")
    else:
        case = get_case(request.user, case_id, owner=True)
    return _render_case(
        request, "cases/delete.html", case, form=form, **_case_summary(case)
    )


def _issue_token(invitation):
    token = secrets.token_urlsafe(32)
    invitation.token_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    invitation.expires_at = timezone.now() + timedelta(
        days=getattr(settings, "PANDECT_INVITATION_DAYS", 7)
    )
    invitation.delivery_failed = False
    invitation.last_sent_at = None
    return token


def _deliver_invitation(request, invitation, token):
    origin = getattr(settings, "PANDECT_PUBLIC_ORIGIN", "http://localhost:8000").rstrip(
        "/"
    )
    parsed = urlsplit(origin)
    try:
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ImproperlyConfigured("Configure PANDECT_PUBLIC_ORIGIN.")
        link = origin + reverse("invitation_accept", args=(token,))
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
    except SMTPException, OSError, ImproperlyConfigured, ValueError:
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
        request, "cases/invitation_form.html", case, form=form, creating=True
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
    form = ConfirmForm(request.POST or None)
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
        form = MembershipRoleForm(request.POST or None, instance=membership)
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
    form = ConfirmForm(request.POST or None)
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


def _invitation_unavailable(request, reason, status=400):
    response = render(
        request, "cases/invitation_unavailable.html", {"reason": reason}, status=status
    )
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "same-origin"
    return response


@product_login_required
@require_http_methods(["GET", "POST"])
def invitation_accept(request, token):
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
        case = get_object_or_404(
            Case.objects.select_for_update(), pk=candidate["case_id"]
        )
        invitation = get_object_or_404(
            CaseInvitation.objects.select_for_update(), token_digest=digest, case=case
        )
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
            )
        if invitation.revoked_at:
            return _invitation_unavailable(
                request,
                "This invitation was revoked. Ask the case owner for a new invitation.",
            )
        existing = CaseMembership.objects.filter(case=case, user=request.user).first()
        if invitation.accepted_at:
            if invitation.accepted_by_id == request.user.pk and existing:
                return redirect("cases:case_detail", case_id=case.pk)
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
        if request.method == "POST":
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
            return redirect("cases:case_detail", case_id=case.pk)
    response = render(
        request, "cases/invitation_accept.html", {"invitation": invitation}
    )
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response
