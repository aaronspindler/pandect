"""Public capability coverage follows the product's supported inventories."""

from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase
from django.utils.html import escape

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
    capability_sections,
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

    def test_shared_reference_lists_every_capability_once_with_correct_inputs(self):
        sections = capability_sections()
        self.assertEqual(
            [section["id"] for section in sections], ["analysis", "reports", "imports"]
        )
        for section, inventory in zip(
            sections, (ANALYSES, REPORTS, IMPORTS), strict=True
        ):
            self.assertEqual([row["id"] for row in section["rows"]], list(inventory))
            for row in section["rows"]:
                with self.subTest(category=section["id"], method=row["id"]):
                    self.assertCountEqual(
                        [evidence["kind"] for evidence in row["evidence_types"]],
                        inventory[row["id"]]["kinds"],
                    )
                    for evidence in row["evidence_types"]:
                        self.assertIn(
                            evidence,
                            [
                                {"kind": item["kind"], "label": item["label"]}
                                for item in EVIDENCE_TYPES
                            ],
                        )

    def test_type_profiles_link_to_one_complete_description_per_capability(self):
        request = RequestFactory().get("/evidence/")
        request.user = AnonymousUser()
        html = render_to_string(
            "marketing/evidence.html",
            {
                "evidence_groups": evidence_groups(),
                "capability_sections": capability_sections(),
            },
            request=request,
        )
        for section in capability_sections():
            for row in section["rows"]:
                with self.subTest(category=section["id"], method=row["id"]):
                    anchor = f"{section['id']}-{row['id']}"
                    self.assertEqual(html.count(f'id="{anchor}"'), 1)
                    self.assertEqual(html.count(f'href="#{anchor}"'), len(row["kinds"]))
                    start = html.index(f'<tr role="row" id="{anchor}">')
                    end = html.index("</tr>", start)
                    method_html = html[start:end]
                    self.assertIn(escape(row["description"]), method_html)
                    self.assertIn(escape(row["detail"]), method_html)
                    self.assertIn(escape(row["requirement"]), method_html)
                    for evidence in row["evidence_types"]:
                        self.assertInHTML(
                            f'<a href="#{evidence["kind"]}">{evidence["label"]}</a>',
                            method_html,
                        )

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
