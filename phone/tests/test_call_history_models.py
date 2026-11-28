"""Imported calls keep case scope and provenance independently of person links."""

from datetime import UTC, datetime, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.db.models.deletion import RestrictedError
from django.http import Http404
from django.test import TestCase

from cases.deletion import delete_case_record
from cases.models import CaseMembership
from core.evidence.deletion import execute_deletion, plan_deletion
from core.evidence.queries import person_evidence, scoped_evidence
from core.storage import canonical_bytes
from core.tests.testing import EvidenceTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.models import (
    CallHistoryImport,
    CallHistoryRow,
    CallImportSource,
    CallPersonLink,
    FaceTimeEvidence,
    PhoneCallEvidence,
    link_imported_person,
)


class CallHistoryModelTests(EvidenceTestMixin, TestCase):
    def imported_call(self, model=PhoneCallEvidence):
        document = self.create_evidence(
            DocumentEvidence,
            file=SimpleUploadedFile("calls.csv", b"synthetic CSV", "text/csv"),
        )
        source = CallImportSource.objects.create(
            case=self.case, label="Synthetic line", created_by=self.editor
        )
        batch = CallHistoryImport.objects.create(
            case=self.case,
            document=document,
            source=source,
            source_sha256=document.sha256,
            source_label=source.label,
            uploaded_by=self.editor,
            requested_by=self.editor,
            timezone="America/Toronto",
            parser_version="test.v1",
            status=CallHistoryImport.Status.COMPLETED,
        )
        row = CallHistoryRow.objects.create(
            batch=batch,
            number=2,
            raw={"Number": " +1 202 555 0100 ", "Contact": "Synthetic label"},
            kind=model.communication_kind,
            direction="incoming",
            occurred_at=datetime(2026, 11, 25, 12, tzinfo=UTC),
            duration=timedelta(),
            disposition=CallHistoryRow.Disposition.PUBLISHED,
        )
        return self.create_evidence(
            model,
            sender=None,
            recipient=None,
            import_row=row,
            occurred_at=row.occurred_at,
            duration=row.duration,
        )

    def test_manual_originals_keep_both_people_and_v1_bytes(self):
        for model in (PhoneCallEvidence, FaceTimeEvidence):
            with self.subTest(model=model.__name__):
                item = self.create_evidence(model, duration=timedelta())
                self.assertEqual(
                    item.original_bytes(),
                    canonical_bytes(
                        {
                            "schema": f"pandect.{item.communication_kind}.v1",
                            "sender_id": str(self.person.pk),
                            "recipient_id": str(self.person.pk),
                            "occurred_at": None,
                            "duration_microseconds": 0,
                        }
                    ),
                )
                with self.assertRaises(ValidationError):
                    self.new_evidence(model, sender=None).save()
                with transaction.atomic(), self.assertRaises(IntegrityError):
                    model.objects.filter(pk=item.pk).update(recipient=None)

    def test_import_without_people_is_scoped_and_links_preserve_original(self):
        call = self.imported_call(FaceTimeEvidence)
        row = call.import_row
        records = scoped_evidence(self.case, "facetime")
        original = call.original_bytes(), call.sha256
        self.assertIsNone(records.get(pk=call.pk).sender_id)
        self.assertEqual(call.original_data()["row"], row.raw)
        self.assertEqual(
            call.original_data()["source"]["sha256"], row.batch.document.sha256
        )
        row.raw = {"Number": "changed"}
        with self.assertRaises(ValidationError):
            row.save()
        row.refresh_from_db()
        self.assertFalse(person_evidence(records, self.person).exists())
        outside = Person.objects.create(case=self.other_case, first_name="Outside")
        for actor, error in (
            (self.viewer, PermissionDenied),
            (self.outsider, Http404),
        ):
            with self.subTest(actor=actor), self.assertRaises(error):
                link_imported_person(
                    call, role="sender", person=self.person, actor=actor
                )
        with self.assertRaises(ValidationError):
            link_imported_person(call, role="sender", person=outside, actor=self.editor)
        FaceTimeEvidence.objects.filter(pk=call.pk).update(sender=outside)
        self.assertFalse(records.filter(pk=call.pk).exists())
        FaceTimeEvidence.objects.filter(pk=call.pk).update(sender=None)
        link = link_imported_person(
            call, role="sender", person=self.person, actor=self.editor
        )
        call.refresh_from_db()
        self.assertEqual(
            (link.row_id, link.person_id, link.linked_by_id),
            (row.pk, self.person.pk, self.editor.pk),
        )
        self.assertEqual(person_evidence(records, self.person).get().pk, call.pk)
        self.assertEqual((call.original_bytes(), call.sha256), original)
        CaseMembership.objects.filter(case=self.case, user=self.editor).delete()
        with self.assertRaises(Http404):
            link_imported_person(
                call, role="recipient", person=self.person, actor=self.editor
            )

    def test_deleting_source_includes_call_review_and_attributed_link(self):
        call = self.imported_call()
        source_row = call.import_row
        batch = source_row.batch
        document = batch.document
        link = link_imported_person(
            call, role="sender", person=self.person, actor=self.editor
        )
        with self.assertRaises(RestrictedError):
            document.delete()
        self.owner.is_superuser = True
        self.owner.save(update_fields=["is_superuser"])
        plan = plan_deletion(document, "document")
        self.assertFalse(plan.blocker)
        removed = {entry["id"] for entry in plan.entries}
        self.assertLessEqual(
            {str(item.pk) for item in (call, batch, source_row, link)}, removed
        )
        execute_deletion(plan, self.owner)
        self.assertFalse(PhoneCallEvidence.objects.filter(pk=call.pk).exists())
        self.assertFalse(CallHistoryImport.objects.filter(pk=batch.pk).exists())
        self.assertFalse(CallHistoryRow.objects.filter(pk=source_row.pk).exists())
        self.assertFalse(CallPersonLink.objects.filter(pk=link.pk).exists())
        self.assertTrue(CallImportSource.objects.filter(pk=batch.source_id).exists())

    def test_whole_case_deletion_includes_imports_and_line(self):
        call = self.imported_call()
        link_imported_person(call, role="sender", person=self.person, actor=self.editor)
        delete_case_record(self.case, self.case.pk)
        self.assertFalse(CallImportSource.objects.exists())
        self.assertFalse(CallPersonLink.objects.exists())
