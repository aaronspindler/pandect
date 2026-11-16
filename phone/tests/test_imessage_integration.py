"""Publication, recovery, and shared evidence integration for message CSVs."""

import csv
import io
import json
from unittest.mock import patch
from uuid import uuid4, uuid5

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.http import Http404
from django.test import TestCase
from django.urls import reverse

from core.analysis.import_checks import imported_sources
from core.analysis.operations import get_operation
from core.analysis.services import process_run, start_run
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.analysis.deterministic.imessage import HEADERS
from phone.imessage_imports import confirm_import
from phone.models import CommunicationContext, TextMessageEvidence


class IMessagePipelineTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=HEADERS)
        writer.writeheader()
        base = dict.fromkeys(HEADERS, "") | {
            "Chat Session": "Synthetic correspondent",
            "Message Date": "2026-07-29 21:23:10",
            "Service": "iMessage",
        }
        for index, values in enumerate(
            (
                {"Type": "Incoming", "Sender ID": "15555550123", "Text": "First"},
                {"Type": "Outgoing", "Text": "Reply"},
                {"Type": "Outgoing", "Attachment": "unfetched.jpg"},
            )
        ):
            writer.writerow(base | {"Message ID": f"synthetic-{index}"} | values)
        self.document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["editor"],
            title="Synthetic CSV",
            file=ContentFile(stream.getvalue().encode(), name="synthetic.csv"),
        )
        self.preview, _ = start_run(
            self.users["editor"],
            self.case.pk,
            "imessage_import",
            [{"kind": "document", "id": str(self.document.pk)}],
            parameters={"phase": "preview", "timezone": "America/Toronto"},
        )
        process_run(self.preview.pk)
        self.preview.refresh_from_db()
        self.assertEqual(self.preview.status, "partial", self.preview.error_code)

    def confirmed(self):
        people = {
            role: {
                "id": str(uuid5(self.preview.pk, role)),
                "create": {"first_name": name},
            }
            for role, name in (("account", "Alex"), ("correspondent", "Blair"))
        }
        return confirm_import(
            self.users["editor"], self.case.pk, self.preview.pk, people
        )[0]

    def test_result_failure_rolls_back_people_messages_and_context_then_retry_succeeds(
        self,
    ):
        before = Person.objects.count()
        final = self.confirmed()
        self.assertEqual(Person.objects.count(), before)
        with patch("core.analysis.catalogue.project_result", side_effect=RuntimeError):
            self.assertEqual(process_run(final.pk), "processing_failed")
        self.assertEqual(Person.objects.count(), before)
        self.assertFalse(TextMessageEvidence.objects.exists())
        self.assertFalse(CommunicationContext.objects.exists())
        final.refresh_from_db()
        self.assertFalse(hasattr(final, "result"))
        retry, _ = start_run(
            self.users["editor"],
            self.case.pk,
            "imessage_import",
            [{"kind": "document", "id": str(self.document.pk)}],
            parameters=final.parameters,
            previous_run=final,
            submission_id=uuid4(),
        )
        process_run(retry.pk)
        retry.refresh_from_db()
        self.assertEqual(retry.status, "partial", retry.error_code)
        self.assertEqual(Person.objects.count(), before + 2)
        self.assertEqual(TextMessageEvidence.objects.count(), 2)

    def test_local_checks_include_only_published_messages_and_skip_reused_rows(self):
        with self.assertRaises(Http404):
            imported_sources(self.case, {"type": "messages", "id": self.preview.pk})
        final = self.confirmed()
        process_run(final.pk)
        final.refresh_from_db()
        self.assertEqual(final.status, "partial", final.error_code)
        self.assertEqual(final.result.data["import_publication_version"], 2)
        self.assertEqual(final.result.data["items"], [])
        kind, messages = imported_sources(
            self.case, {"type": "messages", "id": final.pk}
        )
        self.assertEqual(kind, "text-message")
        self.assertEqual(messages.count(), 2)
        self.assertEqual(
            CommunicationContext.objects.filter(import_run=final).count(), 2
        )
        retained_ids = set(messages.values_list("pk", flat=True))
        repeated, _ = start_run(
            self.users["editor"],
            self.case.pk,
            "imessage_import",
            [{"kind": "document", "id": str(self.document.pk)}],
            parameters=final.parameters,
            previous_run=final,
        )
        process_run(repeated.pk)
        repeated.refresh_from_db()
        self.assertEqual(repeated.status, "partial", repeated.error_code)
        _, repeated_messages = imported_sources(
            self.case, {"type": "messages", "id": repeated.pk}
        )
        self.assertFalse(repeated_messages.exists())
        self.assertEqual(
            set(TextMessageEvidence.objects.values_list("pk", flat=True)), retained_ids
        )

    def test_large_preview_has_no_row_count_or_analysis_item_ceiling(self):
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=HEADERS)
        writer.writeheader()
        base = dict.fromkeys(HEADERS, "") | {
            "Type": "Outgoing",
            "Text": "Synthetic message",
        }
        for number in range(10_001):
            writer.writerow(base | {"Message ID": f"large-{number}"})
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["editor"],
            title="Synthetic large CSV",
            file=ContentFile(stream.getvalue().encode(), name="large.csv"),
        )
        preview, _ = start_run(
            self.users["editor"],
            self.case.pk,
            "imessage_import",
            [{"kind": "document", "id": str(document.pk)}],
            parameters={"phase": "preview", "timezone": "America/Toronto"},
        )
        process_run(preview.pk)
        preview.refresh_from_db()
        self.assertEqual(preview.status, "complete", preview.error_code)
        self.assertEqual(preview.result.data["metrics"]["source_rows"], 10_001)
        self.assertEqual(preview.result.data["metrics"]["ready_rows"], 10_001)
        self.assertEqual(preview.result.data["items"], [])
        self.assertLess(len(json.dumps(preview.result.data).encode()), 16_384)

    def test_legacy_membership_still_resolves_original_published_rows(self):
        final = self.confirmed()
        process_run(final.pk)
        final.refresh_from_db()
        contexts = CommunicationContext.objects.filter(import_run=final)
        legacy = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["editor"],
            operation="imessage_import",
            method_version="1",
            parameters=final.parameters,
            inputs=final.inputs,
            status="partial",
        )
        AnalysisResult.objects.create(
            case=self.case,
            run=legacy,
            data={
                "import_publication_version": 1,
                "items": [
                    {
                        "values": {
                            "source_row": context.source_row,
                            "message_id": str(context.message_id),
                            "import_disposition": "created",
                        }
                    }
                    for context in contexts
                ],
            },
        )
        _, messages = imported_sources(self.case, {"type": "messages", "id": legacy.pk})
        self.assertEqual(messages.count(), 2)

    def test_legacy_preview_requires_a_new_review_before_confirmation(self):
        AnalysisRun.objects.filter(pk=self.preview.pk).update(method_version="1")
        with self.assertRaises(ValidationError):
            self.confirmed()
        self.assertFalse(TextMessageEvidence.objects.exists())

    def test_processing_and_document_routes_lead_to_review_without_exposing_mutation(
        self,
    ):
        self.login("editor")
        detail = reverse("core:analysis:detail", args=[self.case.pk, self.preview.pk])
        review = reverse("phone:imessage_review", args=[self.case.pk, self.preview.pk])
        self.assertRedirects(self.client.get(detail), review)
        self.assertContains(self.client.get(detail + "?processing=1"), "Synthetic CSV")
        processing = reverse("core:analysis:processing", args=[self.case.pk])
        self.assertContains(self.client.get(processing), "Prepare message review")
        source = reverse(
            "core:evidence:detail", args=[self.case.pk, "document", self.document.pk]
        )
        link = reverse("phone:imessage_upload", args=[self.case.pk])
        self.assertContains(self.client.get(source), link + "?document=")
        self.login("viewer")
        self.assertNotContains(self.client.get(source), link + "?document=")
        self.assertContains(self.client.get(review), "Viewer access")
        self.assertEqual(self.client.post(review, {}).status_code, 403)

    def test_csv_operation_is_not_offered_by_generic_analysis_creation(self):
        self.assertEqual(get_operation("conversation_import").version, "3")
        self.login("editor")
        url = reverse("core:analysis:create", args=[self.case.pk])
        before = AnalysisRun.objects.count()
        response = self.client.get(url + "?operation=imessage_import")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="operation" value="imessage_import"')
        self.assertEqual(AnalysisRun.objects.count(), before)
