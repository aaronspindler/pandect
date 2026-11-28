"""Hosted collection discovery preserves exact call anchors and escaped excerpts."""

from django.test import TestCase

from core.case_search.tests.fixtures import indexed
from core.tests.testing import EvidenceViewTestMixin
from phone.models import CallEvidenceAttachment, PhoneCallEvidence


class RetainedContentSearchTests(EvidenceViewTestMixin, TestCase):
    def test_call_and_text_matches_are_current_inspectable_passages_for_all_roles(self):
        original = self.text()
        call = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Dock call",
            sender=self.person,
            recipient=self.person,
        )
        addition = CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            phone_call=call,
            transcript="Recorded café <script>unsafe</script>",
        )
        sources = [
            ("original", "text", original.pk),
            ("attachment", "phone-call", addition.pk),
        ]
        with indexed(self.case, sources):
            for role in ("owner", "editor", "viewer"):
                self.login(role)
                response = self.client.get(self.url("list"), {"q": "café"})
                rows = {r["id"]: r for r in response.context["rows"]}
                self.assertEqual(set(rows), {call.pk, original.pk})
                self.assertTrue(
                    rows[call.pk]["content_match"]["url"].endswith(
                        f"#attachment-{addition.pk}"
                    )
                )
                self.assertContains(response, "&lt;script&gt;unsafe&lt;/script&gt;")
                self.assertIn("no-store", response["Cache-Control"])

    def test_disabled_case_has_no_sql_fallback_even_for_an_exact_original(self):
        self.text()
        self.login("viewer")
        response = self.client.get(self.url("list"), {"q": "Original café"})
        self.assertEqual(response.context["rows"], [])
        self.assertIn("enable external AI", response.context["search_notice"])
