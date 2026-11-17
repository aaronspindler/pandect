"""Public capability coverage follows the product's supported inventories."""

from django.test import SimpleTestCase
from django.urls import reverse
from django.utils.html import escape

from core.analysis.execution import GEOGRAPHIC as GEOGRAPHIC_OPERATIONS
from core.analysis.execution import requires_inference
from core.analysis.operations import OPERATIONS
from core.analysis.reports import REPORT_NAMES
from core.evidence.form_registry import FORM_CLASSES
from core.evidence.types import CALL_KINDS, FILE_KINDS, KINDS
from marketing.catalogue import (
    ANALYSES,
    EVIDENCE_FAMILIES,
    EVIDENCE_TYPES,
    GEOGRAPHIC,
    IMPORTS,
    INCLUDED,
    MODEL,
    OPTIONAL_AI,
    REPORTS,
    catalogue_context,
)


class CapabilityCatalogueTests(SimpleTestCase):
    def test_every_addable_evidence_kind_has_one_complete_section(self):
        groups = catalogue_context()["evidence_groups"]
        kinds = [group["kind"] for group in groups]
        self.assertCountEqual(kinds, KINDS)
        self.assertCountEqual(kinds, FORM_CLASSES)
        self.assertEqual(len(kinds), len(set(kinds)))
        self.assertEqual(kinds, [item["kind"] for item in EVIDENCE_TYPES])
        for group in groups:
            with self.subTest(kind=group["kind"]):
                self.assertTrue(group["formats"])
                self.assertTrue(group["intake"])
                self.assertTrue(group["prerequisites"])

    def test_shared_reference_lists_every_capability_once_with_correct_inputs(self):
        sections = catalogue_context()["capability_sections"]
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

    def test_public_catalogue_keeps_methods_and_requirements_without_database(self):
        response = self.client.get(reverse("marketing:evidence"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn("iMessage HTML", html)
        self.assertNotIn("CSV or HTML", html)
        for section in catalogue_context()["capability_sections"]:
            for row in section["rows"]:
                with self.subTest(category=section["id"], method=row["id"]):
                    anchor = f"{section['id']}-{row['id']}"
                    self.assertEqual(html.count(f'id="{anchor}"'), 1)
                    start = html.index(f'id="{anchor}"')
                    end = html.index("</details>", start)
                    method_html = html[start:end]
                    self.assertIn("<summary", method_html)
                    self.assertIn(escape(row["label"]), method_html)
                    self.assertIn(escape(row["description"]), method_html)
                    self.assertIn(escape(row["detail"]), method_html)
                    self.assertIn(escape(row["requirement"]), method_html)
                    self.assertIn(escape(row["short_requirement"]), method_html)
                    for evidence in row["evidence_types"]:
                        self.assertInHTML(
                            f'<a href="#{evidence["kind"]}">{evidence["label"]}</a>',
                            method_html,
                        )

    def test_families_partition_every_evidence_kind(self):
        kinds = [kind for family in EVIDENCE_FAMILIES[1:] for kind in family["kinds"]]
        self.assertCountEqual(kinds, KINDS)
        self.assertEqual(len(kinds), len(set(kinds)))

    def test_family_links_filter_methods_and_preserve_applicable_inputs(self):
        for family in EVIDENCE_FAMILIES:
            with self.subTest(family=family["id"]):
                response = self.client.get(
                    reverse("marketing:evidence"), {"type": family["id"]}
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.context["selected_family"]["id"], family["id"]
                )
                profiles = response.context["evidence_groups"]
                self.assertCountEqual(
                    [item["kind"] for item in profiles], family["kinds"]
                )
                selected = [
                    item["id"]
                    for item in response.context["evidence_families"]
                    if item["selected"]
                ]
                self.assertEqual(selected, [family["id"]])
                sections = {
                    section["id"]: section["rows"]
                    for section in response.context["capability_sections"]
                }
                for category, inventory in (
                    ("analysis", ANALYSES),
                    ("reports", REPORTS),
                    ("imports", IMPORTS),
                ):
                    expected = {
                        name
                        for name, row in inventory.items()
                        if set(row["kinds"]) & set(family["kinds"])
                    }
                    rows = sections.get(category, [])
                    self.assertCountEqual([row["id"] for row in rows], expected)
                    self.assertEqual(category in sections, bool(expected))
                    for row in rows:
                        self.assertCountEqual(
                            [item["kind"] for item in row["evidence_types"]],
                            set(row["kinds"]) & set(family["kinds"]),
                        )
                        for item in row["evidence_types"]:
                            self.assertContains(
                                response, f'id="{item["kind"]}"', count=1
                            )
                    for excluded in set(inventory) - expected:
                        self.assertNotContains(response, f'id="{category}-{excluded}"')

    def test_messages_and_calls_lead_with_communication_methods(self):
        sections = catalogue_context("communications")["capability_sections"]
        self.assertEqual(
            [row["id"] for row in sections[0]["rows"][:4]],
            ["sessions", "comm_patterns", "conversation_link", "session_summaries"],
        )

    def test_unknown_family_falls_back_to_all_without_reflecting_input(self):
        value = '<script>alert("unknown")</script>'
        response = self.client.get(reverse("marketing:evidence"), {"type": value})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_family"]["id"], "")
        self.assertEqual(
            response.context["capability_sections"],
            catalogue_context()["capability_sections"],
        )
        self.assertNotContains(response, value)
        self.assertNotContains(response, escape(value))

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
            {"imessage_import", "vehicle_locations"},
        )
        self.assertIn("text-message", IMPORTS["imessage_import"]["kinds"])
        self.assertEqual(IMPORTS["vehicle_locations"]["kinds"], ("vehicle-location",))
