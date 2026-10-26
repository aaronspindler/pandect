import hashlib
from datetime import timedelta
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cases.models import Case, CaseInvitation, CaseMembership


@override_settings(PANDECT_PUBLIC_ORIGIN="https://pandect.example")
class CaseInterfaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for name in ("owner", "viewer", "editor", "outsider", "invitee"):
            user = get_user_model().objects.create_user(
                username=name,
                email=f"{name}@example.com",
                password="synthetic-pass-753!",
            )
            EmailAddress.objects.create(
                user=user, email=user.email, verified=True, primary=True
            )
            setattr(cls, name, user)
        cls.case = Case.objects.create(owner=cls.owner, name="Harbour records")
        cls.other_case = Case.objects.create(
            owner=cls.outsider, name="Private unrelated"
        )
        cls.viewer_membership = CaseMembership.objects.create(
            case=cls.case, user=cls.viewer
        )
        cls.editor_membership = CaseMembership.objects.create(
            case=cls.case, user=cls.editor, role="editor"
        )

    def url(self, name, *args):
        return reverse(f"cases:{name}", args=args or (self.case.pk,))

    def login(self, user=None):
        self.client.force_login(user or self.owner)

    def invite(self, *, email=None, role="viewer"):
        self.login()
        response = self.client.post(
            self.url("invitation_create"),
            {"email": email or self.invitee.email, "role": role},
        )
        self.assertEqual(response.status_code, 302)
        invitation = CaseInvitation.objects.latest("created_at")
        token = mail.outbox[-1].body.split("/invitations/", 1)[1].split("/", 1)[0]
        return invitation, token

    def test_case_list_is_currently_scoped_even_for_superusers(self):
        self.login(self.viewer)
        response = self.client.get(reverse("cases:case_list"))
        self.assertContains(response, self.case.name)
        self.assertNotContains(response, self.other_case.name)
        self.viewer.is_superuser = True
        self.viewer.save()
        self.assertEqual(
            self.client.get(self.url("case_detail", self.other_case.pk)).status_code,
            404,
        )
        self.viewer_membership.delete()
        self.assertEqual(self.client.get(self.url("case_detail")).status_code, 404)

    def test_create_sets_current_owner_and_requires_a_name(self):
        self.login(self.viewer)
        response = self.client.post(
            reverse("cases:case_create"),
            {"name": "New investigation", "owner": str(self.outsider.pk)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Case.objects.get(name="New investigation").owner, self.viewer)
        response = self.client.post(reverse("cases:case_create"), {"name": "   "})
        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)

    def test_owner_only_case_mutations(self):
        for user in (self.editor, self.viewer):
            self.login(user)
            for route in ("case_settings", "case_delete", "invitation_create"):
                with self.subTest(user=user.username, route=route):
                    self.assertEqual(self.client.get(self.url(route)).status_code, 403)
                    self.assertEqual(
                        self.client.post(
                            self.url(route), {"confirm": "on"}
                        ).status_code,
                        403,
                    )
        self.case.refresh_from_db()
        self.assertEqual(self.case.owner, self.owner)

    def test_case_edit_detects_conflict_and_preserves_draft(self):
        self.login()
        revision = self.case.updated_at.isoformat()
        self.case.description = "Newer saved details"
        self.case.save()
        response = self.client.post(
            self.url("case_settings"),
            {
                "name": "Draft case name",
                "description": "My draft",
                "expected_updated_at": revision,
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "My draft", status_code=409)
        self.assertContains(response, "Newer saved details", status_code=409)
        self.assertContains(response, "Save reviewed changes", status_code=409)
        reviewed_revision = response.context["form"]["expected_updated_at"].value()
        self.case.refresh_from_db()
        self.assertEqual(self.case.name, "Harbour records")
        self.assertEqual(self.case.description, "Newer saved details")
        self.assertEqual(reviewed_revision, self.case.updated_at.isoformat())
        response = self.client.post(
            self.url("case_settings"),
            {
                "name": "Draft case name",
                "description": "My draft",
                "expected_updated_at": reviewed_revision,
                "owner": self.outsider.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.name, "Draft case name")
        self.assertEqual(self.case.description, "My draft")
        self.assertEqual(self.case.owner, self.owner)

    def test_case_edit_updates_timestamp_and_preserves_owner(self):
        self.login()
        before = self.case.updated_at
        response = self.client.post(
            self.url("case_settings"),
            {
                "name": "Updated harbour",
                "description": "Synthetic",
                "expected_updated_at": before.isoformat(),
                "owner": self.outsider.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.name, "Updated harbour")
        self.assertEqual(self.case.owner, self.owner)
        self.assertGreater(self.case.updated_at, before)

    def test_delete_needs_confirmation_and_keeps_accounts(self):
        self.login()
        response = self.client.post(self.url("case_delete"), {})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Case.objects.filter(pk=self.case.pk).exists())
        response = self.client.post(self.url("case_delete"), {"confirm": "on"})
        self.assertRedirects(response, reverse("cases:case_list"))
        self.assertFalse(Case.objects.filter(pk=self.case.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.owner.pk).exists())

    def test_csrf_required_for_product_mutations(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(
            client.post(self.url("case_delete"), {"confirm": "on"}).status_code, 403
        )

    def test_new_invitation_hashes_token_and_sends_configured_origin(self):
        invitation, token = self.invite(role="editor")
        self.assertEqual(invitation.role, "editor")
        self.assertEqual(
            invitation.token_digest, hashlib.sha256(token.encode()).hexdigest()
        )
        self.assertNotEqual(invitation.token_digest, token)
        self.assertEqual(mail.outbox[-1].to, [self.invitee.email])
        self.assertIn("https://pandect.example/invitations/", mail.outbox[-1].body)
        self.assertFalse(invitation.delivery_failed)
        self.assertIsNotNone(invitation.last_sent_at)
        self.assertLess(
            abs((invitation.expires_at - timezone.now()).total_seconds() - 7 * 86400),
            10,
        )

    def test_get_accept_does_not_grant_access_and_post_accepts_once(self):
        invitation, token = self.invite(role="editor")
        self.login(self.invitee)
        url = reverse("invitation_accept", args=(token,))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertFalse(
            CaseMembership.objects.filter(case=self.case, user=self.invitee).exists()
        )
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertEqual(self.client.post(url).status_code, 302)
        membership = CaseMembership.objects.get(case=self.case, user=self.invitee)
        self.assertEqual(membership.role, "editor")
        self.assertEqual(
            CaseMembership.objects.filter(case=self.case, user=self.invitee).count(), 1
        )
        invitation.refresh_from_db()
        self.assertEqual(invitation.accepted_by, self.invitee)
        self.assertIsNotNone(invitation.accepted_at)

    def test_revoked_accepted_access_cannot_be_restored_by_replay(self):
        _, token = self.invite()
        self.login(self.invitee)
        url = reverse("invitation_accept", args=(token,))
        self.client.post(url)
        CaseMembership.objects.filter(case=self.case, user=self.invitee).delete()
        self.assertEqual(self.client.post(url).status_code, 400)
        self.assertFalse(
            CaseMembership.objects.filter(case=self.case, user=self.invitee).exists()
        )

    def test_acceptance_does_not_change_existing_member_role(self):
        _, token = self.invite(role="editor")
        CaseMembership.objects.create(case=self.case, user=self.invitee, role="viewer")
        self.login(self.invitee)
        self.client.post(reverse("invitation_accept", args=(token,)))
        self.assertEqual(
            CaseMembership.objects.get(case=self.case, user=self.invitee).role, "viewer"
        )

    def test_wrong_account_cannot_see_case_or_accept(self):
        _, token = self.invite()
        self.login(self.outsider)
        response = self.client.post(reverse("invitation_accept", args=(token,)))
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "Harbour records", status_code=403)
        self.assertFalse(
            CaseMembership.objects.filter(case=self.case, user=self.outsider).exists()
        )

    def test_invited_address_itself_must_be_verified(self):
        _, token = self.invite()
        EmailAddress.objects.filter(user=self.invitee).update(verified=False)
        EmailAddress.objects.create(
            user=self.invitee, email="other-invitee@example.com", verified=True
        )
        self.login(self.invitee)
        self.assertEqual(
            self.client.post(reverse("invitation_accept", args=(token,))).status_code,
            403,
        )

    def test_expired_revoked_invalid_and_deleted_case_links(self):
        invitation, token = self.invite()
        self.login(self.invitee)
        url = reverse("invitation_accept", args=(token,))
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save()
        self.assertEqual(self.client.post(url).status_code, 400)
        invitation.expires_at = timezone.now() + timedelta(days=1)
        invitation.revoked_at = timezone.now()
        invitation.save()
        self.assertEqual(self.client.post(url).status_code, 400)
        self.assertEqual(
            self.client.get(
                reverse("invitation_accept", args=("invalid",))
            ).status_code,
            404,
        )
        self.case.delete()
        self.assertEqual(self.client.post(url).status_code, 404)

    def test_resend_and_role_change_rotate_token(self):
        invitation, original_token = self.invite()
        response = self.client.post(
            self.url("invitation_resend", self.case.pk, invitation.pk)
        )
        self.assertEqual(response.status_code, 302)
        invitation.refresh_from_db()
        resent_digest = invitation.token_digest
        self.assertNotEqual(
            resent_digest, hashlib.sha256(original_token.encode()).hexdigest()
        )
        response = self.client.post(
            self.url("invitation_edit", self.case.pk, invitation.pk), {"role": "editor"}
        )
        self.assertEqual(response.status_code, 302)
        invitation.refresh_from_db()
        self.assertNotEqual(invitation.token_digest, resent_digest)
        self.assertEqual(invitation.role, "editor")
        self.login(self.invitee)
        self.assertEqual(
            self.client.get(
                reverse("invitation_accept", args=(original_token,))
            ).status_code,
            404,
        )

    def test_email_failure_keeps_pending_invitation_and_can_retry(self):
        self.login()
        with patch(
            "cases.views.EmailMessage.send", side_effect=OSError("synthetic failure")
        ):
            response = self.client.post(
                self.url("invitation_create"),
                {"email": self.invitee.email, "role": "viewer"},
            )
        self.assertEqual(response.status_code, 302)
        invitation = self.case.invitations.get()
        self.assertTrue(invitation.delivery_failed)
        self.assertTrue(invitation.is_pending)
        response = self.client.post(
            self.url("invitation_resend", self.case.pk, invitation.pk)
        )
        self.assertEqual(response.status_code, 302)
        invitation.refresh_from_db()
        self.assertFalse(invitation.delivery_failed)
        self.assertIsNotNone(invitation.last_sent_at)

    def test_owner_current_member_and_duplicate_pending_invites_rejected(self):
        self.login()
        for email in (self.owner.email, self.viewer.email, self.editor.email):
            response = self.client.post(
                self.url("invitation_create"),
                {"email": email.upper(), "role": "viewer"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("email", response.context["form"].errors)
        invitation, _ = self.invite()
        response = self.client.post(
            self.url("invitation_create"),
            {"email": self.invitee.email.upper(), "role": "editor"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.case.invitations.count(), 1)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            CaseInvitation.objects.create(
                case=self.case,
                email=self.invitee.email.upper(),
                role="viewer",
                invited_by=self.owner,
                expires_at=timezone.now() + timedelta(days=1),
                token_digest="b" * 64,
            )
        self.assertEqual(self.case.invitations.get().pk, invitation.pk)

    def test_expired_invitation_can_be_replaced(self):
        invitation, _ = self.invite()
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save()
        self.login()
        response = self.client.post(
            self.url("invitation_create"),
            {"email": self.invitee.email, "role": "editor"},
        )
        self.assertEqual(response.status_code, 302)
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.revoked_at)
        self.assertEqual(
            self.case.invitations.filter(
                accepted_at__isnull=True, revoked_at__isnull=True
            ).count(),
            1,
        )

    def test_invitation_and_member_ids_are_scoped_to_case(self):
        self.login(self.outsider)
        response = self.client.post(
            self.url(
                "membership_remove", self.other_case.pk, self.viewer_membership.pk
            ),
            {"confirm": "on"},
        )
        self.assertEqual(response.status_code, 404)
        invitation, _ = self.invite()
        self.login(self.outsider)
        response = self.client.post(
            self.url("invitation_resend", self.other_case.pk, invitation.pk)
        )
        self.assertEqual(response.status_code, 404)

    def test_membership_changes_revoke_and_downgrade_on_subsequent_request(self):
        self.login()
        response = self.client.post(
            self.url("membership_edit", self.case.pk, self.editor_membership.pk),
            {"role": "viewer"},
        )
        self.assertEqual(response.status_code, 302)
        self.editor_membership.refresh_from_db()
        self.assertEqual(self.editor_membership.role, "viewer")
        self.client.post(
            self.url("membership_remove", self.case.pk, self.editor_membership.pk),
            {"confirm": "on"},
        )
        self.login(self.editor)
        self.assertEqual(self.client.get(self.url("case_detail")).status_code, 404)

    def test_revoke_requires_confirmation_and_invalidates_link(self):
        invitation, token = self.invite()
        url = self.url("invitation_revoke", self.case.pk, invitation.pk)
        self.assertEqual(self.client.post(url, {}).status_code, 200)
        invitation.refresh_from_db()
        self.assertIsNone(invitation.revoked_at)
        self.assertEqual(self.client.post(url, {"confirm": "on"}).status_code, 302)
        self.login(self.invitee)
        self.assertEqual(
            self.client.post(reverse("invitation_accept", args=(token,))).status_code,
            400,
        )
