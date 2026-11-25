"""Issue, deliver, and accept case collaboration invitations."""

import hashlib
import secrets
from datetime import timedelta

from allauth.account.models import EmailAddress
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMessage
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone

from .forms import InvitationForm, InvitationRoleForm
from .models import Case, CaseInvitation, CaseMembership
from .permissions import get_case


class InvitationUnavailable(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_token(invitation):
    token = secrets.token_urlsafe(32)
    invitation.token_digest = _digest(token)
    invitation.expires_at = timezone.now() + timedelta(
        days=settings.PANDECT_INVITATION_DAYS
    )
    invitation.delivery_failed = False
    invitation.last_sent_at = None
    return token


def _deliver(invitation, token):
    link = settings.PANDECT_PUBLIC_ORIGIN + reverse("invitation_accept", args=(token,))
    issued = CaseInvitation.objects.filter(
        pk=invitation.pk, token_digest=invitation.token_digest
    )
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
        issued.update(delivery_failed=True)
        return False
    issued.update(delivery_failed=False, last_sent_at=timezone.now())
    return True


def create_invitation(actor, case_id, data):
    with transaction.atomic():
        case = get_case(actor, case_id, owner=True, lock=True)
        form = InvitationForm(data, case=case)
        if not form.is_valid():
            return case, form, None
        now = timezone.now()
        case.invitations.filter(
            email__iexact=form.cleaned_data["email"],
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__lte=now,
        ).update(revoked_at=now)
        invitation = form.save(commit=False)
        invitation.case = case
        invitation.invited_by = actor
        token = _issue_token(invitation)
        invitation.save()
    return case, form, _deliver(invitation, token)


def get_pending_invitation(case, invitation_id, *, lock=False):
    invitations = case.invitations.all()
    if lock:
        invitations = invitations.select_for_update()
    invitation = get_object_or_404(invitations, pk=invitation_id)
    if not invitation.is_pending:
        raise Http404("Pending invitation not found.")
    return invitation


def edit_invitation(actor, case_id, invitation_id, data):
    with transaction.atomic():
        case = get_case(actor, case_id, owner=True, lock=True)
        invitation = get_pending_invitation(case, invitation_id, lock=True)
        form = InvitationRoleForm(data, instance=invitation)
        if not form.is_valid():
            return case, invitation, form, None
        token = _issue_token(invitation)
        form.save()
    return case, invitation, form, _deliver(invitation, token)


def resend_invitation(actor, case_id, invitation_id):
    with transaction.atomic():
        case = get_case(actor, case_id, owner=True, lock=True)
        invitation = get_pending_invitation(case, invitation_id, lock=True)
        token = _issue_token(invitation)
        invitation.save()
    return _deliver(invitation, token)


@transaction.atomic
def revoke_invitation(actor, case_id, invitation_id):
    case = get_case(actor, case_id, owner=True, lock=True)
    invitation = get_object_or_404(case.invitations, pk=invitation_id)
    if invitation.is_pending:
        invitation.revoked_at = timezone.now()
        invitation.save(update_fields=["revoked_at"])


def resolve_invitation(actor, token, *, accept=False):
    """Inspect or accept an invitation, returning whether it was newly accepted."""
    if len(token) > 128:
        raise InvitationUnavailable("invalid")
    digest = _digest(token)
    candidate = (
        CaseInvitation.objects.filter(token_digest=digest).values("case_id").first()
    )
    if not candidate:
        raise InvitationUnavailable("invalid")
    with transaction.atomic():
        # The invitee has no case access yet. Lock the case before its invitation.
        cases = Case.objects.all()
        invitations = CaseInvitation.objects.all()
        if accept:
            cases = cases.select_for_update()
            invitations = invitations.select_for_update()
        case = get_object_or_404(cases, pk=candidate["case_id"])
        invitation = get_object_or_404(invitations, token_digest=digest, case=case)
        if not EmailAddress.objects.filter(
            user_id=actor.pk,
            user__is_active=True,
            email__iexact=invitation.email,
            verified=True,
        ).exists():
            raise InvitationUnavailable("wrong_email")
        if invitation.revoked_at:
            raise InvitationUnavailable("revoked")
        existing = CaseMembership.objects.filter(case=case, user=actor).first()
        if invitation.accepted_at:
            if invitation.accepted_by_id == actor.pk and existing:
                return invitation, False
            raise InvitationUnavailable("used")
        if invitation.expires_at <= timezone.now():
            raise InvitationUnavailable("expired")
        if actor.pk == case.owner_id:
            raise InvitationUnavailable("owner")
        if accept:
            if not existing:
                # Keep the role from any concurrent authorized admin insert.
                CaseMembership.objects.get_or_create(
                    case=case, user=actor, defaults={"role": invitation.role}
                )
            invitation.accepted_at = timezone.now()
            invitation.accepted_by = actor
            invitation.save(update_fields=["accepted_at", "accepted_by"])
    return invitation, accept
