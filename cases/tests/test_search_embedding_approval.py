"""Destination-bound consent for passage indexing and hosted search."""

from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse

from core.analysis.tests.test_gateway import CONFIG
from core.case_search.approval import search_approval_current
from core.models import CaseSearchIndex
from core.testing import EvidenceViewTestMixin


@override_settings(
    PANDECT_SEARCH_EMBEDDING_PROVIDER="openai-gateway", PANDECT_AI_GATEWAY=CONFIG
)
class SearchApprovalTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.login("owner")
        self.settings_url = reverse("cases:case_settings", args=[self.case.pk])
        self.approval_url = reverse("cases:case_search_approval", args=[self.case.pk])

    def form(self, action):
        review = self.client.get(self.settings_url).context["search_approval"]
        return {"action": action, "confirmation": review[f"{action}_confirmation"]}

    def test_owner_can_approve_and_start_then_revoke_independently(self):
        source = self.text()
        analysis_approval = {"revision": "retained-analysis-revision"}
        self.case.external_analysis_approval = analysis_approval
        self.case.save(update_fields=["external_analysis_approval"])
        settings = self.client.get(self.settings_url)
        for label in ("Cloudflare", "OpenAI", "archived", "Search by meaning", "1536"):
            self.assertContains(settings, label)
        self.assertNotContains(settings, CONFIG["gateway_token"])

        approve = self.form("approve")
        response = self.client.post(self.approval_url, approve)
        self.assertRedirects(
            response,
            reverse(
                "core:analysis:processing",
                args=[self.case.pk],
                fragment="case-search-index",
            ),
            fetch_redirect_response=False,
        )
        self.case.refresh_from_db()
        grant = self.case.search_embedding_approval
        revision = grant["revision"]
        self.assertTrue(search_approval_current(self.case, revision=revision))
        self.assertNotIn(CONFIG["gateway_token"], str(grant))
        index = CaseSearchIndex.objects.get(case=self.case)
        self.assertEqual(index.status, "queued")
        self.assertEqual(index.execution["approval_revision"], revision)
        started = index.token
        # A repeated submission cannot replace a healthy delivery.
        self.client.post(self.approval_url, approve)
        index.refresh_from_db()
        self.assertEqual(index.token, started)
        revoke = self.form("revoke")
        self.client.post(self.approval_url, revoke)
        self.case.refresh_from_db()
        index.refresh_from_db()
        self.assertFalse(search_approval_current(self.case, revision=revision))
        self.assertNotEqual(index.token, started)
        self.assertEqual(index.error_code, "embedding_approval_required")
        self.assertEqual(index.status, "failed")
        self.assertEqual(self.case.external_analysis_approval, analysis_approval)
        self.assertEqual(self.case.ai_backend, "local")
        source.refresh_from_db()
        self.assertEqual(source.body, "Original café\r\n  preserved.  ")

        self.client.post(self.approval_url, self.form("approve"))
        self.client.post(self.approval_url, revoke)
        self.case.refresh_from_db()
        self.assertTrue(search_approval_current(self.case))
        self.assertFalse(search_approval_current(self.case, revision=revision))

    def test_only_verified_owner_can_approve_the_reviewed_destination_for_this_case(
        self,
    ):
        approve = self.form("approve")
        for role, expected in (("editor", 403), ("viewer", 403), ("outsider", 404)):
            self.login(role)
            self.assertEqual(
                self.client.post(self.approval_url, approve).status_code, expected
            )
        # The outsider owns this case but was shown a form for a different case.
        self.client.post(
            reverse("cases:case_search_approval", args=[self.other_case.pk]), approve
        )
        self.other_case.refresh_from_db()
        self.assertEqual(self.other_case.search_embedding_approval, {})
        self.login("owner")
        self.assertEqual(self.client.get(self.approval_url).status_code, 405)
        with self.settings(PANDECT_AI_GATEWAY=CONFIG | {"gateway": "replacement"}):
            self.client.post(self.approval_url, approve)
        with self.settings(PANDECT_AI_GATEWAY=CONFIG | {"gateway_token": ""}):
            self.assertNotContains(
                self.client.get(self.settings_url), "Approve and start indexing"
            )
            self.client.post(self.approval_url, self.form("approve"))
        EmailAddress.objects.filter(user=self.users["owner"]).update(verified=False)
        self.assertRedirects(
            self.client.post(self.approval_url, approve),
            reverse("account_email"),
            fetch_redirect_response=False,
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.search_embedding_approval, {})
        self.assertFalse(CaseSearchIndex.objects.exists())

    def test_failed_initial_enqueue_rolls_back_approval_and_index(self):
        approve = self.form("approve")
        with patch("core.case_search.indexing.index_case_passages") as task:
            task.enqueue.side_effect = DatabaseError("queue unavailable")
            response = self.client.post(self.approval_url, approve, follow=True)
        self.assertContains(response, "Indexing could not be queued")
        self.case.refresh_from_db()
        self.assertEqual(self.case.search_embedding_approval, {})
        self.assertFalse(CaseSearchIndex.objects.exists())

    def test_unavailable_profile_during_enqueue_rolls_back_owner_approval(self):
        approve = self.form("approve")
        with patch("core.case_search.indexing.profile", return_value=""):
            response = self.client.post(self.approval_url, approve, follow=True)
        self.assertContains(response, "Indexing could not be queued")
        self.assertContains(response, "Approve and start indexing")
        self.case.refresh_from_db()
        self.assertEqual(self.case.search_embedding_approval, {})
        self.assertFalse(CaseSearchIndex.objects.exists())
