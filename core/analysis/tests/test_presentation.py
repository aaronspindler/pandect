"""Readable results preserve source meanings and do not infer identity from UUIDs."""

from copy import deepcopy

from django.template.loader import render_to_string
from django.test import TestCase

from core.analysis.presentation import measurement_formatter
from core.analysis.sources import Source, source_id
from core.testing import EvidenceViewTestMixin
from people.models import Person


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

    def test_unknown_zero_and_false_keep_different_visible_meanings(self):
        content = self.show(
            {"Unknown duration": None, "Zero duration": 0, "Truncated": False}
        )
        self.assertIn("Unknown", content)
        self.assertRegex(content, r">0\s*<")
        self.assertIn("No", content)

    def test_display_bounds_do_not_silently_hide_remaining_values(self):
        content = self.show({"Recorded intervals": list(range(205))})
        self.assertIn("First 200 values shown", content)
        self.assertIn("download retains all values", content)
