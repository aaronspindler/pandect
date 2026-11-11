from django.template.loader import render_to_string
from django.test import SimpleTestCase

from core.analysis.operations import OPERATION_GROUPS, OPERATIONS
from core.analysis.result_views import RESULT_PRESENTERS, RESULT_TEMPLATES


class ResultViewsTests(SimpleTestCase):
    def test_registered_operations_keep_a_readable_legacy_fallback(self):
        self.assertEqual(OPERATIONS.keys(), RESULT_PRESENTERS.keys())
        setup_operations = [
            operation for group in OPERATION_GROUPS.values() for operation in group
        ]
        self.assertCountEqual(OPERATIONS, setup_operations)
        for operation, present in RESULT_PRESENTERS.items():
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
                result = render_to_string(
                    RESULT_TEMPLATES.get(operation, "core/analysis/result_groups.html"),
                    {
                        "run": {"operation": operation},
                        "result_groups": present(operation, items, lambda values: []),
                    },
                )
                self.assertIn("Older source-linked output", result)
                self.assertIn('id="finding-1"', result)
