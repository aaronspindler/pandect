"""Readable results preserve source meanings and do not infer identity from UUIDs."""

from copy import deepcopy
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from core.analysis.presentation import (
    measurement_formatter,
    participant_names,
    participant_text,
)
from core.analysis.projection import is_ai_suggestion
from core.analysis.sources import Source, source_id
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.models import CommunicationContext, TextMessageEvidence


class FindingProvenanceTests(SimpleTestCase):
    def test_mixed_results_keep_observations_separate_from_model_suggestions(self):
        cases = (
            ("text_topics", {"occurrences": 3}, "Keyword", False),
            ("text_topics", {"status": "unresolved"}, "Topic", True),
            ("conversation_link", {"elapsed_seconds": 30}, "Sequence", False),
            ("conversation_link", {"status": "unresolved"}, "Suggested link", True),
            ("session_summaries", {}, "Unsupported source", False),
            ("session_summaries", {"status": "unresolved"}, "Summary", True),
            ("question_synthesis", {"section": "supported"}, "Observation", True),
            ("entities", {"raw": "a@example.test"}, "Email", False),
            ("transcription", {}, "", False),
            ("transcription", {}, "Candidate transcript", True),
        )
        for operation, values, text, expected in cases:
            with self.subTest(operation=operation, values=values):
                self.assertEqual(
                    is_ai_suggestion(operation, {"values": values, "text": text}),
                    expected,
                )


class AnalysisPresentationTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.note = self.text()
        self.source = Source(
            source_id({"kind": "text", "id": str(self.note.pk)}), "text", self.note
        )

    def show(self, values):
        formatter = measurement_formatter(values, case=self.case, sources=[self.source])
        return render_to_string(
            "core/analysis/measurements.html", {"measurements": formatter(values)}
        )

    def test_writing_examples_are_quoted_and_linked_without_raw_internal_fields(self):
        values = {
            "supporting_examples": [
                {
                    "source_id": self.source.id,
                    "start": 2,
                    "end": 10,
                    "excerpt": "<script>source wording</script>",
                    "source_measurement": 0,
                    "group": "A",
                }
            ],
            "contrary_examples": [],
        }
        before = deepcopy(values)
        content = self.show(values)
        self.assertIn("Supporting examples", content)
        self.assertIn("&lt;script&gt;source wording&lt;/script&gt;", content)
        self.assertNotIn("<script>", content)
        self.assertIn(self.source.url, content)
        self.assertIn("Characters 2–10", content)
        self.assertIn("None observed", content)
        self.assertNotIn(self.source.id, content)
        self.assertNotIn("source_measurement", content)
        self.assertEqual(values, before)

    def test_only_explicit_record_references_resolve_to_people(self):
        values = {
            "sender_id": str(self.person.pk),
            "raw": str(self.person.pk),
            "Subject association": f"person:{self.person.pk}",
        }
        content = self.show(values)
        self.assertIn("Reported sender", content)
        self.assertIn(str(self.person), content)
        self.assertIn(
            str(self.person.pk), content
        )  # The observed raw identifier stays exact.
        self.assertNotIn(f"person:{self.person.pk}", content)

    def test_foreign_record_names_cannot_appear_through_value_presentation(self):
        hidden = Person.objects.create(
            case=self.other_case, first_name="Unrelated secret name"
        )
        content = self.show({"sender_id": str(hidden.pk)})
        self.assertNotIn("Unrelated secret name", content)

    def test_report_filters_and_title_case_reference_fields_show_names(self):
        values = {
            "Participant filter": str(self.person.pk),
            "Other participant filter": str(self.person.pk),
            "Sender ID": str(self.person.pk),
            "Vehicle ID": str(self.vehicle.pk),
            "native_message_id": str(self.person.pk),
        }
        content = self.show(values)
        self.assertIn("Reported sender", content)
        self.assertIn(str(self.person), content)
        self.assertIn(str(self.vehicle), content)
        self.assertNotIn(str(self.vehicle.pk), content)
        # A native message ID is observed source data, even when it is a UUID.
        self.assertEqual(content.count(str(self.person.pk)), 1)

    def test_processing_bookkeeping_is_retained_without_becoming_assumptions(self):
        values = {
            "automatic_batch": str(uuid4()),
            "import_run_id": str(uuid4()),
            "batch_id": str(uuid4()),
            "imported_messages": 42,
        }
        before = deepcopy(values)
        content = self.show(values)
        self.assertIn("Imported messages", content)
        self.assertIn("42", content)
        for key in ("automatic_batch", "import_run_id", "batch_id"):
            self.assertNotIn(values[key], content)
        self.assertEqual(values, before)

    def test_session_threads_show_export_titles_and_preserve_native_thread_ids(self):
        native_thread = str(uuid4())
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Message export <script>",
            file=SimpleUploadedFile("synthetic.csv", b"Synthetic export"),
        )
        message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Message",
            body="Original text",
            sender=self.person,
            recipient=self.person,
        )
        CommunicationContext.objects.create(
            case=self.case,
            message=message,
            source_document=document,
            source_sha256=document.sha256,
            source_row=1,
            source_start=0,
            source_end=1,
            parser_version="synthetic-v1",
            timezone="UTC",
            raw_sender="Synthetic person",
            native_thread_id=native_thread,
        )
        source = Source("message", "text-message", message)
        values = {"native_thread_references": [f"{document.pk}:{native_thread}"]}

        def show():
            formatter = measurement_formatter(values, case=self.case, sources=[source])
            return render_to_string(
                "core/analysis/measurements.html", {"measurements": formatter(values)}
            )

        content = show()
        self.assertIn(
            f"Message export &lt;script&gt; · Thread {native_thread}", content
        )
        self.assertNotIn(str(document.pk), content)
        self.assertNotIn("<script>", content)
        DocumentEvidence.objects.filter(pk=document.pk).update(case=self.other_case)
        content = show()
        self.assertIn(f"Unavailable export · Thread {native_thread}", content)
        self.assertNotIn("Message export", content)
        self.assertEqual(
            values["native_thread_references"], [f"{document.pk}:{native_thread}"]
        )

    def test_participant_display_preserves_arbitrary_and_wrong_role_identifiers(self):
        recipient = Person.objects.create(case=self.case, first_name="Recipient")
        message = TextMessageEvidence(
            case=self.case, sender=self.person, recipient=recipient
        )
        names = participant_names([Source("message", "text-message", message)])
        literal = (
            f"An observed identifier {self.person.pk} "
            f"and reported recipient {self.person.pk}."
        )
        self.assertEqual(
            participant_text(literal, names, operation="content_events"), literal
        )
        self.assertEqual(
            participant_text(
                f"Reported sender *{self.person.pk}*.",
                names,
                operation="conversation_link",
            ),
            f"Reported sender {self.person}.",
        )

    def test_unknown_zero_and_false_keep_different_visible_meanings(self):
        content = self.show(
            {"Unknown duration": None, "Zero duration": 0, "Truncated": False}
        )
        self.assertIn("Unknown", content)
        self.assertRegex(content, r">0\s*<")
        self.assertIn("No", content)

    def test_display_bounds_do_not_silently_hide_remaining_values(self):
        content = self.show({"Recorded intervals": list(range(205))})
        self.assertIn("1–200 of 205", content)
        self.assertIn("Next values", content)
        self.assertIn("=2#values_", content)

    def test_structured_instants_are_localized_without_rewriting_source_values(self):
        values = {
            "Observed at (UTC)": "2026-07-01T00:30:00.123456+00:00",
            "first_recorded_at": "2026-01-01T00:30:00+00:00",
            "Raw": "2026-07-01T00:30:00+00:00",
            "Text": "2026-07-01T00:30:00+00:00",
            "Event time": "2026-02-30T00:30:00+00:00",
            "Last observed": "2026-07-01T00:30:00",  # No timezone was supplied.
            "Coverage": {
                "Window start (inclusive)": "2026-07-01T01:00:00+00:00",
                "Window end (exclusive)": "2026-07-01T02:00:00+00:00",
            },
        }
        before = deepcopy(values)
        with timezone.override("America/Toronto"):
            content = self.show(values)
        self.assertIn("30 Jun 2026, 20:30:00.123456 EDT", content)
        self.assertIn("31 Dec 2025, 19:30:00 EST", content)
        self.assertIn("30 Jun 2026, 21:00:00 EDT", content)
        self.assertIn("30 Jun 2026, 22:00:00 EDT", content)
        self.assertIn('datetime="2026-07-01T00:30:00.123456+00:00"', content)
        self.assertNotIn("Observed at (UTC)", content)
        self.assertIn("2026-07-01T00:30:00+00:00", content)
        self.assertIn("2026-02-30T00:30:00+00:00", content)
        self.assertIn("2026-07-01T00:30:00", content)
        self.assertEqual(values, before)
