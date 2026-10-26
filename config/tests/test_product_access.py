import hashlib
import logging
from datetime import timedelta
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.adapter import PandectAccountAdapter
from cases.models import Case, CaseInvitation
from config.privacy import (
    PrivateAccessLogger,
    PrivatePageMiddleware,
    RedactPrivateURLs,
    redact_url,
)


class ProductAccessTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="product-access", email="access@example.com", password="synthetic"
        )

    def test_anonymous_html_and_enhanced_requests_require_authentication(self):
        self.assertRedirects(
            self.client.get(reverse("cases:case_list")),
            reverse("account_login") + "?next=/cases/",
        )
        response = self.client.get(
            reverse("cases:case_list"), HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "authentication_required")

    def test_forced_unverified_session_cannot_open_case_material(self):
        case = Case.objects.create(owner=self.user, name="Private synthetic case")
        self.client.force_login(self.user)
        response = self.client.get(reverse("cases:case_detail", args=(case.pk,)))
        self.assertRedirects(response, reverse("account_email"))
        response = self.client.get(
            reverse("cases:case_detail", args=(case.pk,)),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, case.name, status_code=403)
        self.assertEqual(response.json()["code"], "verification_required")

    def test_home_and_login_lead_to_cases_after_verification(self):
        EmailAddress.objects.create(
            user=self.user, email=self.user.email, verified=True, primary=True
        )
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get("/"), reverse("cases:case_list"))

    def test_unverified_session_keeps_invitation_through_confirmation(self):
        self.client.force_login(self.user)
        invitation_path = "/invitations/synthetic-token/"
        self.assertRedirects(self.client.get(invitation_path), reverse("account_email"))
        self.assertEqual(
            self.client.session["pending_invitation_path"], invitation_path
        )
        request = RequestFactory().get("/accounts/confirm-email/synthetic/")
        request.session = self.client.session
        adapter = PandectAccountAdapter()
        adapter.request = request
        self.assertEqual(
            adapter.get_email_verification_redirect_url(None), invitation_path
        )
        self.assertEqual(adapter.get_login_redirect_url(request), invitation_path)
        request.session["pending_invitation_path"] = "https://untrusted.example/"
        self.assertIsNone(adapter.pending_invitation(request))

    def test_anonymous_invitation_survives_the_sign_in_flow(self):
        path = "/invitations/synthetic-token/"
        response = self.client.get(path)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["pending_invitation_path"], path)
        EmailAddress.objects.create(
            user=self.user, email=self.user.email, verified=True, primary=True
        )
        response = self.client.post(
            reverse("account_login"),
            {"login": self.user.email, "password": "synthetic"},
        )
        self.assertRedirects(response, path, fetch_redirect_response=False)

    def test_wrong_verified_email_keeps_invitation_for_alias_verification(self):
        owner = get_user_model().objects.create_user(username="inviting-owner")
        case = Case.objects.create(owner=owner, name="Private alias invitation")
        token = "synthetic-alias-token"
        CaseInvitation.objects.create(
            case=case,
            email="invited-alias@example.com",
            invited_by=owner,
            token_digest=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(days=1),
        )
        EmailAddress.objects.create(
            user=self.user, email=self.user.email, verified=True, primary=True
        )
        self.client.force_login(self.user)
        path = reverse("invitation_accept", args=(token,))
        response = self.client.get(path)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.session["pending_invitation_path"], path)
        self.assertNotContains(response, case.name, status_code=403)
        EmailAddress.objects.create(
            user=self.user, email="invited-alias@example.com", verified=True
        )
        self.assertContains(self.client.get(path), "Accept invitation")
        self.assertNotIn("pending_invitation_path", self.client.session)

    def test_private_pages_have_no_store_and_same_origin_referrers(self):
        request = RequestFactory().get("/invitations/synthetic-token/")
        response = PrivatePageMiddleware(lambda request: HttpResponse("Synthetic"))(
            request
        )
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])
        self.assertEqual(response["Referrer-Policy"], "same-origin")

    def test_log_filter_removes_tokens_queries_and_expected_error_tracebacks(self):
        record = logging.LogRecord(
            "django.server",
            logging.WARNING,
            "synthetic.py",
            1,
            '"GET %s HTTP/1.1" 403',
            ("/invitations/synthetic-secret/?q=sensitive-source",),
            None,
        )
        record.status_code = 403
        self.assertTrue(RedactPrivateURLs().filter(record))
        self.assertNotIn("synthetic-secret", record.getMessage())
        self.assertNotIn("sensitive-source", record.getMessage())
        self.assertIn("[redacted]", record.getMessage())
        self.assertEqual(redact_url("/cases/?q=source%20claim&page=2"), "/cases/")

    def test_gunicorn_access_atoms_do_not_contain_tokens_or_queries(self):
        with patch(
            "gunicorn.glogging.Logger.atoms",
            return_value={
                "r": "GET /invitations/raw-secret/?q=source HTTP/1.1",
                "U": "/invitations/raw-secret/",
                "q": "q=source",
                "f": "https://example.com/path?q=private",
            },
        ):
            logger = object.__new__(PrivateAccessLogger)
            result = logger.atoms(None, None, {}, timedelta())
        self.assertNotIn("raw-secret", str(result))
        self.assertNotIn("q=source", str(result))
        self.assertNotIn("q=private", str(result))
