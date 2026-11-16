"""Public capability coverage follows the product's supported inventories."""

from django.test import SimpleTestCase

from core.analysis.execution import GEOGRAPHIC as GEOGRAPHIC_OPERATIONS
from core.analysis.execution import requires_inference
from core.analysis.operations import OPERATIONS
from core.analysis.reports import REPORT_NAMES
from core.evidence.form_registry import FORM_CLASSES
from core.evidence.types import CALL_KINDS, FILE_KINDS, KINDS
from marketing.catalogue import (
    ANALYSES,
    EVIDENCE_TYPES,
    GEOGRAPHIC,
    IMPORTS,
    INCLUDED,
    MODEL,
    OPTIONAL_AI,
    REPORTS,
    evidence_groups,
)


class CapabilityCatalogueTests(SimpleTestCase):
    def test_every_addable_evidence_kind_has_one_complete_section(self):
        groups = evidence_groups()
        kinds = [group["kind"] for group in groups]
        self.assertCountEqual(kinds, KINDS)
        self.assertCountEqual(kinds, FORM_CLASSES)
        self.assertEqual(len(kinds), len(set(kinds)))
        self.assertEqual(kinds, [item["kind"] for item in EVIDENCE_TYPES])
        for group in groups:
            with self.subTest(kind=group["kind"]):
                for category, inventory in (
                    ("analyses", ANALYSES),
                    ("reports", REPORTS),
                    ("imports", IMPORTS),
                ):
                    self.assertCountEqual(
                        [row["id"] for row in group[category]],
                        [
                            name
                            for name, row in inventory.items()
                            if group["kind"] in row["kinds"]
                        ],
                    )
                self.assertTrue(group["formats"])
                self.assertTrue(group["intake"])

    def test_analysis_and_import_coverage_matches_registered_operations(self):
        registered_imports = {
            name: row for name, row in IMPORTS.items() if name in OPERATIONS
        }
        self.assertFalse(set(ANALYSES) & set(registered_imports))
        self.assertEqual(set(ANALYSES) | set(registered_imports), set(OPERATIONS))
        for name, operation in OPERATIONS.items():
            with self.subTest(operation=name):
                if name in registered_imports:
                    kinds = registered_imports[name]["source_kinds"]
                else:
                    kinds = ANALYSES[name]["kinds"]
                self.assertCountEqual(kinds, operation.kinds)

    def test_requirements_distinguish_builtin_optional_model_and_geography(self):
        disabled = {"include_summary": False, "semantic_links": False}
        enabled = {"include_summary": True, "semantic_links": True}
        for name, row in ANALYSES.items():
            with self.subTest(operation=name):
                if name in GEOGRAPHIC_OPERATIONS:
                    expected = GEOGRAPHIC
                elif requires_inference(name, disabled):
                    expected = MODEL
                elif requires_inference(name, enabled):
                    expected = OPTIONAL_AI
                else:
                    expected = INCLUDED
                self.assertEqual(row["requirement"], expected)
                self.assertTrue(row["detail"])

    def test_pattern_reports_and_supported_imports_are_covered(self):
        self.assertEqual(set(REPORTS), set(REPORT_NAMES))
        self.assertCountEqual(REPORTS["calls"]["kinds"], CALL_KINDS)
        self.assertCountEqual(
            REPORTS["duplicates"]["kinds"], (*FILE_KINDS, *CALL_KINDS)
        )
        for name in ("vehicle-coverage", "vehicle-speed", "vehicle-trips"):
            self.assertEqual(REPORTS[name]["kinds"], ("vehicle-location",))
        self.assertEqual(
            set(IMPORTS),
            {"conversation_import", "imessage_import", "vehicle_locations"},
        )
        for name in ("conversation_import", "imessage_import"):
            self.assertIn("text-message", IMPORTS[name]["kinds"])
        self.assertEqual(IMPORTS["vehicle_locations"]["kinds"], ("vehicle-location",))
