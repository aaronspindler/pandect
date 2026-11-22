"""CSV-only message intake preserves retained evidence and import history."""

from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from core.analysis.operations import get_operation
from core.analysis.services import process_run, snapshot_sources, start_run
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.models import CommunicationContext, TextMessageEvidence


class CSVOnlyMessageImportTests(EvidenceViewTestMixin, TestCase):
    html = (
        b'<div class="message"><span class="timestamp"></span>'
        b'<span class="sender">Me</span><span class="bubble">Hello</span></div>'
    )

    def retained_html(self):
        return DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Retained HTML export",
            file=SimpleUploadedFile("messages.html", self.html, "text/html"),
        )

    def test_removed_operation_cannot_be_selected_or_queued(self):
        with self.assertRaises(ValidationError):
            get_operation("conversation_import")
        document = self.retained_html()
        for actor in ("owner", "editor"):
            with self.subTest(actor=actor):
                self.login(actor)
                url = reverse("core:analysis:create", args=[self.case.pk])
                for response in (
                    self.client.get(url),
                    self.client.get(url, {"operation": "conversation_import"}),
                ):
                    self.assertEqual(response.status_code, 200)
                    self.assertNotContains(response, "conversation_import")
                    self.assertNotContains(response, "Export account mapping")
                rejected = self.client.post(
                    url,
                    {
                        "operation": "conversation_import",
                        "submission_id": uuid4(),
                        "sources": [f"document:{document.pk}"],
                    },
                )
                self.assertEqual(rejected.status_code, 200)
                self.assertIn("operation", rejected.context["form"].errors)
                with self.assertRaises(ValidationError):
                    start_run(
                        self.users[actor],
                        self.case.pk,
                        "conversation_import",
                        [{"kind": "document", "id": str(document.pk)}],
                    )
        self.assertFalse(AnalysisRun.objects.exists())
        self.assertFalse(TextMessageEvidence.objects.exists())

    def test_csv_intake_rejects_uploaded_and_retained_html_without_publication(self):
        document = self.retained_html()
        people_before = Person.objects.count()
        self.login("owner")
        url = reverse("phone:imessage_upload", args=[self.case.pk])
        for source in ("messages.html", "messages.csv", "retained"):
            with self.subTest(source=source):
                payload = {
                    "submission_id": str(uuid4()),
                    "timezone": "UTC",
                    "confirm_timezone": "on",
                }
                if source == "retained":
                    payload["document"] = str(document.pk)
                else:
                    payload["file"] = SimpleUploadedFile(source, self.html)
                response = self.client.post(url, payload)
                self.assertEqual(response.status_code, 302)
                run = AnalysisRun.objects.latest("created_at")
                self.assertEqual(process_run(run.pk), "imessage_invalid_format")
                run.refresh_from_db()
                self.assertEqual(run.status, "failed")
                self.assertContains(
                    self.client.get(response.url),
                    "18-column iMessage or 15-column WhatsApp",
                )
                self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
        self.assertEqual(Person.objects.count(), people_before)
        self.assertFalse(TextMessageEvidence.objects.exists())
        self.assertFalse(CommunicationContext.objects.exists())
        with document.file.open("rb") as retained:
            self.assertEqual(retained.read(), self.html)

    def test_old_queued_import_fails_and_retained_result_has_no_retry(self):
        document = self.retained_html()
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="conversation_import",
            method_version="3",
            inputs=snapshot_sources(
                self.case, [{"kind": "document", "id": str(document.pk)}]
            ),
        )
        self.assertEqual(process_run(run.pk), "processing_version_unavailable")
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertFalse(TextMessageEvidence.objects.exists())
        # Model a result published before HTML import was removed.
        run.status = "complete"
        run.save(update_fields=["status"])
        AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data={"summary": "Retained import summary", "items": []},
        )
        url = reverse("core:analysis:detail", args=[self.case.pk, run.pk])
        for actor in ("owner", "editor", "viewer"):
            with self.subTest(actor=actor):
                self.login(actor)
                response = self.client.get(url)
                self.assertContains(response, "Retained import summary")
                self.assertNotContains(response, 'id="run-again"')
        self.login("owner")
        response = self.client.post(
            reverse("core:analysis:retry", args=[self.case.pk, run.pk]),
            {"submission_id": str(uuid4())},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AnalysisRun.objects.count(), 1)
        self.login("outsider")
        self.assertEqual(self.client.get(url).status_code, 404)
