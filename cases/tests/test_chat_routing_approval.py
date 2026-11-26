from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.evidence_chat import decisions
from core.evidence_chat.decisions_approval import scope
from core.models import ChatRoutingApproval
from core.testing import EvidenceViewTestMixin


@override_settings(PANDECT_DIRECT_AI={"openai_api_key": "synthetic-key"})
class ChatRoutingApprovalTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.login("owner")
        self.settings_url = reverse("cases:case_settings", args=[self.case.pk])
        self.change_url = reverse("cases:case_chat_routing", args=[self.case.pk])

    def form(self, action):
        ctx = self.client.get(self.settings_url).context["chat_routing"]
        return {"action": action, "confirmation": ctx[action]}

    def test_separate_destination_review_and_stale_forms(self):
        self.assertEqual(scope(self.case), {})
        settings = self.client.get(self.settings_url)
        for disclosure in (
            "OpenAI Decisions",
            "gpt-6-luna",
            "up to eight source descriptions",
            "a short excerpt",
            "local AI",
            "retention",
        ):
            self.assertContains(settings, disclosure)
        self.assertNotContains(settings, "synthetic-key")
        initial = self.form("approve")
        self.assertRedirects(
            self.client.post(self.change_url, initial),
            self.settings_url + "#chat-routing",
        )
        original = scope(self.case)
        self.assertEqual(original["model"], "gpt-6-luna")
        saved = ChatRoutingApproval.objects.get(case=self.case)
        self.assertEqual(saved.changed_by, self.users["owner"])
        self.assertNotIn("synthetic-key", str(saved.destination))
        self.client.post(self.change_url, initial)
        self.assertEqual(scope(self.case), original)
        self.assertEqual(self.case.ai_backend, "local")
        self.assertFalse(self.case.external_analysis_approval)
        self.assertFalse(self.case.search_rerank_approval)
        revoke = self.form("revoke")
        self.client.post(self.change_url, revoke)
        self.client.post(self.change_url, initial)
        self.assertEqual(scope(self.case), {})
        self.client.post(self.change_url, self.form("approve"))
        self.client.post(self.change_url, revoke)
        self.assertNotEqual(scope(self.case)["revision"], original["revision"])

    def test_destination_drift_and_revocation_while_unconfigured(self):
        initial = self.form("approve")
        with patch.object(
            decisions,
            "destination",
            return_value=decisions.destination() | {"payload_version": 2},
        ):
            self.client.post(self.change_url, initial)
            self.assertEqual(scope(self.case), {})
        self.client.post(self.change_url, initial)
        with self.settings(PANDECT_DIRECT_AI={}):
            self.assertEqual(scope(self.case), {})
            response = self.client.get(self.settings_url)
            self.assertContains(response, "is inactive")
            self.assertNotContains(response, "Approve OpenAI source selection")
            self.client.post(self.change_url, self.form("revoke"))
        self.assertEqual(scope(self.case), {})

    def test_only_verified_owner_can_approve_this_case(self):
        approve = self.form("approve")
        self.assertEqual(self.client.get(self.change_url).status_code, 405)
        for role, status in (("editor", 403), ("viewer", 403), ("outsider", 404)):
            self.login(role)
            self.assertEqual(
                self.client.post(self.change_url, approve).status_code, status
            )
        self.client.post(
            reverse("cases:case_chat_routing", args=[self.other_case.pk]), approve
        )
        self.login("owner")
        EmailAddress.objects.filter(user=self.users["owner"]).update(verified=False)
        self.assertRedirects(
            self.client.post(self.change_url, approve),
            reverse("account_email"),
            fetch_redirect_response=False,
        )
        self.assertFalse(ChatRoutingApproval.objects.exists())

    def test_change_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.users["owner"])
        self.assertEqual(
            client.post(self.change_url, self.form("approve")).status_code, 403
        )
        self.assertFalse(ChatRoutingApproval.objects.exists())
