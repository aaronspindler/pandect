"""Current, source-linked case findings without a review or analysis setup step."""

import json
from datetime import timedelta
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.catalogue import project_result
from core.analysis.services import start_run
from core.analysis.source_selection import snapshot_sources
from core.analysis.worker import process_run
from core.models import AnalysisResult, AnalysisReview, AnalysisRun, CaseAnalysisBatch
from core.tests.testing import EvidenceViewTestMixin
from geo.models import VehicleLocationImport
from geo.tests.testing import csv_content, parse_csv
from geo.vehicle_import_jobs import process_import
from people.models import Person
from phone.models import CallEvidenceAttachment, PhoneCallEvidence, TextMessageEvidence
from text.models import TextEvidence


class CaseFindingsTests(EvidenceViewTestMixin, TestCase):
    def test_saved_ai_findings_display_cited_names_and_preserve_results(self):
        self.person.first_name = "Alex <script>"
        self.person.save()
        recipient = Person.objects.create(
            case=self.case, email="recipient@example.test"
        )
        hidden = Person.objects.create(
            case=self.other_case, first_name="Private person"
        )
        uncited = Person.objects.create(case=self.case, first_name="Uncited person")
        message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Reported correspondence",
            sender=self.person,
            recipient=recipient,
            body=f"Reported sender {self.person.pk} appears in the original.",
        )
        inputs = snapshot_sources(
            self.case, [{"kind": "text-message", "id": message.pk}]
        )
        narrative = (
            f"The reported sender **{str(self.person.pk).upper()}** contacted "
            f"reported recipient {recipient.pk}. "
            f"Reported sender {hidden.pk} and "
            f"reported sender {uncited.pk} are unresolved."
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="content_events",
            method_version="test",
            inputs=inputs,
            status="complete",
        )
        item = {
            "label": f"Reported sender **{self.person.pk}**",
            "text": narrative,
            "source_ids": [inputs[0]["id"]],
            "anchors": [
                {"source_id": inputs[0]["id"], "start": 0, "end": len(message.body)}
            ],
            "values": {"status": "unresolved"},
        }
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data={"items": [item]}
        )
        project_result(result)
        self.login("viewer")
        catalogue = self.client.get(
            reverse("core:analysis:browse", args=[self.case.pk])
        )
        preview = catalogue.context["rows"][0]["preview"]
        self.assertEqual(catalogue.context["rows"][0]["label"], "Alex <script>")
        self.assertTrue(
            preview.startswith(
                "Alex <script> contacted reported recipient recipient@example.test"
            )
        )
        self.assertNotIn(str(self.person.pk).upper(), preview)
        self.assertIn(str(hidden.pk), preview)
        self.assertIn(str(uncited.pk), preview)
        self.assertNotContains(catalogue, "Private person")
        self.assertNotIn("Uncited person", preview)
        self.assertContains(catalogue, "Alex &lt;script&gt;")
        self.assertNotContains(catalogue, "<script>")
        _, highlights = self.findings()
        evidence = self.client.get(self.url("detail", message, kind="text-message"))
        for rows in (
            highlights,
            evidence.context["findings_page"],
        ):
            self.assertTrue(
                rows[0]["summary_text"].startswith("Alex <script> contacted")
            )
            self.assertTrue(rows[0]["ai_suggestion"])
        detail = self.client.get(
            reverse("core:analysis:detail", args=[self.case.pk, run.pk])
        )
        self.assertEqual(detail.context["items"][0]["label"], "Alex <script>")
        self.assertTrue(
            detail.context["items"][0]["text"].startswith("Alex <script> contacted")
        )
        self.assertContains(detail, message.body)
        result.refresh_from_db()
        self.assertEqual(result.data["items"][0], item)
        download = self.client.get(
            reverse("core:analysis:download", args=[self.case.pk, run.pk])
        )
        retained = json.loads(download.content)
        self.assertEqual(retained["result"]["items"][0]["text"], narrative)

    def test_model_suggestions_remain_labeled_in_reading(self):
        from core.analysis.navigation import finding_url

        source = self.source()
        inputs = snapshot_sources(self.case, [{"kind": "text", "id": source.pk}])
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="content_events",
            method_version="test",
            inputs=inputs,
            status="complete",
        )
        result = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data={
                "summary": "Source-cited suggestions",
                "items": [
                    {
                        "label": "Reported commitment",
                        "text": "A delivery may be planned.",
                        "source_ids": [inputs[0]["id"]],
                        "values": {"status": "unresolved"},
                    }
                ],
            },
        )
        project_result(result)
        self.login("viewer")
        for url in (
            self.index_url(),
            self.url("detail", source),
            finding_url(self.case.pk, run.pk, 0),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, "AI suggestion")
                self.assertNotContains(response, ">Record finding review</button>")
        catalogue = self.client.get(
            reverse("core:analysis:browse", args=[self.case.pk])
        )
        self.assertContains(catalogue, "AI-assisted run")

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
        return response, [
            row
            for row in response.context["latest_records"]
            if row["record_type"] == "Finding"
        ]

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
        self.assertContains(pending, "Case Tools")
        self.assertNotContains(pending, "Run local checks")
        self.assertContains(
            pending, reverse("core:analysis:processing", args=[self.case.pk])
        )
        batch = CaseAnalysisBatch.objects.get(case=self.case)
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        for run in batch.runs.all():
            process_run(run.pk)
            run.refresh_from_db()
            self.assertIn(
                run.status, {"complete", "empty", "insufficient"}, run.error_code
            )
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

    def test_latest_per_source_does_not_resurface_superseded_results(self):
        source = self.source()
        first = self.entities(source)
        newest = self.entities(source)
        # Even if the older run finished later, the newer run supersedes it.
        AnalysisResult.objects.filter(run=first).update(
            created_at=newest.result.created_at + timedelta(seconds=1)
        )
        self.login("owner")
        _, highlights = self.findings()
        self.assertEqual(len(highlights), 1)
        self.assertIn(str(newest.pk), highlights[0]["url"])
        self.assertNotIn(str(first.pk), highlights[0]["url"])

    def test_latest_records_reserve_each_kind_and_order_by_added_or_saved_time(self):
        sources = [
            self.source(f"source-{n}@example.test", title=f"Record {n}")
            for n in range(4)
        ]
        runs = [self.entities(source) for source in sources]
        # The earliest requested run completes after a later evidence addition.
        newest_evidence = self.source(title="Latest addition")
        saved = newest_evidence.created_at + timedelta(seconds=1)
        AnalysisResult.objects.filter(run=runs[0]).update(created_at=saved)
        self.login("viewer")
        response, highlights = self.findings()
        records = response.context["latest_records"]
        self.assertEqual(len(records), 6)
        self.assertEqual(
            [row["title"] for row in records],
            [
                "source-0@example.test",
                "Latest addition",
                "source-3@example.test",
                "source-2@example.test",
                "Record 3",
                "Record 2",
            ],
        )
        self.assertEqual(records[0]["created_at"], saved)
        self.assertEqual(records[1]["url"], self.url("detail", newest_evidence))
        self.assertIn(str(runs[0].pk), highlights[0]["url"])

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
        reader = self.client.get(self.url("detail", call, "phone-call"))
        for rows in (highlights, reader.context["findings_page"]):
            self.assertEqual(
                {row["source_url"] for row in rows},
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
        self.assertContains(self.findings()[0], "Call patterns")
        second = PhoneCallEvidence.objects.create(title="Second call", **fields)
        response, _ = self.findings()
        self.assertContains(response, "Call patterns")
        report_url = reverse("core:analysis:report", args=[self.case.pk, "calls"])
        self.assertContains(self.client.get(report_url), "2 selected call records")
        self.assertFalse(AnalysisRun.objects.exists())
        PhoneCallEvidence.objects.filter(pk=second.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.assertContains(self.client.get(report_url), "1 selected call record")

    def test_completed_csv_trip_is_available_from_case_tools_without_a_processing_run(
        self,
    ):
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
        submission_id = uuid4()
        upload = self.client.post(
            reverse("geo:vehicle_import_upload", kwargs={"case_id": self.case.pk}),
            {
                "submission_id": str(submission_id),
                "file": SimpleUploadedFile("trips.csv", content),
                "vehicle": str(self.vehicle.pk),
                "timezone": "UTC",
                "use_created_time": "on",
            },
        )
        self.assertEqual(upload.status_code, 302)
        batch = VehicleLocationImport.objects.get(
            case=self.case,
            uploaded_by=self.users["editor"],
            submission_id=submission_id,
        )
        self.assertEqual(process_import(batch.pk, batch.job_token), "review")
        confirmation = self.client.post(
            upload.url, {"vehicle_0": str(self.vehicle.pk), "confirm": "on"}
        )
        self.assertEqual(confirmation.status_code, 302)
        batch.refresh_from_db()
        self.assertEqual(process_import(batch.pk, batch.job_token), "complete")
        from geo.trip_jobs import process_trips
        from geo.trip_processing import prepare_case_trips

        for generation in prepare_case_trips(self.users["editor"], self.case.pk):
            while generation.status == "queued":
                process_trips(str(generation.pk), str(generation.token))
                generation.refresh_from_db()
            self.assertEqual(generation.status, "complete", generation.error_code)
        self.assertFalse(AnalysisRun.objects.exists())
        self.login("viewer")
        response, highlights = self.findings()
        self.assertEqual(highlights, [])
        trips_url = reverse("geo:case_map", args=[self.case.pk])
        self.assertContains(response, trips_url)
        response = self.client.get(trips_url)
        self.assertContains(response, "<h1>Trips</h1>", html=True)
        self.assertContains(response, "Possible trip")
        for observation in batch.observations.all():
            self.assertContains(
                response, self.url("detail", observation, "vehicle-location")
            )

    def test_case_tools_previews_and_controls_respect_case_and_role(
        self,
    ):
        active = [self.source(title=f"Active source {index}") for index in range(28)]
        archived = self.source(title="Archived source")
        TextEvidence.objects.filter(pk=archived.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.source(title="Foreign source", case=self.other_case)
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            response, _ = self.findings()
            self.assertEqual(len(response.context["latest_records"]), 3)
            self.assertEqual(response.context["latest_records"][0]["id"], active[-1].pk)
            self.assertNotContains(response, "Archived source")
            self.assertNotContains(response, "Foreign source")
            self.assertNotContains(response, 'id="processing-history"')
            self.assertNotContains(response, "Run local checks")
            self.assertNotContains(response, "tool-selections")
            self.assertContains(response, 'aria-label="Case Tools"')
            self.assertContains(response, 'aria-label="Processing"')
            settings = reverse("cases:case_settings", args=[self.case.pk])
            if role == "owner":
                self.assertContains(response, settings)
            else:
                self.assertNotContains(response, settings)
            analysis = reverse("core:analysis:create", args=[self.case.pk])
            if role == "viewer":
                self.assertEqual(response.context["operation_groups"], [])
                self.assertNotContains(response, analysis)
            else:
                self.assertContains(response, analysis)

    def test_empty_case_home_offers_intake_only_to_contributors(self):
        for role in ("owner", "editor", "viewer"):
            with self.subTest(role=role):
                self.login(role)
                home = self.client.get(self.index_url())
                self.assertEqual(home.context["latest_records"], [])
                if role == "viewer":
                    self.assertNotContains(home, self.url("create"))
                    self.assertContains(home, "when an owner or editor adds it")
                else:
                    self.assertContains(home, self.url("create"))
                    self.assertContains(home, "No recent evidence or findings.")

    def test_homepage_method_links_match_chooser_and_open_without_starting_work(self):
        self.login("editor")
        chooser_url = reverse("core:analysis:create", args=[self.case.pk])
        chooser = self.client.get(chooser_url)
        home = self.client.get(self.index_url())
        home_groups = {
            group: [choice["url"] for choice in choices]
            for group, choices in home.context["operation_groups"]
        }
        self.assertEqual(
            home_groups,
            {
                group: [chooser_url + choice["url"] for choice in choices]
                for group, choices in chooser.context["operation_groups"]
            },
        )
        for choices in home_groups.values():
            for url in choices:
                with self.subTest(url=url):
                    self.assertContains(home, f'href="{url}"')
                    self.assertEqual(self.client.get(url).status_code, 200)
        self.assertFalse(AnalysisRun.objects.exists())
