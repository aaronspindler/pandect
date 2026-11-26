from datetime import UTC, datetime

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.catalogue import project_result
from core.analysis.navigation import finding_url
from core.analysis.services import start_run
from core.analysis.source_selection import snapshot_sources
from core.analysis.worker import process_run
from core.evidence.time_claims import time_claims_page
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from phone.models import CallEvidenceAttachment, PhoneCallEvidence, TextMessageEvidence


class UnplacedTimeClaimTests(EvidenceViewTestMixin, TestCase):
    def message(self, body):
        return TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            sender=self.person,
            recipient=self.person,
            body=body,
            title="Reported pickup conversation",
            occurred_at=datetime(2026, 6, 10, 12, tzinfo=UTC),
        )

    def process(self, run):
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)

    def publish(self, inputs, items, *, status="complete"):
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="content_events",
            method_version="1",
            inputs=inputs,
            status=status,
        )
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data={"items": items}
        )
        project_result(result)
        return result

    def test_relative_dates_and_competing_plans_keep_their_exact_sources(self):
        first = "I will pick it up Tomorrow."
        second = "Today's report says the pickup was cancelled yesterday."
        message = self.message(first + " " + second)
        choice = [{"kind": "text-message", "id": message.pk}]
        inputs = snapshot_sources(self.case, choice)
        key = inputs[0]["id"]
        run, _ = start_run(self.users["owner"], self.case.pk, "entities", choice)
        self.process(run)
        self.assertEqual(run.method_version, "2")

        def claim(label, status, quote):
            start = message.body.index(quote)
            return {
                "label": label,
                "values": {"status": status},
                "text": "Interpretation of the pickup.",
                "anchors": [
                    {"source_id": key, "start": start, "end": start + len(quote)}
                ],
                "source_ids": [key],
            }

        items = [
            claim("plan", "proposed", first),
            claim("cancellation", "reported cancellation", second),
            claim("correction", "reported", second) | {"report_only": True},
        ]
        result = self.publish(inputs, items, status="partial")
        self.message("Unrelated text.")
        latest = time_claims_page(self.case, {"person": self.person}, 1)
        self.assertEqual(latest.paginator.count, 5)
        rows = latest.object_list
        self.assertCountEqual(
            [row["citations"][0]["quote"] for row in rows],
            [first, second, "Tomorrow", "Today", "yesterday"],
        )
        self.assertEqual(
            {r["label"] for r in rows},
            {"Relative date", "Planned event", "Cancellation"},
        )
        self.assertTrue(all(r["url"] for r in rows))
        self.assertTrue(
            all(r["method_version"] == "2" for r in rows if not r["uses_ai"])
        )
        self.assertEqual(
            [(r["status"], r["url"]) for r in rows if r["uses_ai"] and r["partial"]],
            [
                (item["values"]["status"], finding_url(self.case.pk, result.run_id, i))
                for i, item in enumerate(items[:2])
            ],
        )
        dates = time_claims_page(self.case, {}, 1, dates_only=True)
        self.assertEqual(dates.paginator.count, 3)
        self.assertFalse(
            time_claims_page(self.case, {"type": "document"}, 1).object_list
        )
        self.login("viewer")
        for row in rows:
            detail = self.client.get(row["url"])
            self.assertEqual(
                detail.context["items"][0]["sources"][0]["excerpt"],
                row["citations"][0]["quote"],
            )
        self.publish(inputs, [], status="failed")
        self.assertEqual(time_claims_page(self.case, {}, 1).paginator.count, 5)
        TextMessageEvidence.objects.filter(pk=message.pk).update(
            is_archived=True,
            archive_changed_at=message.occurred_at,
            archive_changed_by=self.users["owner"],
        )
        self.assertEqual(time_claims_page(self.case, {}, 1).paginator.count, 0)
        self.assertEqual(
            time_claims_page(self.case, {}, 1, dates_only=True).paginator.count, 0
        )
        prior_url = finding_url(self.case.pk, result.run_id, 0)
        archived = self.client.get(prior_url)
        self.assertContains(archived, "Restore the evidence")
        self.assertNotContains(archived, first)
        self.login("editor")
        restore = reverse(
            "core:evidence:restore", args=(self.case.pk, "text-message", message.pk)
        )
        self.assertEqual(self.client.post(restore).status_code, 302)
        self.assertEqual(time_claims_page(self.case, {}, 1).paginator.count, 5)
        self.publish(inputs, [], status="empty")
        self.assertEqual(time_claims_page(self.case, {}, 1).paginator.count, 3)
        self.assertContains(self.client.get(prior_url), first)

    def test_malformed_or_foreign_claims_withhold_quotations_and_interpretations(self):
        message = self.message("Tomorrow we will close the synthetic office.")
        inputs = snapshot_sources(
            self.case, [{"kind": "text-message", "id": message.pk}]
        )
        key = inputs[0]["id"]
        anchor = {"source_id": key, "start": 0, "end": 8}
        item = {
            "label": "plan",
            "text": "Private claim text",
            "values": {"status": "planned"},
            "source_ids": [key],
            "anchors": [anchor],
        }
        result = self.publish(inputs, [item])
        self.login("viewer")
        url = finding_url(self.case.pk, result.run_id, 0)
        self.assertContains(self.client.get(url), item["text"])
        report = reverse("core:analysis:detail", args=[self.case.pk, result.run_id])
        download = reverse("core:analysis:download", args=[self.case.pk, result.run_id])
        for malformed_anchor in (
            anchor | {"end": 999},
            anchor | {"start": False},
            anchor | {"source_id": str(self.other_case.pk)},
            "invalid",
        ):
            with self.subTest(anchor=malformed_anchor):
                AnalysisResult.objects.filter(pk=result.pk).update(
                    data={"items": [item | {"anchors": [malformed_anchor]}]}
                )
                self.assertEqual(
                    time_claims_page(self.case, {}, 1).object_list,
                    [{"unavailable": True}],
                )
                for link in (url, report):
                    detail = self.client.get(link)
                    self.assertContains(detail, "Source unavailable")
                    self.assertNotContains(detail, item["text"])
                    self.assertNotContains(detail, "Tomorrow")
                self.assertEqual(self.client.get(download).status_code, 404)
        AnalysisResult.objects.filter(pk=result.pk).update(data={"items": [item]})
        finding = result.findings.get()
        finding.sources.update(source_sha256="f" * 64)
        self.assertEqual(
            time_claims_page(self.case, {}, 1).object_list, [{"unavailable": True}]
        )
        finding.sources.update(source_sha256=message.sha256)
        # Same-length changes still fit the original offsets; verify captured bytes.
        tampered = message.body.replace("Tomorrow", "Saturday")
        TextMessageEvidence.objects.filter(pk=message.pk).update(body=tampered)
        self.assertEqual(
            time_claims_page(self.case, {}, 1).object_list, [{"unavailable": True}]
        )
        detail = self.client.get(url)
        self.assertNotContains(detail, item["text"])
        self.assertNotContains(detail, "Saturday")
        self.assertEqual(self.client.get(download).status_code, 404)
        TextMessageEvidence.objects.filter(pk=message.pk).update(
            body=message.body, sha256="a" * 64
        )
        self.assertEqual(
            time_claims_page(self.case, {}, 1).object_list, [{"unavailable": True}]
        )
        self.assertNotContains(self.client.get(url), item["text"])
        TextMessageEvidence.objects.filter(pk=message.pk).update(
            sha256=message.sha256, case=self.other_case
        )
        self.assertEqual(
            time_claims_page(self.case, {}, 1).object_list, [{"unavailable": True}]
        )
        self.assertNotContains(self.client.get(url), item["text"])
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(
            self.client.get(
                reverse("core:evidence:chronology", args=[self.case.pk]),
                {"view": "unplaced"},
            ).status_code,
            404,
        )

    def test_changed_supplied_transcript_cannot_supply_new_words_at_old_offsets(self):
        call = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Dispatch call",
            sender=self.person,
            recipient=self.person,
        )
        transcript = CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            phone_call=call,
            transcript="Tomorrow is possible.",
        )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "entities",
            [
                {
                    "kind": "phone-call",
                    "id": str(call.pk),
                    "attachment_id": str(transcript.pk),
                }
            ],
        )
        self.process(run)
        (row,) = time_claims_page(self.case, {}, 1)
        self.assertEqual(row["citations"][0]["quote"], "Tomorrow")
        CallEvidenceAttachment.objects.filter(pk=transcript.pk).update(
            transcript="Saturday is possible."
        )
        self.assertEqual(
            time_claims_page(self.case, {}, 1).object_list, [{"unavailable": True}]
        )
        self.login("viewer")
        self.assertContains(self.client.get(row["url"]), "Result withheld")

    def test_derived_date_links_to_the_pinned_passage_and_version(self):
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Dispatch",
            file=SimpleUploadedFile(
                "dispatch.txt", b"On 2026-06-22 the delivery was proposed."
            ),
        )
        choice = {"kind": "document", "id": document.pk}
        actor = self.users["owner"]
        extract, _ = start_run(actor, self.case.pk, "native_text", [choice])
        self.process(extract)
        run, _ = start_run(
            actor,
            self.case.pk,
            "entities",
            [choice | {"result_id": str(extract.result.pk)}],
        )
        self.process(run)
        (row,) = time_claims_page(self.case, {}, 1)
        self.assertEqual(row["citations"][0]["quote"], "2026-06-22")
        self.login("owner")
        response = self.client.get(row["url"])
        self.assertEqual(
            response.context["items"][0]["sources"][0]["url"],
            finding_url(self.case.pk, extract.pk, 0),
        )
        item = run.result.data["items"][0]
        item["anchors"][0]["extraction_ranges"] = []
        AnalysisResult.objects.filter(pk=run.result.pk).update(data={"items": [item]})
        self.assertEqual(
            time_claims_page(self.case, {}, 1).object_list, [{"unavailable": True}]
        )
        broken = self.client.get(row["url"])
        self.assertContains(broken, "Source unavailable")
        self.assertNotContains(broken, "2026-06-22")
