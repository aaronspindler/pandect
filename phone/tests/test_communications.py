"""Retained communication originals, append-only material, and case boundaries."""

import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import RestrictedError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from cases.models import CaseMembership
from core.evidence.types import CALL_KINDS, COMMUNICATION_KINDS, KINDS
from core.testing import COMMUNICATION_MODELS, EvidenceTestMixin, EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.models import (
    CallEvidenceAttachment,
    CommunicationContext,
    FaceTimeEvidence,
    PhoneCallEvidence,
    TextMessageEvidence,
)


class CommunicationModelTests(EvidenceTestMixin, TestCase):
    def attachment(self, call=None, **values):
        call = call or self.create_evidence(PhoneCallEvidence)
        return CallEvidenceAttachment.objects.create(
            case=call.case,
            uploaded_by=self.editor,
            **{
                "phone_call"
                if isinstance(call, PhoneCallEvidence)
                else "facetime": call
            },
            **({"transcript": "  Supplied café\r\n  "} | values),
        )

    def test_original_json_retains_time_precision_text_and_zero_duration(self):
        instant = datetime(2026, 10, 26, 12, 30, 15, 123456, tzinfo=UTC)
        for model in COMMUNICATION_MODELS:
            item = self.create_evidence(
                model,
                occurred_at=instant,
                **(
                    {}
                    if model is COMMUNICATION_MODELS[0]
                    else {"duration": timedelta()}
                ),
            )
            item.refresh_from_db()
            data = item.original_data()
            self.assertEqual(data["occurred_at"], instant.isoformat())
            self.assertEqual(data["sender_id"], str(self.person.pk))
            self.assertEqual(
                item.sha256, hashlib.sha256(item.original_bytes()).hexdigest()
            )
            if hasattr(item, "body"):
                self.assertEqual(data["body"], "  Original message.\r\n")
            else:
                self.assertEqual(data["duration_microseconds"], 0)
            original = item.original_bytes()
            self.person.first_name = "Corrected person name"
            self.person.save()
            self.assertEqual(item.original_bytes(), original)

    def test_cross_case_participants_and_invalid_timing_rejected_on_normal_saves(self):
        other = Person.objects.create(case=self.other_case, first_name="Other person")
        for model in COMMUNICATION_MODELS:
            for values in (
                {"sender": other},
                {"recipient": other},
                {"occurred_at": datetime(2026, 10, 26)},
            ):
                with (
                    self.subTest(model=model, values=values),
                    self.assertRaises(ValidationError),
                ):
                    self.new_evidence(model, **values).save()
            if model is not COMMUNICATION_MODELS[0]:
                with self.assertRaises(ValidationError):
                    self.new_evidence(model, duration=timedelta(seconds=-1)).save()
                item = self.create_evidence(model)
                with transaction.atomic(), self.assertRaises(IntegrityError):
                    model.objects.filter(pk=item.pk).update(
                        duration=timedelta(seconds=-1)
                    )

    def test_attachments_preserve_content_and_leave_call_hash_unchanged(self):
        for model in (PhoneCallEvidence, FaceTimeEvidence):
            call = self.create_evidence(model)
            original = call.original_bytes(), call.sha256
            for transcript, audio in (
                ("  café\r\n  ", False),
                ("", True),
                ("text", True),
            ):
                addition = self.attachment(
                    call,
                    transcript=transcript,
                    file=SimpleUploadedFile(
                        "recording.wav", b"audio\x00\r\n", content_type="audio/wav"
                    )
                    if audio
                    else "",
                )
                addition.refresh_from_db()
                self.assertEqual(addition.transcript, transcript)
                self.assertEqual(
                    addition.sha256,
                    hashlib.sha256(addition.original_bytes()).hexdigest(),
                )
                if audio:
                    self.assertEqual(addition.file.read(), b"audio\x00\r\n")
                    self.assertEqual(
                        addition.audio_sha256,
                        hashlib.sha256(b"audio\x00\r\n").hexdigest(),
                    )
                    self.assertTrue(
                        addition.file.name.startswith(
                            f"evidence/cases/{self.case.pk}/callevidenceattachment/"
                        )
                    )
                    self.assertNotIn("recording.wav", addition.file.name)
            call.refresh_from_db()
            self.assertEqual((call.original_bytes(), call.sha256), original)
            self.assertEqual(call.attachments.count(), 3)

    def test_parent_constraints_and_content_required(self):
        call = self.create_evidence(PhoneCallEvidence)
        facetime = self.create_evidence(FaceTimeEvidence)
        for values in (
            {"phone_call": None},
            {"facetime": facetime},
            {"case": self.other_case},
            {"transcript": ""},
        ):
            item = CallEvidenceAttachment(
                case=self.case,
                phone_call=call,
                uploaded_by=self.editor,
                transcript="original",
            )
            for name, value in values.items():
                setattr(item, name, value)
            with self.subTest(values=values), self.assertRaises(ValidationError):
                item.save()
        item = self.attachment(call)
        for values in (
            {"phone_call": None},
            {"facetime": facetime},
            {"transcript": ""},
            {"sha256": "invalid"},
            {"byte_size": 1},
            {"file": "fake"},
        ):
            with (
                self.subTest(values=values),
                transaction.atomic(),
                self.assertRaises(IntegrityError),
            ):
                CallEvidenceAttachment.objects.filter(pk=item.pk).update(**values)

    def test_attachment_mutation_replacement_and_resurrection_rejected(self):
        item = self.attachment(file=SimpleUploadedFile("original.wav", b"original"))
        for name, value in {
            "case": self.other_case,
            "uploaded_by": self.owner,
            "transcript": "changed",
            "created_at": datetime(2000, 1, 1, tzinfo=UTC),
            "submission_id": uuid4(),
            "sha256": "0" * 64,
            "audio_sha256": "0" * 64,
            "byte_size": 1,
            "original_filename": "changed.wav",
            "declared_content_type": "changed",
            "file": ContentFile(b"changed", name=item.file.name),
        }.items():
            changed = CallEvidenceAttachment.objects.get(pk=item.pk)
            setattr(changed, name, value)
            with self.subTest(field=name):
                with self.assertRaises(ValidationError):
                    changed.full_clean()
                with self.assertRaises(ValidationError):
                    changed.save()
        with self.assertRaises(ValidationError):
            self.attachment(item.call, pk=item.pk)
        key = item.file.name
        with self.captureOnCommitCallbacks(execute=True):
            CallEvidenceAttachment.objects.filter(pk=item.pk).delete()
        self.assertFalse(item.file.storage.exists(key))
        with self.assertRaises(ValidationError):
            item.save()

    def test_person_restriction_case_cascade_and_rollback_keep_originals(self):
        call = self.create_evidence(PhoneCallEvidence)
        addition = self.attachment(call, file=SimpleUploadedFile("call.wav", b"audio"))
        key = addition.file.name
        with self.assertRaises(RestrictedError):
            self.person.delete()
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    type(self.case).objects.get(pk=self.case.pk).delete()
                    raise RuntimeError("rollback")
            except RuntimeError:
                pass
        self.assertTrue(addition.file.storage.exists(key))
        self.assertTrue(CallEvidenceAttachment.objects.filter(pk=addition.pk).exists())
        with self.captureOnCommitCallbacks(execute=True):
            self.case.delete()
        self.assertFalse(addition.file.storage.exists(key))
        self.assertFalse(CallEvidenceAttachment.objects.exists())


class CommunicationViewTests(EvidenceViewTestMixin, TestCase):
    def test_imported_reply_and_thread_lists_use_message_case_scope(self):
        source = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Export",
            file=ContentFile(b"Synthetic export", name="messages.html"),
        )
        rows = (
            ("Selected message", "selected", "reply"),
            ("Visible reply candidate", "reply", ""),
            ("Inconsistent reply candidate", "reply", ""),
        )
        messages = []
        for number, (title, native_id, reply_to_id) in enumerate(rows, 1):
            message = TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                sender=self.person,
                recipient=self.person,
                title=title,
                body="Synthetic message",
            )
            CommunicationContext.objects.create(
                case=self.case,
                message=message,
                source_document=source,
                source_sha256=source.sha256,
                source_row=number,
                source_start=number - 1,
                source_end=number,
                parser_version="test",
                timezone="UTC",
                raw_sender="Synthetic sender",
                native_message_id=native_id,
                native_thread_id="thread",
                reply_to_id=reply_to_id,
            )
            messages.append(message)
        other_person = Person.objects.create(
            case=self.other_case, first_name="Unrelated person"
        )
        TextMessageEvidence.objects.filter(pk=messages[-1].pk).update(
            recipient=other_person
        )

        self.login("viewer")
        response = self.client.get(self.url("detail", messages[0], kind="text-message"))
        self.assertContains(response, messages[1].title)
        self.assertNotContains(response, messages[2].title)
        self.assertEqual(
            [
                context.message_id
                for context in response.context["native_reply_candidates"]
            ],
            [messages[1].pk],
        )
        self.assertEqual(
            [
                context.message_id
                for context in response.context["native_thread_messages"]
            ],
            [messages[0].pk, messages[1].pk],
        )

    def create_call(self, kind="phone-call", **values):
        self.login("owner")
        response = self.client.post(
            self.url("create"),
            self.payload(kind, **values),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        return KINDS[kind].objects.get(pk=response.json()["id"])

    def attachment_url(self, item, attachment=None, component="manifest"):
        kwargs = {
            "case_id": item.case_id,
            "kind": item.communication_kind,
            "evidence_id": item.pk,
        }
        if attachment:
            kwargs |= {"attachment_id": attachment.pk, "component": component}
        return reverse(
            "phone:attachment_download" if attachment else "phone:add_attachment",
            kwargs=kwargs,
        )

    def add(self, call, **values):
        return self.client.post(
            self.attachment_url(call),
            {
                "submission_id": str(uuid4()),
                "transcript": "  Supplied café\r\n<script>source</script>  ",
            }
            | values,
            HTTP_ACCEPT="application/json",
        )

    def test_initial_content_combinations_and_idempotent_later_additions(self):
        for kind in CALL_KINDS:
            for transcript, audio in (
                ("", False),
                ("supplied", False),
                ("", True),
                ("supplied", True),
            ):
                values = {"transcript": transcript}
                if audio:
                    values["file"] = SimpleUploadedFile("call.wav", b"audio")
                item = self.create_call(kind, **values)
                self.assertEqual(
                    item.attachments.count(), int(bool(transcript or audio))
                )
                checksum = item.sha256
                submission = str(uuid4())
                self.login("editor")
                added = self.add(item, submission_id=submission)
                self.assertEqual(added.status_code, 201, added.content)
                repeated = self.add(item, submission_id=submission)
                self.assertEqual(repeated.status_code, 200)
                self.assertEqual(added.json()["id"], repeated.json()["id"])
                self.assertEqual(
                    item.attachments.count(), int(bool(transcript or audio)) + 1
                )
                item.refresh_from_db()
                self.assertEqual(item.sha256, checksum)
                detail = self.client.get(self.url("detail", item, kind=kind))
                self.assertNotContains(detail, "<script>source</script>")
                self.assertContains(detail, "&lt;script&gt;source&lt;/script&gt;")

    def test_empty_attachment_post_is_bound_and_reports_required_fields(self):
        item = self.create_call()
        response = self.client.post(
            self.attachment_url(item), {}, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("submission_id", response.json()["errors"])
        self.assertFalse(item.attachments.exists())

    def test_initial_retry_and_storage_failure_are_atomic(self):
        self.login("owner")
        payload = self.payload("phone-call", transcript="Initial transcript")
        with patch(
            "phone.forms.CallEvidenceAttachment.objects.create",
            side_effect=OSError("private storage detail"),
        ):
            response = self.client.post(
                self.url("create"), payload, HTTP_ACCEPT="application/json"
            )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(PhoneCallEvidence.objects.exists())
        self.assertFalse(CallEvidenceAttachment.objects.exists())
        for status in (201, 200):
            response = self.client.post(
                self.url("create"), payload, HTTP_ACCEPT="application/json"
            )
            self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(PhoneCallEvidence.objects.count(), 1)
        self.assertEqual(CallEvidenceAttachment.objects.count(), 1)

    def test_downloads_are_exact_private_and_parent_scoped(self):
        item = self.create_call(
            transcript="  café\r\n  ",
            file=SimpleUploadedFile("unsafe.html", b"<script>audio</script>"),
        )
        addition = item.attachments.get()
        other = self.create_call()
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            for component, expected in (
                ("manifest", addition.original_bytes()),
                ("transcript", addition.transcript.encode()),
                ("audio", b"<script>audio</script>"),
            ):
                response = self.client.get(
                    self.attachment_url(item, addition, component)
                )
                actual = (
                    b"".join(response.streaming_content)
                    if response.streaming
                    else response.content
                )
                self.assertEqual(actual, expected)
                self.assertEqual(response["Content-Type"], "application/octet-stream")
                self.assertIn("attachment;", response["Content-Disposition"])
                self.assertIn("no-store", response["Cache-Control"])
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")
            self.assertEqual(
                self.client.get(self.attachment_url(other, addition)).status_code, 404
            )
        self.login("outsider")
        self.assertEqual(
            self.client.get(self.attachment_url(item, addition)).status_code, 404
        )
        self.login("owner")
        self.assertEqual(
            self.client.get(self.attachment_url(item, addition, "invalid")).status_code,
            404,
        )
        addition.file.storage.delete(addition.file.name)
        unavailable = self.client.get(self.attachment_url(item, addition, "audio"))
        self.assertEqual(unavailable.status_code, 503)
        self.assertNotContains(unavailable, addition.file.name, status_code=503)

    def test_current_permissions_csrf_and_submission_conflicts(self):
        item = self.create_call()
        other = self.create_call("facetime")
        self.login("editor")
        submission = str(uuid4())
        self.assertEqual(self.add(item, submission_id=submission).status_code, 201)
        self.assertEqual(self.add(other, submission_id=submission).status_code, 409)
        self.login("owner")
        self.assertEqual(self.add(item, submission_id=submission).status_code, 409)
        for role, status in (("viewer", 403), ("outsider", 404)):
            self.login(role)
            self.assertEqual(
                self.client.get(self.attachment_url(item)).status_code, status
            )
            self.assertEqual(self.add(item).status_code, status)
        self.login("editor")
        CaseMembership.objects.filter(case=self.case, user=self.users["editor"]).update(
            role="viewer"
        )
        self.assertEqual(self.add(item).status_code, 403)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(self.add(item).status_code, 404)
        secured = Client(enforce_csrf_checks=True)
        secured.force_login(self.users["owner"])
        self.assertEqual(
            secured.post(
                self.attachment_url(item), {"transcript": "forged"}
            ).status_code,
            403,
        )

    @override_settings(PANDECT_MAX_TEXT_BYTES=4, PANDECT_MAX_FILE_BYTES=4)
    def test_limits_missing_content_and_later_storage_failure(self):
        item = self.create_call()
        for values in (
            {"transcript": ""},
            {"transcript": "ééé"},
            {"transcript": "", "file": SimpleUploadedFile("big.wav", b"12345")},
            {"transcript": "", "file": SimpleUploadedFile("empty.wav", b"")},
        ):
            self.assertIn(self.add(item, **values).status_code, (400, 413))
        with patch(
            "phone.forms.CallEvidenceAttachment.objects.create",
            side_effect=OSError("private error"),
        ):
            failed = self.add(item, transcript="abc")
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn(b"private error", failed.content)
        self.assertFalse(item.attachments.exists())

    def test_participant_forms_no_people_state_and_forged_choices(self):
        self.login("owner")
        other = Person.objects.create(
            case=self.other_case, first_name="Hidden participant"
        )
        for kind in COMMUNICATION_KINDS:
            form = self.client.get(self.url("create"), {"kind": kind})
            self.assertNotContains(form, "Hidden participant")
            for name in ("sender", "recipient"):
                response = self.client.post(
                    self.url("create"), self.payload(kind, **{name: str(other.pk)})
                )
                self.assertEqual(response.status_code, 400)
        Person.objects.filter(case=self.case).delete()
        self.assertContains(
            self.client.get(self.url("create"), {"kind": "text-message"}),
            "Add a person to this case",
        )

    def test_search_returns_unique_calls_and_case_counts_exclude_attachments(self):
        item = self.create_call()
        for _ in range(2):
            self.assertEqual(
                self.add(
                    item, file=SimpleUploadedFile("matching.wav", b"audio")
                ).status_code,
                201,
            )
        for query in ("matching.wav", "Synthetic person"):
            result = self.client.get(self.url("list"), {"q": query})
            self.assertEqual(result.context["page_obj"].paginator.count, 1)
        result = self.client.get(self.url("list"))
        self.assertEqual(result.context["page_obj"].paginator.count, 1)
        self.login("viewer")
        self.assertNotContains(
            self.client.get(self.url("detail", item, kind="phone-call")),
            "Add transcript or audio",
        )


class CommunicationMigrationTests(TestCase):
    def test_fresh_migration_state_has_domain_owned_communication_relations(self):
        state = MigrationExecutor(connection).loader.project_state().apps
        message = state.get_model("phone", "TextMessageEvidence")
        call = state.get_model("phone", "PhoneCallEvidence")
        attachment = state.get_model("phone", "CallEvidenceAttachment")
        context = state.get_model("phone", "CommunicationContext")
        self.assertEqual(
            message._meta.get_field("sender").related_model._meta.label, "people.Person"
        )
        self.assertEqual(
            call._meta.get_field("case").related_model._meta.label, "cases.Case"
        )
        self.assertIs(attachment._meta.get_field("phone_call").related_model, call)
        self.assertIs(context._meta.get_field("message").related_model, message)
        self.assertEqual(
            context._meta.get_field("source_document").related_model._meta.label,
            "document.DocumentEvidence",
        )
        # The test database has actually applied these migrations to PostgreSQL.
        self.assertFalse(message.objects.exists())
        self.assertFalse(call.objects.exists())
        self.assertFalse(attachment.objects.exists())
        self.assertFalse(context.objects.exists())
