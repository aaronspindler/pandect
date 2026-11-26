"""Published comparisons link exact private sources throughout their lifetime."""

from datetime import UTC, datetime
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.errors import AnalysisError
from core.analysis.services import start_run
from core.analysis.sources import source_id
from core.analysis.worker import process_run
from core.models import AnalysisResult
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from document.tests.test_analysis import pdf_bytes
from geo.models import (
    PersonLocationEvidence,
    VehicleLocationEvidence,
    VehicleLocationImport,
)
from people.models import Person
from phone.models import (
    CallEvidenceAttachment,
    CommunicationContext,
    PhoneCallEvidence,
    TextMessageEvidence,
)
from text.models import TextEvidence


class ComparisonReaderTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.observation = PersonLocationEvidence.objects.create(
            case=self.case,
            person=self.person,
            title="Doorway observation",
            uploaded_by=self.users["owner"],
            address="49 Harbour Road, Testville",
            observed_at=datetime(2026, 10, 4, 10, tzinfo=UTC),
        )
        self.movement = {"kind": "person-location", "id": str(self.observation.pk)}

    def request_run(
        self,
        choice,
        start,
        end,
        *,
        actor="owner",
        submission=None,
        previous=None,
        **fields,
    ):
        params = {
            "claims": [
                {
                    "source_id": source_id(choice),
                    "start": start,
                    "end": end,
                    "status": "proposed",
                    "subjects": [f"person:{self.person.pk}"],
                    "place_resolved": True,
                    "address": self.observation.address,
                    "time_start": "2026-10-04T10:00Z",
                    "time_basis": "explicit",
                    **fields,
                }
            ],
            "observations": [source_id(self.movement)],
        }
        return start_run(
            self.users[actor],
            self.case.pk,
            "journey_claims",
            [choice, self.movement],
            parameters=params,
            submission_id=submission,
            previous_run=previous,
        )

    def comparison_urls(self, run):
        args = (self.case.pk, run.pk)
        return (
            reverse("core:analysis:detail", args=args),
            reverse("geo:plan_passage", args=(*args, 0, 0)),
            reverse("core:analysis:download", args=args),
        )

    def test_published_comparison_opens_every_native_page_and_preserves_history(self):
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Calendar pages",
            file=SimpleUploadedFile(
                "calendar.pdf", pdf_bytes("First proposal", "Next place")
            ),
        )
        original = {"kind": "document", "id": str(document.pk)}
        sibling = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Other calendar",
            file=SimpleUploadedFile("other.txt", b"A separate calendar."),
        )
        extraction, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "native_text",
            [original, {"kind": "document", "id": str(sibling.pk)}],
        )
        process_run(extraction.pk)
        choice = original | {"result_id": str(extraction.result.pk)}
        pieces = extraction.result.data["items"]
        body = "\n".join(
            piece["text"]
            for piece in pieces
            if source_id(original) in piece["source_ids"]
        )
        start, end = body.index("proposal"), body.index("place") + len("place")
        submission = uuid4()
        run, created = self.request_run(choice, start, end, submission=submission)
        self.assertTrue(created)
        repeat, created = self.request_run(choice, start, end, submission=submission)
        self.assertEqual(repeat.pk, run.pk)
        self.assertFalse(created)
        process_run(run.pk)
        process_run(run.pk)
        self.assertEqual(AnalysisResult.objects.filter(run=run).count(), 1)
        self.login("viewer")
        detail, passage, download = self.comparison_urls(run)
        self.assertContains(self.client.get(detail), passage)
        response = self.client.get(passage)
        self.assertContains(response, f"<mark>{body[start:end]}</mark>", html=True)
        for page in (1, 2):
            self.assertContains(response, f"?document_page={page}")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.client.get(download).status_code, 200)
        rerun, _ = self.request_run(choice, start, end, previous=run)
        self.assertEqual(rerun.inputs, run.inputs)
        self.assertEqual(rerun.previous_run_id, run.pk)
        sibling.file.storage.delete(sibling.file.name)
        self.assertEqual(self.client.get(passage).status_code, 200)
        self.assertEqual(self.client.get(download).status_code, 200)
        foreign = document.file.storage.save(
            f"evidence/cases/{self.other_case.pk}/documentevidence/{uuid4().hex}",
            SimpleUploadedFile("foreign.pdf", b"Other file."),
        )
        DocumentEvidence.objects.filter(pk=document.pk).update(file=foreign)
        self.assert_withheld(run)
        DocumentEvidence.objects.filter(pk=document.pk).update(file=document.file.name)
        document.file.storage.delete(document.file.name)
        self.assertNotContains(self.client.get(detail), "49 Harbour Road")
        for url in (passage, download):
            self.assertEqual(self.client.get(url).status_code, 404)
        self.login("outsider")
        for url in (detail, passage, download):
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_transcript_citation_pins_addition_and_the_right_unicode_occurrence(self):
        call = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            sender=self.person,
            recipient=self.person,
            title="Reported phone call",
        )
        addition = CallEvidenceAttachment.objects.create(
            case=self.case,
            phone_call=call,
            uploaded_by=self.users["owner"],
            transcript="📅 Visit here.\nNew plan: Visit here.",
        )
        CallEvidenceAttachment.objects.create(
            case=self.case,
            phone_call=call,
            uploaded_by=self.users["owner"],
            transcript="An unrelated addition",
        )
        choice = {
            "kind": "phone-call",
            "id": str(call.pk),
            "attachment_id": str(addition.pk),
        }
        start = addition.transcript.rindex("Visit")
        run, _ = self.request_run(choice, start, start + len("Visit here."))
        process_run(run.pk)
        self.login("viewer")
        detail, passage, download = self.comparison_urls(run)
        response = self.client.get(passage)
        self.assertContains(response, "New plan: <mark>Visit here.</mark>")
        self.assertContains(response, f"#attachment-{addition.pk}")
        exchanges = self.client.get(response.context["exchange_url"])
        self.assertEqual(exchanges.context["filters"]["method"], "conversation_link")
        self.assertEqual(exchanges.context["filters"]["evidence"], call.pk)
        self.assertNotContains(response, "unrelated addition")
        self.assertEqual(
            self.client.get(
                reverse("geo:plan_passage", args=(self.case.pk, run.pk, 0, 1))
            ).status_code,
            404,
        )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        for url in (detail, passage, download):
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_revocation_and_changed_source_block_publication_and_forged_inputs(self):
        text = self.text()
        choice = {"kind": "text", "id": str(text.pk)}
        foreign = Person.objects.create(case=self.other_case, first_name="Elsewhere")
        with self.assertRaises(AnalysisError):
            self.request_run(choice, 0, 8, subjects=[f"person:{foreign.pk}"])
        unrelated, _ = start_run(
            self.users["owner"], self.case.pk, "entities", [choice]
        )
        process_run(unrelated.pk)
        with self.assertRaises(AnalysisError):
            self.request_run(choice | {"result_id": str(unrelated.result.pk)}, 0, 1)
        revoked, _ = self.request_run(choice, 0, 8, actor="editor")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        process_run(revoked.pk)
        changed, _ = self.request_run(choice, 0, 8)
        TextEvidence.objects.filter(pk=text.pk).update(body="different")
        process_run(changed.pk)
        self.assertFalse(
            AnalysisResult.objects.filter(run__in=(revoked, changed)).exists()
        )

    def assert_withheld(self, run):
        from core.analysis.source_resolution import (
            SourceResolutionCache,
            resolve_inputs,
        )

        self.login("viewer")
        detail, passage, download = self.comparison_urls(run)
        self.assertContains(self.client.get(detail), "Result withheld")
        self.assertEqual(self.client.get(passage).status_code, 404)
        self.assertEqual(self.client.get(download).status_code, 404)
        with self.assertRaises(AnalysisError):
            resolve_inputs(
                run,
                self.case,
                cache=SourceResolutionCache(self.case, with_content=False),
            )

    def test_same_length_message_and_transcript_edits_withhold_results(
        self,
    ):
        message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            sender=self.person,
            recipient=self.person,
            title="Synthetic message",
            body="See you Monday.",
        )
        message_run, _ = self.request_run(
            {"kind": "text-message", "id": str(message.pk)}, 0, len(message.body)
        )
        process_run(message_run.pk)
        TextMessageEvidence.objects.filter(pk=message.pk).update(body="See you Friday.")
        self.assert_withheld(message_run)
        self.login("editor")
        preview = self.client.post(
            reverse("core:analysis:create", args=[self.case.pk]),
            {
                "operation": "journey_claims",
                "action": "prepare",
                "claim": f"text-message:{message.pk}",
                "movement": f"person-location:{self.observation.pk}",
            },
        )
        self.assertContains(preview, "Review the selection")
        self.assertNotContains(preview, "See you Friday.")

        call = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            sender=self.person,
            recipient=self.person,
            title="Synthetic call",
        )
        addition = CallEvidenceAttachment.objects.create(
            case=self.case,
            phone_call=call,
            uploaded_by=self.users["owner"],
            transcript="See you Monday.",
            file=SimpleUploadedFile("recording.wav", b"Synthetic call recording."),
        )
        call_run, _ = self.request_run(
            {
                "kind": "phone-call",
                "id": str(call.pk),
                "attachment_id": str(addition.pk),
            },
            0,
            len(addition.transcript),
        )
        process_run(call_run.pk)
        other_kind = addition.file.storage.save(
            f"evidence/cases/{self.case.pk}/audioevidence/{uuid4().hex}",
            SimpleUploadedFile("other.wav", b"Other file."),
        )
        CallEvidenceAttachment.objects.filter(pk=addition.pk).update(file=other_kind)
        self.assert_withheld(call_run)
        CallEvidenceAttachment.objects.filter(pk=addition.pk).update(
            transcript="See you Friday.", file=addition.file.name
        )
        self.assert_withheld(call_run)
        CallEvidenceAttachment.objects.filter(pk=addition.pk).update(
            transcript=addition.transcript
        )
        PhoneCallEvidence.objects.filter(pk=call.pk).update(
            occurred_at=datetime(2026, 10, 1, tzinfo=UTC)
        )
        self.assert_withheld(call_run)

    def test_unavailable_parent_csv_withholds_both_message_and_vehicle_row_comparisons(
        self,
    ):
        owner = self.users["owner"]
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=owner,
            title="Retained export",
            file=SimpleUploadedFile("export.csv", b"row,source\n1,synthetic\n"),
        )
        message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=owner,
            sender=self.person,
            recipient=self.person,
            title="Imported appointment",
            body="See you Monday.",
        )
        CommunicationContext.objects.create(
            case=self.case,
            message=message,
            source_document=document,
            source_sha256=document.sha256,
            source_row=2,
            source_start=1,
            source_end=4,
            parser_version="test",
            timezone="UTC",
            raw_sender="Sender",
        )
        message_run, _ = self.request_run(
            {"kind": "text-message", "id": str(message.pk)}, 0, len(message.body)
        )
        process_run(message_run.pk)
        batch = VehicleLocationImport.objects.create(
            case=self.case, uploaded_by=owner, document=document, timezone="UTC"
        )
        source = VehicleLocationEvidence.objects.create(
            case=self.case,
            uploaded_by=owner,
            vehicle=self.vehicle,
            title="Reported imported position",
            address="19 Source Road",
            import_batch=batch,
            source_row=2,
            raw_row={"Address": "19 Source Road"},
        )
        self.movement = {"kind": "vehicle-location", "id": str(source.pk)}
        text = self.text()
        location_run, _ = self.request_run({"kind": "text", "id": str(text.pk)}, 0, 8)
        process_run(location_run.pk)
        self.login("viewer")
        for run in (location_run, message_run):
            self.assertEqual(
                self.client.get(self.comparison_urls(run)[2]).status_code, 200
            )
        document.file.storage.delete(document.file.name)
        for run in (location_run, message_run):
            self.assert_withheld(run)
