import hashlib
from datetime import timedelta

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cases.models import Case, CaseInvitation, CaseMembership


class AuthenticationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "synthetic-correct-horse-742!"
        cls.user = get_user_model().objects.create_user(
            username="investigator",
            email="investigator@example.com",
            password=cls.password,
        )
        EmailAddress.objects.create(
            user=cls.user, email=cls.user.email, primary=True, verified=True
        )

    def test_user_password_is_hashed_and_custom_model_is_active(self):
        self.assertEqual(self.user._meta.label, "accounts.User")
        self.assertNotEqual(self.user.password, self.password)
        self.assertTrue(self.user.check_password(self.password))

    @override_settings(PANDECT_ALLOW_SIGNUPS=False)
    def test_verified_user_can_sign_in_with_email_when_signups_are_closed(self):
        response = self.client.post(
            reverse("account_login"),
            {"login": self.user.email, "password": self.password},
        )
        self.assertRedirects(response, reverse("cases:case_list"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))
        self.assertEqual(
            self.client.get(reverse("account_email")).wsgi_request.user, self.user
        )

    def test_primary_verified_email_has_no_redundant_management_actions(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("account_email"))
        self.assertContains(response, self.user.email)
        self.assertContains(response, 'name="action_add"')
        for action in ("action_primary", "action_send", "action_remove"):
            self.assertNotContains(response, f'name="{action}"')

    def test_secondary_email_controls_preserve_allauth_verification_and_actions(self):
        self.client.force_login(self.user)
        secondary = EmailAddress.objects.create(
            user=self.user, email="secondary@example.com"
        )
        url = reverse("account_email")
        response = self.client.get(url)
        for action in ("action_send", "action_remove"):
            self.assertContains(response, f'name="{action}"', count=1)
        self.assertNotContains(response, 'name="action_primary"')
        self.assertContains(response, "Remove secondary@example.com from your account?")
        self.client.post(url, {"email": secondary.email, "action_send": ""})
        self.assertEqual(mail.outbox[-1].to, [secondary.email])
        self.client.post(url, {"email": secondary.email, "action_primary": ""})
        secondary.refresh_from_db()
        self.assertFalse(secondary.primary)
        secondary.verified = True
        secondary.save(update_fields=["verified"])
        response = self.client.get(url)
        self.assertContains(response, 'name="action_primary"', count=1)
        self.assertNotContains(response, 'name="action_send"')
        self.client.post(url, {"email": secondary.email, "action_primary": ""})
        secondary.refresh_from_db()
        self.assertTrue(secondary.primary)
        self.client.post(url, {"email": self.user.email, "action_remove": ""})
        self.assertFalse(
            EmailAddress.objects.filter(user=self.user, email=self.user.email).exists()
        )
        self.assertTrue(EmailAddress.objects.filter(pk=secondary.pk).exists())

    def test_incorrect_password_does_not_create_session(self):
        response = self.client.post(
            reverse("account_login"),
            {"login": self.user.username, "password": "incorrect-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_unverified_user_must_confirm_email_before_signing_in(self):
        EmailAddress.objects.filter(user=self.user).update(verified=False)
        response = self.client.post(
            reverse("account_login"),
            {"login": self.user.username, "password": self.password},
        )
        self.assertRedirects(response, reverse("account_email_verification_sent"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(mail.outbox[0].to, [self.user.email])

    @override_settings(PANDECT_ALLOW_SIGNUPS=True)
    def test_signup_creates_custom_user_and_requires_email_verification(self):
        login = self.client.get(reverse("account_login"))
        self.assertContains(login, f'href="{reverse("account_signup")}"')
        response = self.client.post(
            reverse("account_signup"),
            {
                "username": "new-investigator",
                "email": "new@example.com",
                "password1": self.password,
                "password2": self.password,
            },
        )
        self.assertRedirects(response, reverse("account_email_verification_sent"))
        user = get_user_model().objects.get(username="new-investigator")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(EmailAddress.objects.get(user=user).verified)
        self.assertEqual(mail.outbox[0].to, ["new@example.com"])

    @override_settings(PANDECT_ALLOW_SIGNUPS=False)
    def test_closed_registration_blocks_get_and_post_even_with_pending_invitation(self):
        session = self.client.session
        session["pending_invitation_path"] = "/invitations/synthetic-token/"
        session.save()
        before = get_user_model().objects.count()
        for method in ("get", "post"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(
                    reverse("account_signup"),
                    {
                        "username": "closed-investigator",
                        "email": "closed@example.com",
                        "password1": self.password,
                        "password2": self.password,
                    },
                )
                self.assertContains(response, "Registration is closed")
                self.assertNotContains(response, "<form")
                self.assertNotContains(response, f'href="{reverse("account_signup")}"')
                self.assertEqual(get_user_model().objects.count(), before)
                self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(mail.outbox, [])

    @override_settings(PANDECT_ALLOW_SIGNUPS=False)
    def test_closed_registration_login_explains_availability_without_signup_links(self):
        response = self.client.get(reverse("account_login"))
        self.assertContains(response, "Account registration is currently closed")
        self.assertNotContains(response, f'href="{reverse("account_signup")}"')
        self.assertContains(response, 'name="login"')
        self.assertContains(response, 'type="submit"')

    @override_settings(PANDECT_ALLOW_SIGNUPS=False)
    def test_existing_invitee_can_sign_in_and_accept_when_signups_are_closed(self):
        owner = get_user_model().objects.create_user(
            username="case-owner", email="case-owner@example.com"
        )
        case = Case.objects.create(owner=owner, name="Synthetic invited case")
        token = "synthetic-invitation-token"
        invitation = CaseInvitation.objects.create(
            case=case,
            email=self.user.email,
            invited_by=owner,
            token_digest=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(days=1),
        )
        invitation_url = reverse("invitation_accept", args=(token,))
        response = self.client.get(invitation_url)
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            reverse("account_login"),
            {"login": self.user.email, "password": self.password},
        )
        self.assertRedirects(response, invitation_url)
        self.assertFalse(
            CaseMembership.objects.filter(case=case, user=self.user).exists()
        )
        response = self.client.post(invitation_url)
        self.assertRedirects(response, reverse("cases:case_detail", args=(case.pk,)))
        self.assertTrue(
            CaseMembership.objects.filter(case=case, user=self.user).exists()
        )
        invitation.refresh_from_db()
        self.assertEqual(invitation.accepted_by, self.user)

    def test_login_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            reverse("account_login"),
            {"login": self.user.username, "password": self.password},
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("_auth_user_id", client.session)

    def test_logout_requires_post(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("account_logout")).status_code, 200)
        self.assertIn("_auth_user_id", self.client.session)
        self.client.post(reverse("account_logout"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_admin_requires_staff_access(self):
        response = self.client.get(reverse("admin:index"))
        self.assertRedirects(
            response, f"{reverse('admin:login')}?next={reverse('admin:index')}"
        )
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 302)

    def test_superuser_can_access_admin(self):
        superuser = get_user_model().objects.create_superuser(
            username="operator", email="operator@example.com", password=self.password
        )
        self.client.force_login(superuser)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
