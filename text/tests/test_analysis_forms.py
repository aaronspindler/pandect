"""Writing sample assumptions remain attached to the selected retained sources."""

from uuid import uuid4

from django.test import TestCase

from core.analysis.forms import analysis_form
from core.analysis.sources import source_id
from core.testing import EvidenceTestMixin


class WritingAnalysisFormTests(EvidenceTestMixin, TestCase):
    def test_sample_context_serialization_and_overlapping_groups(self):
        records = [self.create_evidence() for _ in range(2)]
        choices = [f"text:{record.pk}" for record in records]
        keys = [source_id({"kind": "text", "id": str(record.pk)}) for record in records]
        data = {
            "operation": "style_change",
            "submission_id": uuid4(),
            "sources": choices,
            "group_a": choices[:1],
            "group_b": choices[1:],
            "sample_origin": "translated",
            "sample_language": "English",
            "sample_channel": "Letter",
            "sample_topic": "Delivery",
            "comparison_channel": "Email",
            "comparison_topic": "Collection",
            "exclude_phrases": "Quoted wording",
        }
        form = analysis_form(data, case=self.case, operation="style_change")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.parameters(),
            {
                "group_a": keys[:1],
                "group_b": keys[1:],
                "exclude_phrases": "Quoted wording",
                "contexts": {
                    keys[0]: {
                        "origin": "translated",
                        "language": "English",
                        "channel": "Letter",
                        "topic": "Delivery",
                    },
                    keys[1]: {
                        "origin": "translated",
                        "language": "English",
                        "channel": "Email",
                        "topic": "Collection",
                    },
                },
            },
        )
        invalid = analysis_form(
            data | {"group_b": choices}, case=self.case, operation="style_change"
        )
        self.assertFalse(invalid.is_valid())
        self.assertIn("group_b", invalid.errors)
