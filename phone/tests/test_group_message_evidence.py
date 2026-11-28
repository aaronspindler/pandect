from hashlib import sha256

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from cases.deletion import CaseDeletionBlocked, delete_case_record
from core.evidence.deletion import CASE_CONFLICT, execute_deletion, plan_deletion
from core.evidence.queries import scoped_evidence
from core.storage import canonical_bytes
from core.tests.testing import EvidenceTestMixin
from document.models import DocumentEvidence
from phone.models import (
    CommunicationContext,
    ConversationPairRevision,
    TextMessageEvidence,
)


class GroupMessageEvidenceTests(EvidenceTestMixin, TestCase):
    def export(self, **values):
        return self.create_evidence(
            DocumentEvidence,
            file=SimpleUploadedFile(
                "group.csv", b"Sender ID,Text\nsender-1,Hello\n", "text/csv"
            ),
            **values,
        )

    def context(self, message, source, *, row=1):
        return CommunicationContext(
            case=message.case,
            message=message,
            source_document=source,
            source_sha256=source.sha256,
            source_row=row,
            source_start=15,
            source_end=29,
            parser_version="imessage-csv-v1",
            timezone="UTC",
            raw_sender="sender-1",
        )

    def group_message(self, source, **values):
        return self.new_evidence(
            TextMessageEvidence, group_source=source, recipient=None, **values
        )

    def retain_group(self, source, *, row=1, bulk=False):
        message = self.group_message(source)
        context = self.context(message, source, row=row)
        with transaction.atomic():
            if bulk:
                message._prepare_original()
                TextMessageEvidence.objects.bulk_create([message])
                CommunicationContext.objects.bulk_create([context])
            else:
                message.save()
                context.save()
        return message, context

    def test_canonical_destinations_and_immutable_group_source(self):
        direct = self.create_evidence(TextMessageEvidence)
        source = self.export()
        group, _ = self.retain_group(source)
        self.assertEqual(
            direct.original_bytes(),
            canonical_bytes(
                {
                    "schema": "pandect.text-message.v1",
                    "body": direct.body,
                    "occurred_at": None,
                    "sender_id": str(self.person.pk),
                    "recipient_id": str(self.person.pk),
                }
            ),
        )
        self.assertEqual(
            group.original_data(),
            {
                "schema": "pandect.group-text-message.v1",
                "body": group.body,
                "occurred_at": None,
                "sender_id": str(self.person.pk),
                "group_source_id": str(source.pk),
            },
        )
        self.assertEqual(group.sha256, sha256(group.original_bytes()).hexdigest())
        group.group_source = self.export()
        with self.assertRaises(ValidationError):
            group.save()

    def test_bulk_database_writes_require_exactly_one_destination(self):
        for source, recipient in ((None, None), (self.export(), self.person)):
            with self.subTest(source=source, recipient=recipient):
                message = self.new_evidence(
                    TextMessageEvidence, group_source=source, recipient=recipient
                )
                message._prepare_original()
                with self.assertRaises(IntegrityError), transaction.atomic():
                    TextMessageEvidence.objects.bulk_create([message])

    def test_context_must_match_the_retained_group_export(self):
        source, different = self.export(), self.export()
        foreign = self.export(case=self.other_case, uploaded_by=self.outsider)
        with self.assertRaisesMessage(ValidationError, "group export from this case"):
            self.group_message(foreign).save()
        message = self.group_message(source)
        message.save()
        messages = scoped_evidence(self.case, "text-message")
        self.assertFalse(messages.filter(pk=message.pk).exists())
        with self.assertRaisesMessage(ValidationError, "use its retained group export"):
            self.context(message, different).save()
        self.context(message, source).save()
        self.assertTrue(messages.filter(pk=message.pk).exists())
        self.assertFalse(
            scoped_evidence(self.other_case, "text-message")
            .filter(pk=message.pk)
            .exists()
        )

    def test_inconsistent_ownership_blocks_deletion_from_either_document(self):
        source, different = self.export(), self.export()
        message, context = self.retain_group(source)
        CommunicationContext.objects.filter(pk=context.pk).update(
            source_document=different
        )
        self.assertFalse(scoped_evidence(self.case, "text-message").exists())
        for item, kind in (
            (source, "document"),
            (different, "document"),
            (message, "text-message"),
        ):
            with self.subTest(item=item):
                plan = plan_deletion(item, kind)
                self.assertEqual(plan.blocker, CASE_CONFLICT)
                self.assertFalse(plan.entries)
        with self.assertRaises(CaseDeletionBlocked):
            delete_case_record(self.case, self.case.pk)

    def test_source_deletion_removes_its_group_messages_and_context(self):
        source = self.export()
        message, context = self.retain_group(source)
        other_source = self.export()
        other_message, other_context = self.retain_group(other_source)
        self.owner.is_superuser = True
        self.owner.save(update_fields=("is_superuser",))
        initial = plan_deletion(source, "document")
        self.assertFalse(initial.blocker)
        later_message, _ = self.retain_group(source, row=2)
        reviewed = plan_deletion(source, "document")
        self.assertFalse(reviewed.blocker)
        self.assertNotEqual(reviewed.fingerprint, initial.fingerprint)
        self.assertEqual(
            {
                entry["id"]
                for entry in reviewed.entries
                if entry["action"] == "Delete record"
            }
            & {
                str(message.pk),
                str(context.pk),
                str(later_message.pk),
                str(other_message.pk),
            },
            {str(message.pk), str(context.pk), str(later_message.pk)},
        )
        execute_deletion(reviewed, self.owner)
        self.assertFalse(DocumentEvidence.objects.filter(pk=source.pk).exists())
        self.assertFalse(TextMessageEvidence.objects.filter(pk=message.pk).exists())
        self.assertFalse(CommunicationContext.objects.filter(pk=context.pk).exists())
        self.assertTrue(
            TextMessageEvidence.objects.filter(pk=other_message.pk).exists()
        )
        self.assertTrue(
            CommunicationContext.objects.filter(pk=other_context.pk).exists()
        )
        individual = plan_deletion(other_message, "text-message")
        self.assertFalse(individual.blocker)
        execute_deletion(individual, self.owner)
        self.assertFalse(
            CommunicationContext.objects.filter(pk=other_context.pk).exists()
        )
        self.assertTrue(DocumentEvidence.objects.filter(pk=other_source.pk).exists())

        remaining, _ = self.retain_group(other_source)
        delete_case_record(self.case, self.case.pk)
        self.assertFalse(TextMessageEvidence.objects.filter(pk=remaining.pk).exists())

    def test_group_mutations_leave_a_real_direct_self_pair_revision_unchanged(self):
        direct = self.create_evidence(TextMessageEvidence)
        start = ConversationPairRevision.objects.get(case=self.case).revision
        source = self.export()
        message, context = self.retain_group(source, bulk=True)
        TextMessageEvidence.objects.filter(pk=message.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.editor,
        )
        TextMessageEvidence.objects.filter(pk=message.pk).update(is_archived=False)
        CommunicationContext.objects.filter(pk=context.pk).update(raw_timestamp="today")
        context.delete()
        message.delete()
        self.assertEqual(
            ConversationPairRevision.objects.get(case=self.case).revision, start
        )

        self.context(direct, source).save()
        revision = ConversationPairRevision.objects.get(case=self.case)
        self.assertEqual(revision.first_person_id, self.person.pk)
        self.assertEqual(revision.second_person_id, self.person.pk)
        self.assertNotEqual(revision.revision, start)
