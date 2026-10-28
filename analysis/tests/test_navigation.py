"""Exact extraction versions, source ranges, review indices and native links."""

import json
from unittest.mock import patch
from uuid import UUID

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from analysis.communication_models import CommunicationContext
from analysis.models import AnalysisResult, AnalysisReview, AnalysisRun
from analysis.services import process_run, resolve_inputs, start_run
from analysis.sources import output, source_id
from evidence.models import (
    CallEvidenceAttachment,
    DocumentEvidence,
    ImageEvidence,
    LocationEvidence,
    PhoneCallEvidence,
    TextEvidence,
    TextMessageEvidence,
)
from evidence.tests.test_views import EvidenceViewTestMixin
from people.models import Person

from .test_documents import pdf_bytes
from .test_media import wav_bytes
from .test_result_integrations import image_bytes


class AnalysisNavigationTests(EvidenceViewTestMixin, TestCase):
    def detail_url(self, run):
        return reverse(
            "analysis:detail", kwargs={"case_id": self.case.pk, "run_id": run.pk}
        )

    def start(self, operation, item, kind, *, extra=None, external=False):
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            operation,
            [{"kind": kind, "id": str(item.pk), **(extra or {})}],
            external_processing=external,
        )
        return run

    def process(self, run):
        process_run(str(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        return run.result

    def test_two_pdf_pages_remain_linked_to_the_exact_selected_extraction_version(self):
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Two retained pages",
            file=SimpleUploadedFile(
                "two-pages.pdf", pdf_bytes("first@example.test", "second@example.test")
            ),
        )
        first = self.start("native_text", document, "document")
        first_result = self.process(first)
        newest = self.start("native_text", document, "document")
        self.process(newest)
        choice = {
            "kind": "document",
            "id": str(document.pk),
            "result_id": str(first_result.pk),
        }
        derived = self.start(
            "entities", document, "document", extra={"result_id": str(first_result.pk)}
        )
        resolved = resolve_inputs(derived, self.case)[0]
        self.assertEqual(resolved.id, source_id(choice))
        self.assertEqual(UUID(resolved.id).version, 5)
        self.assertEqual(resolved.url, self.detail_url(first) + "?page=1#finding-1")
        self.assertNotIn(str(newest.pk), resolved.url)
        self.assertEqual(
            resolved.original_url, self.url("detail", document, kind="document")
        )
        start = resolved.text.index("second@example.test")
        anchor = resolved.anchor(start=start, end=start + len("second@example.test"))
        self.assertEqual(anchor["derived_result_id"], str(first_result.pk))
        self.assertEqual(
            anchor["extraction_ranges"],
            [
                {
                    "finding_index": 1,
                    "start": 0,
                    "end": len("second@example.test"),
                    "original_anchors": first_result.data["items"][1]["anchors"],
                }
            ],
        )
        self.assertEqual(
            anchor["extraction_ranges"][0]["original_anchors"][0]["page"], 2
        )
        across_pages = resolved.anchor(start=0, end=len(resolved.text))[
            "extraction_ranges"
        ]
        self.assertEqual([part["finding_index"] for part in across_pages], [0, 1])
        self.assertEqual(
            [part["original_anchors"][0]["page"] for part in across_pages], [1, 2]
        )
        self.process(derived)
        self.login("viewer")
        response = self.client.get(self.detail_url(derived))
        second_email = next(
            item
            for item in response.context["items"]
            if item["text"] == "second@example.test"
        )
        source = second_email["sources"][0]
        self.assertEqual(source["url"], self.detail_url(first) + "?page=1#finding-2")
        self.assertTrue(any(value.get("page") == 2 for value in source["anchors"]))
        self.assertContains(response, "Exact selected extraction version")
        self.assertContains(response, "Page 2")
        original = self.client.get(resolved.original_url)
        self.assertEqual(original.status_code, 200)
        selected_version = self.client.get(source["url"])
        self.assertContains(selected_version, 'id="finding-2"')
        self.assertContains(selected_version, "Page 2")

    def test_ocr_regions_survive_entity_extraction_and_point_to_the_correct_finding(
        self,
    ):
        image = ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Two printed regions",
            file=SimpleUploadedFile("regions.png", image_bytes()),
        )
        extraction = self.start("ocr", image, "image", external=True)
        reply = {
            "response": json.dumps(
                {
                    "status": "complete",
                    "language": "en",
                    "rotation_degrees": 0,
                    "regions": [
                        {
                            "text": text,
                            "bbox": box,
                            "order": index,
                            "confidence": "clear",
                            "alternatives": [],
                        }
                        for index, (text, box) in enumerate(
                            [
                                ("first@example.test", [0.1, 0.1, 0.7, 0.3]),
                                ("second@example.test", [0.1, 0.5, 0.8, 0.8]),
                            ]
                        )
                    ],
                }
            )
        }
        with patch("analysis.gateway.GatewayClient.request", return_value=reply):
            result = self.process(extraction)
        derived = self.start(
            "entities", image, "image", extra={"result_id": str(result.pk)}
        )
        resolved = resolve_inputs(derived, self.case)[0]
        offset = resolved.text.index("second@example.test")
        selected = resolved.anchor(
            start=offset, end=offset + len("second@example.test")
        )["extraction_ranges"]
        self.assertEqual(selected[0]["finding_index"], 1)
        self.assertEqual(
            selected[0]["original_anchors"][0]["region"], [0.1, 0.5, 0.8, 0.8]
        )
        self.process(derived)
        self.login("viewer")
        response = self.client.get(self.detail_url(derived))
        second = next(
            item
            for item in response.context["items"]
            if item["text"] == "second@example.test"
        )
        self.assertEqual(
            second["sources"][0]["url"],
            self.detail_url(extraction) + "?page=1#finding-2",
        )
        self.assertContains(response, "Region")
        self.assertEqual(resolved.original_url, self.url("detail", image, kind="image"))

    def test_call_transcript_version_preserves_recording_intervals_and_addition(self):
        call = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Retained call",
            sender=self.person,
            recipient=self.person,
        )
        addition = CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            phone_call=call,
            transcript="Separately supplied transcript",
            file=SimpleUploadedFile("call.wav", wav_bytes()),
        )
        extraction = self.start(
            "transcription",
            call,
            "phone-call",
            extra={"attachment_id": str(addition.pk)},
            external=True,
        )
        reply = {
            "text": "first@example.test second@example.test",
            "segments": [
                {"start": 0, "end": 0.5, "text": "first@example.test"},
                {"start": 0.5, "end": 1, "text": "second@example.test"},
            ],
            "transcription_info": {"language": "en"},
        }
        with patch("analysis.gateway.GatewayClient.request", return_value=reply):
            result = self.process(extraction)
        derived = self.start(
            "entities",
            call,
            "phone-call",
            extra={"attachment_id": str(addition.pk), "result_id": str(result.pk)},
        )
        resolved = resolve_inputs(derived, self.case)[0]
        self.assertEqual(
            resolved.original_url,
            self.url("detail", call, kind="phone-call") + f"#attachment-{addition.pk}",
        )
        self.assertNotIn("Separately supplied", resolved.text)
        offset = resolved.text.index("second@example.test")
        anchors = resolved.anchor(
            start=offset, end=offset + len("second@example.test")
        )["extraction_ranges"][0]["original_anchors"]
        self.assertEqual(
            anchors,
            [
                {
                    "source_id": extraction.inputs[0]["id"],
                    "start_seconds": 0.5,
                    "end_seconds": 1,
                }
            ],
        )
        self.process(derived)
        self.login("viewer")
        response = self.client.get(self.detail_url(derived))
        second = next(
            item
            for item in response.context["items"]
            if item["text"] == "second@example.test"
        )
        self.assertEqual(
            second["sources"][0]["url"],
            self.detail_url(extraction) + "?page=1#finding-2",
        )
        self.assertContains(response, "0.5–1.0 seconds")
        addition.refresh_from_db()
        self.assertEqual(addition.transcript, "Separately supplied transcript")

    def findings_fixture(self, count=60):
        note = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Review selection",
            body="A retained description of candidate topics and place matches.",
        )
        run = self.start("text_topics", note, "text")
        source = run.inputs[0]["id"]
        data = output(
            "Synthetic candidates for individual review",
            items=[
                {
                    "label": f"Candidate {index + 1}",
                    "text": f"Topic/place candidate {index + 1}",
                    "source_ids": [source],
                    "anchors": [
                        {"source_id": source, "start": 0, "end": len(note.body)}
                    ],
                    "values": {"Basis": "Synthetic candidate selection"},
                }
                for index in range(count)
            ],
        )
        AnalysisRun.objects.filter(pk=run.pk).update(status="complete", progress=100)
        run.refresh_from_db()
        result = AnalysisResult.objects.create(case=self.case, run=run, data=data)
        return run, result

    def test_selected_source_link_opens_its_first_finding_on_a_later_result_page(self):
        documents = [
            DocumentEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=title,
                file=SimpleUploadedFile("pages.pdf", content),
            )
            for title, content in (
                ("Fifty earlier pages", pdf_bytes(*(["Earlier native page"] * 50))),
                ("Selected later source", pdf_bytes("later@example.test")),
            )
        ]
        prior, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "native_text",
            [{"kind": "document", "id": str(document.pk)} for document in documents],
        )
        result = self.process(prior)
        selected = self.start(
            "entities",
            documents[1],
            "document",
            extra={"result_id": str(result.pk)},
        )
        resolved = resolve_inputs(selected, self.case)[0]
        expected = self.detail_url(prior) + "?page=2#finding-51"
        self.assertEqual(resolved.url, expected)
        self.assertEqual(resolved.segments[0]["finding_index"], 50)
        self.process(selected)
        self.login("viewer")
        extraction = self.client.get(resolved.url)
        self.assertContains(extraction, 'id="finding-51"')
        self.assertContains(extraction, "later@example.test")
        downstream = self.client.get(self.detail_url(selected))
        self.assertEqual(downstream.context["items"][0]["sources"][0]["url"], expected)

    def test_equivalent_address_candidates_can_receive_different_item_reviews(self):
        observations = [
            LocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=f"Address observation {index}",
                address=address,
            )
            for index, address in enumerate(
                ("1 Example Rd", "1 example road", "2 Example St", "2 example street"),
                start=1,
            )
        ]
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "normalize_addresses",
            [{"kind": "location", "id": str(item.pk)} for item in observations],
        )
        result = self.process(run)
        candidates = [
            index
            for index, item in enumerate(result.data["items"])
            if item["label"] == "Candidate equivalent wording"
        ]
        self.assertEqual(len(candidates), 2)
        self.login("editor")
        endpoint = reverse(
            "analysis:review",
            kwargs={"case_id": self.case.pk, "run_id": run.pk},
        )
        for index, decision in zip(candidates, ("accepted", "rejected"), strict=True):
            response = self.client.post(
                endpoint,
                {
                    "item_index": index,
                    "decision": decision,
                    "comment": f"Independent address candidate {index}",
                },
            )
            self.assertEqual(response.status_code, 302)
        self.assertEqual(
            set(result.reviews.values_list("item_index", "decision")),
            {(candidates[0], "accepted"), (candidates[1], "rejected")},
        )
        self.assertFalse(result.reviews.filter(item_index__isnull=True).exists())
        unchanged = list(LocationEvidence.objects.values_list("address", flat=True))
        self.assertCountEqual(unchanged, [item.address for item in observations])

    def test_findings_pagination_keeps_global_indices_and_independent_reviews(self):
        run, result = self.findings_fixture()
        self.login("editor")
        first = self.client.get(self.detail_url(run))
        second = self.client.get(self.detail_url(run), {"page": 2})
        self.assertEqual(len(first.context["items"]), 50)
        self.assertEqual(len(second.context["items"]), 10)
        self.assertEqual(
            (second.context["items"][0]["number"], second.context["items"][0]["index"]),
            (51, 50),
        )
        self.assertContains(second, 'id="finding-51"')
        self.assertNotContains(second, 'id="finding-1"')
        self.assertContains(second, 'name="item_index" value="50"')
        endpoint = reverse(
            "analysis:review", kwargs={"case_id": self.case.pk, "run_id": run.pk}
        )
        for index, decision in ((50, "accepted"), (0, "rejected")):
            response = self.client.post(
                endpoint,
                {
                    "item_index": index,
                    "decision": decision,
                    "comment": f"Individual finding {index + 1}",
                },
            )
            self.assertRedirects(
                response,
                self.detail_url(run) + f"?page={index // 50 + 1}#finding-{index + 1}",
            )
        self.assertEqual(
            set(result.reviews.values_list("item_index", "decision")),
            {(50, "accepted"), (0, "rejected")},
        )
        self.assertFalse(result.reviews.filter(item_index__isnull=True).exists())
        rendered = self.client.get(self.detail_url(run), {"page": 2})
        self.assertContains(rendered, "Accepted · Finding 51")
        self.assertContains(rendered, "Rejected · Finding 1")
        result.refresh_from_db()
        self.assertEqual(result.data["items"][50]["label"], "Candidate 51")

    def test_invalid_individual_index_does_not_turn_into_a_whole_result_review(self):
        run, result = self.findings_fixture(2)
        self.login("editor")
        endpoint = reverse(
            "analysis:review", kwargs={"case_id": self.case.pk, "run_id": run.pk}
        )
        for invalid in ("-1", "not-an-index", "2", "", "999999999"):
            with self.subTest(invalid=invalid):
                before = AnalysisReview.objects.filter(result=result).count()
                response = self.client.post(
                    endpoint, {"item_index": invalid, "decision": "accepted"}
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    AnalysisReview.objects.filter(result=result).count(), before
                )

    def test_invalid_finding_review_keeps_its_index_and_exact_return_link(self):
        run, result = self.findings_fixture()
        self.login("editor")
        endpoint = reverse(
            "analysis:review", kwargs={"case_id": self.case.pk, "run_id": run.pk}
        )
        comment = "An investigator's unsubmitted assessment."
        response = self.client.post(
            endpoint, {"item_index": "50", "decision": "invalid", "comment": comment}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context["form"]["comment"].value(), comment)
        self.assertEqual(response.context["form"]["item_index"].value(), "50")
        self.assertEqual(
            response.context["return_url"], self.detail_url(run) + "?page=2#finding-51"
        )
        self.assertFalse(result.reviews.exists())

    def export(self, *, case=None):
        case = case or self.case
        return DocumentEvidence.objects.create(
            case=case,
            uploaded_by=case.owner,
            title="Synthetic export",
            file=SimpleUploadedFile(
                "messages.html",
                b"Synthetic retained export rows and native identifiers.",
            ),
        )

    def native_message(
        self, document, row, *, native_id="", thread="", reply="", person=None
    ):
        person = person or self.person
        message = TextMessageEvidence.objects.create(
            case=document.case,
            uploaded_by=document.uploaded_by,
            title=f"Native source row {row}",
            sender=person,
            recipient=person,
            body=f"Synthetic retained message row {row}.",
        )
        context = CommunicationContext.objects.create(
            case=document.case,
            message=message,
            source_document=document,
            source_sha256=document.sha256,
            source_row=row,
            source_start=row,
            source_end=row + 1,
            parser_version="synthetic.v1",
            timezone="UTC",
            raw_sender="Synthetic source account",
            native_message_id=native_id,
            native_thread_id=thread,
            reply_to_id=reply,
        )
        return message, context

    def test_native_reply_and_thread_candidates_are_scoped_to_case_and_export(self):
        document = self.export()
        target, first = self.native_message(
            document, 1, native_id="m1", thread="thread"
        )
        reply, middle = self.native_message(
            document, 2, native_id="m2", thread="thread", reply="m1"
        )
        duplicate, last = self.native_message(
            document, 3, native_id="m1", thread="thread"
        )
        unrelated, _ = self.native_message(
            self.export(), 1, native_id="m1", thread="thread"
        )
        private_person = Person.objects.create(
            case=self.other_case, first_name="Private"
        )
        private, _ = self.native_message(
            self.export(case=self.other_case),
            1,
            native_id="m1",
            thread="thread",
            person=private_person,
        )
        self.login("viewer")
        response = self.client.get(self.url("detail", reply, kind="text-message"))
        self.assertEqual(
            {x.pk for x in response.context["native_reply_candidates"]},
            {first.pk, last.pk},
        )
        self.assertEqual(
            [x.pk for x in response.context["native_thread_messages"]],
            [first.pk, middle.pk, last.pk],
        )
        fragment = (
            response.content.decode()
            .split("<summary>Imported source context</summary>", 1)[1]
            .split("</details>", 1)[0]
        )
        self.assertIn(self.url("detail", target, kind="text-message"), fragment)
        self.assertIn(self.url("detail", duplicate, kind="text-message"), fragment)
        self.assertNotIn(str(unrelated.pk), fragment)
        self.assertNotIn(str(private.pk), fragment)
        self.assertContains(response, "Multiple matching native IDs remain candidates")
        self.assertContains(response, "no target is chosen automatically")
        self.assertEqual(
            CommunicationContext.objects.filter(
                source_document=document, native_message_id="m1"
            ).count(),
            2,
        )

    def test_missing_native_relationships_remain_explicitly_unresolved(self):
        document = self.export()
        missing, _ = self.native_message(document, 1, native_id="m2", reply="missing")
        blank, _ = self.native_message(document, 2)
        self.login("viewer")
        response = self.client.get(self.url("detail", missing, kind="text-message"))
        self.assertEqual(list(response.context["native_reply_candidates"]), [])
        self.assertContains(
            response, "Unresolved: no matching native ID in this retained export."
        )
        blank_response = self.client.get(self.url("detail", blank, kind="text-message"))
        self.assertEqual(list(blank_response.context["native_reply_candidates"]), [])
        self.assertEqual(list(blank_response.context["native_thread_messages"]), [])
        self.assertContains(blank_response, "Native message ID</dt><dd>Not supplied")
        self.assertContains(blank_response, "Native thread ID</dt><dd>Not supplied")
