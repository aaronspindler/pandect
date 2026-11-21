from django.template.loader import render_to_string
from django.test import SimpleTestCase

from core.analysis.operations import OPERATION_GROUPS, OPERATIONS
from core.analysis.result_views import RESULT_VIEWS, result_view


class ResultViewsTests(SimpleTestCase):
    def test_domain_groups_keep_the_ranked_reading_order(self):
        for operation in (
            "text_topics",
            "question_synthesis",
            "comm_patterns",
            "transcription",
            "image_metadata",
            "native_text",
        ):
            with self.subTest(operation=operation):
                rows = [
                    {
                        "index": index,
                        "number": index + 1,
                        "url": "?finding=1",
                        "label": label,
                        "text": "Retained text",
                        "values": values,
                        "measurements": [],
                        "sources": [{"id": source}],
                        "start_seconds": index,
                        "plate_score": None,
                    }
                    for index, label, source, values in (
                        (2, "Raw metadata · Date", "first", {"section": "contrary"}),
                        (0, "Selected message", "second", {"occurrences": 1}),
                        (1, "Summary", "first", {"section": "supported"}),
                    )
                ]
                groups, template = result_view(operation, rows, lambda values: [])
                rendered = render_to_string(
                    template, {"run": {"operation": operation}, "result_groups": groups}
                )
                markers = [
                    rendered.index(f'id="finding-{row["number"]}"') for row in rows
                ]
                self.assertEqual(markers, sorted(markers))

    def test_registered_operations_keep_a_readable_legacy_fallback(self):
        # Retired HTML imports remain readable. Sighting groups have a dedicated reader.
        self.assertEqual(
            (OPERATIONS.keys() - {"sighting_group"}) | {"conversation_import"},
            RESULT_VIEWS.keys(),
        )
        setup_operations = [
            operation for group in OPERATION_GROUPS.values() for operation in group
        ]
        # CSV imports and sighting groups have dedicated selection flows.
        self.assertCountEqual(
            OPERATIONS.keys() - {"imessage_import", "sighting_group"}, setup_operations
        )
        for operation in RESULT_VIEWS:
            with self.subTest(operation=operation):
                items = [
                    {
                        "index": 0,
                        "number": 1,
                        "url": "?finding=1#finding-1",
                        "label": "Retained finding",
                        "text": "Older source-linked output",
                        "values": {},
                        "measurements": [],
                        "sources": [],
                        "plate_score": None,
                    }
                ]
                groups, template = result_view(operation, items, lambda values: [])
                result = render_to_string(
                    template,
                    {"run": {"operation": operation}, "result_groups": groups},
                )
                self.assertIn("Older source-linked output", result)
                self.assertIn('id="finding-1"', result)
