"""Timing-only visibility preserves retained findings, indices, and case access."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from django.test import TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.catalogue import project_result
from core.analysis.services import start_run
from core.analysis.worker import process_run
from core.models import AnalysisResult, AnalysisReview, AnalysisRun
from core.testing import EvidenceViewTestMixin
from people.models import Person
from phone.models import TextMessageEvidence


class TimingResultTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        recipient = Person.objects.create(case=self.case, first_name="Morgan")
        self.messages = [
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=f"Message {index + 1}",
                sender=self.person if index == 0 else recipient,
                recipient=recipient if index == 0 else self.person,
                occurred_at=datetime(2026, 7, 30, 1, 23, 10, tzinfo=UTC)
                + timedelta(seconds=16 * index),
                body=body,
            )
            for index, body in enumerate(("Tomorrow will be better.", "Thank you!"))
        ]
        self.run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "conversation_link",
            [{"kind": "text-message", "id": str(item.pk)} for item in self.messages],
        )
        process_run(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "complete", self.run.error_code)
        self.result = self.run.result
        self.detail_url = reverse(
            "core:analysis:detail", args=[self.case.pk, self.run.pk]
        )

    def test_hidden_only_results_keep_limitations_reveal_and_original_download(self):
        self.assertFalse(self.result.findings.exists())
        self.login("viewer")
        response = self.client.get(self.detail_url)
        self.assertEqual(response.context["items"], [])
        self.assertEqual(response.context["timing_only_count"], 1)
        self.assertContains(response, "1 timing-only finding hidden")
        self.assertContains(response, "Show all findings")
        self.assertContains(response, "Limitations")
        self.assertNotContains(response, self.messages[0].body)
        shown = self.client.get(self.detail_url, {"weak": "1"})
        self.assertEqual([item["index"] for item in shown.context["items"]], [0])
        self.assertTrue(shown.context["show_timing_only"])
        self.assertNotContains(shown, "Record finding review</button>")
        download = self.client.get(
            reverse("core:analysis:download", args=[self.case.pk, self.run.pk])
        )
        self.assertEqual(json.loads(download.content)["result"], self.result.data)

    def test_legacy_acceptance_does_not_promote_timing_only_findings(self):
        self.login("owner")
        evidence_url = self.url("detail", self.messages[0], kind="text-message")
        index_url = reverse("core:analysis:index", args=[self.case.pk])
        for index in (None, 0):
            AnalysisReview.objects.create(
                case=self.case,
                result=self.result,
                reviewed_by=self.users["owner"],
                decision="accepted",
                item_index=index,
            )
        for url in (self.detail_url, index_url, evidence_url):
            response = self.client.get(url)
            self.assertFalse(
                response.context.get("items", response.context.get("highlights"))
            )

    def test_direct_link_reveals_exact_hidden_item(self):
        self.login("editor")
        response = self.client.get(self.detail_url, {"finding": "1"})
        self.assertEqual(response.context["items"][0]["index"], 0)
        self.assertTrue(response.context["show_timing_only"])

    def test_filter_before_pagination_preserves_global_indices(self):
        items = [deepcopy(self.result.data["items"][0]) for _ in range(55)]
        for item in items[53:]:
            item["values"]["shared_words"] = ["harbour"]
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation=self.run.operation,
            method_version=self.run.method_version,
            inputs=self.run.inputs,
            status="partial",
        )
        result = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data={
                "items": items,
                "warnings": ["Only selected exchanges were checked."],
            },
        )
        project_result(result)
        url = reverse("core:analysis:detail", args=[self.case.pk, run.pk])
        self.login("viewer")
        response = self.client.get(url)
        self.assertEqual(response.context["finding_page"].paginator.count, 2)
        self.assertEqual(
            [item["index"] for item in response.context["items"]], [53, 54]
        )
        self.assertContains(response, "Only selected exchanges were checked.")
        exact = self.client.get(url, {"finding": "51"})
        self.assertEqual([item["index"] for item in exact.context["items"]], [50])
        self.assertEqual(exact.context["finding_page"].number, 2)
        self.assertEqual(exact.context["finding_page"].paginator.count, 55)

    def test_reveal_does_not_bypass_case_or_current_source_access(self):
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            self.assertEqual(
                self.client.get(self.detail_url, {"weak": "1"}).status_code, 200
            )
        self.login("outsider")
        self.assertEqual(
            self.client.get(self.detail_url, {"weak": "1"}).status_code, 404
        )
        self.login("viewer")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(
            self.client.get(self.detail_url, {"weak": "1"}).status_code, 404
        )
        self.login("owner")
        TextMessageEvidence.objects.filter(pk=self.messages[0].pk).update(
            sha256="0" * 64
        )
        unavailable = self.client.get(self.detail_url, {"weak": "1"})
        self.assertContains(unavailable, "Result withheld")
        self.assertNotContains(unavailable, self.messages[0].body)
