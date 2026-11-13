"""Current, source-linked case findings without a review or analysis setup step."""

from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.analysis.services import process_run, review_result, start_run
from core.models import AnalysisResult, AnalysisReview, AnalysisRun
from core.testing import EvidenceViewTestMixin
from geo.models import VehicleLocationImport
from geo.testing import csv_content, parse_csv
from geo.vehicle_import_jobs import process_import
from phone.models import CallEvidenceAttachment, PhoneCallEvidence
from text.models import TextEvidence


class CaseFindingsTests(EvidenceViewTestMixin, TestCase):
    def index_url(self, case=None):
        return reverse(
            "core:analysis:index", kwargs={"case_id": (case or self.case).pk}
        )

    def source(self, email="contact@example.test", *, case=None, title="Source"):
        case = case or self.case
        return TextEvidence.objects.create(
            case=case,
            uploaded_by=case.owner,
            title=title,
            body=f"Contact {email} about the delivery.",
        )

    def entities(self, source, *, kind="text", attachment=None):
        choice = {"kind": kind, "id": str(source.pk)}
        if attachment is not None:
            choice["attachment_id"] = str(attachment.pk)
        run, _ = start_run(source.case.owner, source.case_id, "entities", [choice])
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        return run

    def findings(self):
        response = self.client.get(self.index_url())
        self.assertEqual(response.status_code, 200)
        return response, response.context["highlights"]

    def test_adding_text_produces_source_linked_case_findings_without_review(self):
        self.login("editor")
        added = self.client.post(
            self.url("create"),
            self.payload(body="Contact delivery@example.test about the delivery."),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(added.status_code, 201)
        source = TextEvidence.objects.get(pk=added.json()["id"])
        pending, highlights = self.findings()
        self.assertEqual(highlights, [])
        self.assertContains(pending, "Checking evidence")
        for run in AnalysisRun.objects.filter(case=self.case):
            process_run(run.pk)
            run.refresh_from_db()
            self.assertEqual(run.status, "complete", run.error_code)
        self.assertFalse(AnalysisReview.objects.exists())

        self.login("viewer")
        response, highlights = self.findings()
        finding = next(
            item
            for item in highlights
            if item["summary_text"] == "delivery@example.test"
        )
        self.assertEqual(finding["source_title"], source.title)
        self.assertEqual(finding["source_url"], self.url("detail", source))
        self.assertIsNone(finding["latest_review"])
        self.assertContains(response, "delivery@example.test")
        self.assertContains(response, finding["url"].replace("&", "&amp;"))
        result = self.client.get(finding["url"])
        self.assertContains(result, self.url("detail", source))
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])

    def test_same_operation_on_different_evidence_keeps_each_source_visible(self):
        first = self.source("first@example.test", title="First source")
        second = self.source("second@example.test", title="Second source")
        self.entities(first)
        self.entities(second)
        self.login("owner")
        _, highlights = self.findings()
        self.assertEqual(
            {item["summary_text"] for item in highlights},
            {"first@example.test", "second@example.test"},
        )
        self.assertEqual(
            {item["source_url"] for item in highlights},
            {self.url("detail", first), self.url("detail", second)},
        )

    def test_latest_per_source_does_not_resurface_rejected_or_superseded_results(self):
        source = self.source()
        first = self.entities(source)
        review_result(self.users["owner"], self.case.pk, first.result.pk, "rejected")
        newest = self.entities(source)
        self.login("owner")
        _, highlights = self.findings()
        self.assertEqual(len(highlights), 1)
        self.assertIn(str(newest.pk), highlights[0]["url"])
        self.assertNotIn(str(first.pk), highlights[0]["url"])
        review_result(self.users["owner"], self.case.pk, newest.result.pk, "rejected")
        self.assertEqual(self.findings()[1], [])

    def test_rejected_individual_findings_are_hidden_without_hiding_other_values(self):
        source = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Two contacts",
            body="Contact first@example.test or second@example.test.",
        )
        run = self.entities(source)
        index = next(
            index
            for index, item in enumerate(run.result.data["items"])
            if item.get("text") == "first@example.test"
        )
        review_result(
            self.users["owner"],
            self.case.pk,
            run.result.pk,
            "rejected",
            item_index=index,
        )
        self.login("viewer")
        _, highlights = self.findings()
        self.assertEqual(
            [item["summary_text"] for item in highlights], ["second@example.test"]
        )

    def test_archived_changed_and_cross_case_sources_are_excluded(self):
        active = self.source("active@example.test")
        archived = self.source("archived@example.test")
        changed = self.source("changed@example.test")
        moved = self.source("moved@example.test")
        foreign = self.source("private@example.test", case=self.other_case)
        for source in (active, archived, changed, moved):
            self.entities(source)
        foreign_run = self.entities(foreign)
        TextEvidence.objects.filter(pk=archived.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        TextEvidence.objects.filter(pk=changed.pk).update(
            body="Changed without checksum"
        )
        TextEvidence.objects.filter(pk=moved.pk).update(case=self.other_case)
        # Even inconsistent historical result links cannot import a foreign source.
        AnalysisRun.objects.filter(pk=foreign_run.pk).update(case=self.case)
        AnalysisResult.objects.filter(run=foreign_run).update(case=self.case)
        self.login("owner")
        response, highlights = self.findings()
        self.assertEqual(
            [item["summary_text"] for item in highlights], ["active@example.test"]
        )
        self.assertNotContains(response, "private@example.test")
        self.assertNotContains(response, "changed@example.test")
        self.assertNotContains(response, "moved@example.test")

    def test_each_retained_call_transcript_keeps_its_findings(self):
        call = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Call with two supplied transcripts",
            sender=self.person,
            recipient=self.person,
        )
        additions = [
            CallEvidenceAttachment.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                phone_call=call,
                transcript=f"Contact {name}@example.test.",
            )
            for name in ("first", "second")
        ]
        for addition in additions:
            self.entities(call, kind="phone-call", attachment=addition)
        self.login("viewer")
        _, highlights = self.findings()
        self.assertEqual(
            {item["source_url"] for item in highlights},
            {
                self.url("detail", call, "phone-call") + f"#attachment-{a.pk}"
                for a in additions
            },
        )

    def test_current_case_access_controls_findings_and_their_links(self):
        self.entities(self.source())
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            _, highlights = self.findings()
            self.assertEqual(len(highlights), 1)
        finding_url = highlights[0]["url"]
        self.case.memberships.filter(user=self.users["viewer"]).delete()
        self.assertEqual(self.client.get(self.index_url()).status_code, 404)
        self.assertEqual(self.client.get(finding_url).status_code, 404)
        self.login("outsider")
        self.assertEqual(self.client.get(self.index_url()).status_code, 404)
        self.assertEqual(self.client.get(finding_url).status_code, 404)

    def test_call_patterns_appear_and_refresh_without_processing_or_review(self):
        self.login("viewer")
        fields = {
            "case": self.case,
            "uploaded_by": self.users["owner"],
            "sender": self.person,
            "recipient": self.person,
        }
        PhoneCallEvidence.objects.create(title="First call", **fields)
        self.assertEqual(self.findings()[0].context["pattern_findings"], [])
        second = PhoneCallEvidence.objects.create(title="Second call", **fields)
        response, _ = self.findings()
        self.assertEqual(response.context["pattern_findings"][0]["name"], "calls")
        self.assertContains(response, "2 selected call records")
        self.assertFalse(AnalysisRun.objects.exists())
        PhoneCallEvidence.objects.filter(pk=second.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.assertEqual(self.findings()[0].context["pattern_findings"], [])

    def test_completed_csv_trip_appears_on_findings_without_a_processing_run(self):
        first = parse_csv(csv_content(), "UTC")[0].raw
        content = csv_content(
            first,
            first
            | {
                "Address": "20 Example Street",
                "Created Time": "2026-09-16, 3:07:22 PM",
            },
        )
        self.login("editor")
        batch_id = uuid4()
        upload = self.client.post(
            reverse("geo:vehicle_import_upload", kwargs={"case_id": self.case.pk}),
            {
                "submission_id": str(batch_id),
                "file": SimpleUploadedFile("trips.csv", content),
                "vehicle": str(self.vehicle.pk),
                "timezone": "UTC",
                "use_created_time": "on",
            },
        )
        self.assertEqual(upload.status_code, 302)
        batch = VehicleLocationImport.objects.get(pk=batch_id)
        self.assertEqual(process_import(batch.pk, batch.job_token), "review")
        confirmation = self.client.post(
            upload.url, {"vehicle_0": str(self.vehicle.pk), "confirm": "on"}
        )
        self.assertEqual(confirmation.status_code, 302)
        batch.refresh_from_db()
        self.assertEqual(process_import(batch.pk, batch.job_token), "complete")
        self.assertFalse(AnalysisRun.objects.exists())
        self.login("viewer")
        response, highlights = self.findings()
        self.assertEqual(highlights, [])
        self.assertEqual(len(response.context["trip_report"]["items"]), 1)
        self.assertContains(response, "Vehicle trips")
        self.assertContains(response, "Possible trip")
        for observation in batch.observations.all():
            self.assertContains(
                response, self.url("detail", observation, "vehicle-location")
            )
