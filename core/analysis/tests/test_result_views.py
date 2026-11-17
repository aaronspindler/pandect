from django.template.loader import render_to_string
from django.test import SimpleTestCase

from core.analysis.operations import OPERATION_GROUPS, OPERATIONS
from core.analysis.result_views import RESULT_VIEWS, result_view


class ResultViewsTests(SimpleTestCase):
    def test_registered_operations_keep_a_readable_legacy_fallback(self):
        # HTML imports are retired; their retained results remain readable.
        self.assertEqual(
            OPERATIONS.keys() | {"conversation_import"}, RESULT_VIEWS.keys()
        )
        setup_operations = [
            operation for group in OPERATION_GROUPS.values() for operation in group
        ]
        # CSV imports have a dedicated preparation/confirmation flow.
        self.assertCountEqual(OPERATIONS.keys() - {"imessage_import"}, setup_operations)
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
