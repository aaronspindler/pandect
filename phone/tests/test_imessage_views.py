"""Reviewed source intake never creates people or messages before publication."""

import csv
import io
from copy import deepcopy
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.services import process_run, resolve_inputs
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.analysis.deterministic.imessage import HEADERS, iter_import_rows
from phone.models import CommunicationContext, TextMessageEvidence


def message_csv(*rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=HEADERS)
    writer.writeheader()
    for index, values in enumerate(
        rows or ({}, {"Type": "Outgoing", "Sender ID": "", "Sender Name": ""})
    ):
        writer.writerow(
            dict.fromkeys(HEADERS, "")
            | {
                "Chat Session": "Synthetic correspondent",
                "Message ID": f"message-{index}",
                "Message Date": "2026-07-29 21:23:10",
                "Service": "iMessage",
                "Type": "Incoming",
                "Sender ID": "17055550100",
                "Sender Name": "Synthetic correspondent",
                "Text": "  Original café\r\nmessage.  ",
            }
            | values
        )
    return stream.getvalue().encode("utf-8")


class IMessageImportViewTests(EvidenceViewTestMixin, TestCase):
    def upload_url(self, case=None):
        return reverse("phone:imessage_upload", args=[(case or self.case).pk])

    def review_url(self, run, case=None):
        return reverse("phone:imessage_review", args=[(case or self.case).pk, run.pk])

    def prepare(self, content=None):
        self.login("owner")
        content = message_csv() if content is None else content
        response = self.client.post(
            self.upload_url(),
            {
                "submission_id": str(uuid4()),
                "timezone": "America/Toronto",
                "confirm_timezone": "on",
                "file": SimpleUploadedFile(
                    "messages.csv", content, content_type="text/csv"
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        run = AnalysisRun.objects.get(
            operation="imessage_import", parameters__phase="preview"
        )
        process_run(run.pk)
        run.refresh_from_db()
        return run

    def new_people(self):
        return {
            "account-mode": "create",
            "account-first_name": "Export account",
            "correspondent-mode": "create",
            "correspondent-first_name": "Correspondent",
            "correspondent-phone_number": "17055550100",
            "direct_chat": "on",
            "confirm": "on",
        }

    def test_upload_retains_exact_csv_and_retries_without_duplicates(self):
        self.login("owner")
        raw = message_csv()
        submission = str(uuid4())

        def payload(content=raw):
            return {
                "submission_id": submission,
                "timezone": "America/Toronto",
                "confirm_timezone": "on",
                "file": SimpleUploadedFile(
                    "messages.csv", content, content_type="text/csv"
                ),
            }

        before = Person.objects.count()
        first = self.client.post(self.upload_url(), payload())
        second = self.client.post(self.upload_url(), payload())
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.url, first.url)
        self.assertEqual(DocumentEvidence.objects.count(), 1)
        self.assertEqual(AnalysisRun.objects.count(), 1)
        document = DocumentEvidence.objects.get()
        with document.file.open("rb") as retained:
            self.assertEqual(retained.read(), raw)
        self.assertEqual(Person.objects.count(), before)
        self.assertFalse(TextMessageEvidence.objects.exists())
        conflict = self.client.post(
            self.upload_url(), payload(message_csv({"Text": "Changed"}))
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(DocumentEvidence.objects.count(), 1)

    def test_review_and_confirm_create_people_only_in_worker_and_bind_choices(self):
        preview = self.prepare()
        self.assertEqual(preview.status, "complete", preview.error_code)
        before = Person.objects.count()
        response = self.client.get(self.review_url(preview))
        self.assertContains(response, "2 ready")
        self.assertContains(response, "17055550100")
        self.assertContains(response, "Message preview")
        self.assertContains(response, "Excerpts from the first 2 source rows")
        self.assertLess(
            response.content.index(b'id="imessage-preview-heading"'),
            response.content.index(b'name="account-mode"'),
        )
        self.assertFalse(TextMessageEvidence.objects.exists())
        first = self.client.post(self.review_url(preview), self.new_people())
        second = self.client.post(self.review_url(preview), self.new_people())
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.url, first.url)
        self.assertEqual(Person.objects.count(), before)
        final = AnalysisRun.objects.get(parameters__phase="import")
        self.assertEqual(final.previous_run_id, preview.pk)
        changed = self.client.post(
            self.review_url(preview),
            self.new_people() | {"account-first_name": "Changed"},
        )
        self.assertEqual(changed.status_code, 409)
        self.assertContains(changed, "already used", status_code=409)
        self.assertEqual(
            AnalysisRun.objects.filter(parameters__phase="import").count(), 1
        )
        process_run(final.pk)
        final.refresh_from_db()
        self.assertEqual(final.status, "complete", final.error_code)
        self.assertEqual(Person.objects.count(), before + 2)
        self.assertEqual(TextMessageEvidence.objects.count(), 2)
        self.assertEqual(CommunicationContext.objects.count(), 2)
        self.assertContains(
            self.client.get(self.review_url(final)), "Messages imported"
        )

    def test_existing_people_and_rejected_cross_case_references(self):
        preview = self.prepare()
        other = Person.objects.create(case=self.case, first_name="Other participant")
        foreign = Person.objects.create(
            case=self.other_case, first_name="Foreign participant"
        )
        data = {
            "account-mode": "existing",
            "account-person": str(self.person.pk),
            "correspondent-mode": "existing",
            "correspondent-person": str(foreign.pk),
            "direct_chat": "on",
            "confirm": "on",
        }
        invalid = self.client.post(self.review_url(preview), data)
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse(
            AnalysisRun.objects.filter(parameters__phase="import").exists()
        )
        before = Person.objects.count()
        self.login("editor")
        good = self.client.post(
            self.review_url(preview), data | {"correspondent-person": str(other.pk)}
        )
        self.assertEqual(good.status_code, 302)
        final = AnalysisRun.objects.get(parameters__phase="import")
        process_run(final.pk)
        self.assertEqual(Person.objects.count(), before)
        pairs = set(
            TextMessageEvidence.objects.values_list("sender_id", "recipient_id")
        )
        self.assertEqual(
            pairs, {(self.person.pk, other.pk), (other.pk, self.person.pk)}
        )

    def test_case_roles_viewer_read_and_revocation(self):
        preview = self.prepare()
        self.login("viewer")
        self.assertEqual(self.client.get(self.upload_url()).status_code, 403)
        page = self.client.get(self.review_url(preview))
        self.assertContains(page, "Viewer access")
        self.assertNotContains(page, 'name="account-mode"')
        self.assertEqual(
            self.client.post(self.review_url(preview), self.new_people()).status_code,
            403,
        )
        self.login("outsider")
        self.assertEqual(self.client.get(self.review_url(preview)).status_code, 404)
        self.login("editor")
        self.assertEqual(self.client.get(self.review_url(preview)).status_code, 200)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(
            self.client.post(self.review_url(preview), self.new_people()).status_code,
            404,
        )
        self.login("owner")
        self.assertEqual(
            self.client.get(self.review_url(preview, case=self.other_case)).status_code,
            404,
        )

    def test_zero_ready_rows_cannot_create_people_or_confirm(self):
        preview = self.prepare(message_csv({"Text": "", "Attachment": "photo.jpg"}))
        before = Person.objects.count()
        page = self.client.get(self.review_url(preview))
        self.assertNotContains(page, 'name="account-mode"')
        self.assertEqual(
            self.client.post(self.review_url(preview), self.new_people()).status_code,
            409,
        )
        self.assertEqual(Person.objects.count(), before)
        self.assertFalse(
            AnalysisRun.objects.filter(parameters__phase="import").exists()
        )

    def test_retained_document_intake_timezone_confirmation_and_safe_context(self):
        preview = self.prepare(
            message_csv({"Reactions": '<script>alert("x")</script>'})
        )
        document = DocumentEvidence.objects.get()
        page = self.client.get(self.upload_url() + f"?document={document.pk}")
        self.assertEqual(page.context["form"]["document"].value(), document.pk)
        invalid = self.client.post(
            self.upload_url(),
            {
                "submission_id": str(uuid4()),
                "document": str(document.pk),
                "timezone": "Unknown/zone",
            },
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("confirm_timezone", invalid.context["form"].errors)
        self.assertIn("timezone", invalid.context["form"].errors)
        response = self.client.get(self.review_url(preview))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, '<script>alert("x")</script>')
        self.assertEqual(
            self.client.get(self.upload_url() + "?document=invalid").status_code, 404
        )
        foreign = DocumentEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Foreign CSV",
            file=SimpleUploadedFile("foreign.csv", message_csv()),
        )
        self.assertEqual(
            self.client.get(self.upload_url() + f"?document={foreign.pk}").status_code,
            404,
        )

    def test_invalid_confirmation_preserves_draft_and_source_removal_hides_rows(self):
        preview = self.prepare()
        invalid = self.client.post(
            self.review_url(preview),
            self.new_people()
            | {
                "account-first_name": "Preserved draft",
                "confirm": "",
            },
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(
            invalid.context["account_form"]["first_name"].value(), "Preserved draft"
        )
        self.assertFalse(
            AnalysisRun.objects.filter(parameters__phase="import").exists()
        )
        DocumentEvidence.objects.update(case=self.other_case)
        removed = self.client.get(self.review_url(preview))
        self.assertNotContains(removed, "Original café")
        self.assertNotContains(removed, 'name="account-mode"')

    def test_changed_source_hash_hides_rows_and_retry_follows_original_preview(self):
        preview = self.prepare()
        self.client.post(self.review_url(preview), self.new_people())
        final = AnalysisRun.objects.get(parameters__phase="import")
        AnalysisRun.objects.filter(pk=final.pk).update(
            status="failed", error_code="source_unavailable"
        )
        failed = self.client.get(self.review_url(final))
        self.assertContains(failed, "?processing=1")
        retried = self.client.post(
            reverse("core:analysis:retry", args=[self.case.pk, final.pk]),
            {
                "submission_id": str(uuid4()),
            },
        )
        self.assertEqual(retried.status_code, 302)
        attempt = AnalysisRun.objects.filter(previous_run=final).get()
        page = self.client.get(self.review_url(attempt))
        self.assertEqual(page.context["preview"].pk, preview.pk)
        self.assertEqual(page.context["run"].pk, attempt.pk)
        DocumentEvidence.objects.update(sha256="0" * 64)
        changed = self.client.get(self.review_url(attempt))
        self.assertNotContains(changed, "Original café")
        self.assertNotContains(changed, 'name="account-mode"')

    def test_large_compact_review_has_no_row_cap_and_paginates_source_rows(self):
        raw = message_csv(
            *({"Text": f"Large message {index} " + "." * 4200} for index in range(551))
        )
        self.assertGreater(len(raw), 2 * 1024 * 1024)
        preview = self.prepare(raw)
        self.assertEqual(preview.status, "complete", preview.error_code)
        self.assertEqual(preview.result.data["items"], [])
        self.assertEqual(preview.result.data["metrics"]["source_rows"], 551)
        response = self.client.get(self.review_url(preview) + "?page=23")
        rows = response.context["rows"]
        self.assertEqual(rows.paginator.count, 551)
        self.assertEqual(len(rows.object_list), 1)
        self.assertEqual(rows.object_list[0]["values"]["source_row"], 552)
        self.assertEqual(len(response.context["samples"]), 3)
        self.assertContains(response, "Page 23 of 23")
        self.assertContains(response, "Large message 550")
        self.assertNotContains(response, "Large message 500")

    def test_new_message_list_is_paginated_with_publication_counts(self):
        preview = self.prepare(
            message_csv(*({"Text": f"Message {index}"} for index in range(31)))
        )
        self.client.post(self.review_url(preview), self.new_people())
        final = AnalysisRun.objects.get(parameters__phase="import")
        process_run(final.pk)
        final.refresh_from_db()
        self.assertEqual(final.status, "complete", final.error_code)
        first = self.client.get(self.review_url(final))
        self.assertEqual(first.context["imported"].paginator.count, 31)
        self.assertEqual(len(first.context["imported"].object_list), 25)
        self.assertContains(first, "31 new messages added")
        second = self.client.get(self.review_url(final) + "?messages_page=2")
        self.assertEqual(len(second.context["imported"].object_list), 6)
        self.assertContains(second, "Messages page 2 of 2")

    def test_legacy_preview_stays_readable_and_requires_a_new_review(self):
        preview = self.prepare()
        data = deepcopy(preview.result.data)
        data["items"] = list(
            iter_import_rows(resolve_inputs(preview, self.case)[0], preview.parameters)
        )
        AnalysisResult.objects.filter(run=preview).update(data=data)
        AnalysisRun.objects.filter(pk=preview.pk).update(method_version="1")
        page = self.client.get(self.review_url(preview))
        self.assertContains(page, "Original café")
        self.assertContains(page, "earlier CSV review is retained for inspection")
        self.assertNotContains(page, 'name="account-mode"')
        self.assertEqual(
            self.client.post(self.review_url(preview), self.new_people()).status_code,
            409,
        )
        self.assertFalse(
            AnalysisRun.objects.filter(parameters__phase="import").exists()
        )

    def test_review_uses_retained_text_limit_and_withholds_changed_file_bytes(self):
        body = "Retained message " * 200
        with self.settings(PANDECT_MAX_TEXT_BYTES=6000):
            preview = self.prepare(message_csv({"Text": body}))
        with self.settings(PANDECT_MAX_TEXT_BYTES=1024):
            page = self.client.get(self.review_url(preview))
        self.assertEqual(page.context["metrics"]["ready_rows"], 1)
        self.assertEqual(page.context["rows"].object_list[0]["text"], body)
        document = DocumentEvidence.objects.get()
        with document.file.storage.open(document.file.name, "wb") as stream:
            stream.write(message_csv({"Text": "Changed source bytes"}))
        changed = self.client.get(self.review_url(preview))
        self.assertEqual(changed.context["samples"], [])
        self.assertNotContains(changed, "Retained message")
        self.assertNotContains(changed, "Changed source bytes")
        self.assertNotContains(changed, 'name="account-mode"')
