"""Manage case invitations and investigator access."""

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from config.access import product_login_required

from .forms import (
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
from .permissions import get_case
from .responses import render_case


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
    return render_case(
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
    return render_case(
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
    return render_case(
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
    return render_case(
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
    return render_case(
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
