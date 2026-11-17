"""Synthetic OCR/map journeys for the shared analysis interface."""

import io
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urljoin
from uuid import UUID

from django.core.files.base import ContentFile
from django.test import override_settings
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import expect

from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase
from core.analysis.services import process_run, start_run
from core.models import AnalysisRun
from geo.models import LocationEvidence
from image.models import ImageEvidence


def synthetic_page():
    image = Image.new("RGB", (8064, 6048), "white")
    ImageDraw.Draw(image).text(
        (400, 800),
        "Synthetic printed pickup 07:30",
        font=ImageFont.load_default(size=400),
        fill="black",
    )
    content = io.BytesIO()
    image.save(content, format="JPEG", progressive=True)
    return content.getvalue()


def synthetic_reference():
    ring = [
        {"lat": lat, "lon": lon}
        for lon, lat in (
            (-0.001, -0.001),
            (0.001, -0.001),
            (0.001, 0.001),
            (-0.001, 0.001),
            (-0.001, -0.001),
        )
    ]
    return json.dumps(
        {
            "version": 0.6,
            "osm3s": {"timestamp_osm_base": "2026-10-27T00:00:00Z"},
            "elements": [
                {
                    "type": "node",
                    "id": 1,
                    "lat": 0,
                    "lon": 0,
                    "tags": {"amenity": "cafe", "name": "Synthetic archive cafe"},
                },
                {
                    "type": "way",
                    "id": 2,
                    "tags": {"highway": "residential", "name": "Synthetic lane"},
                    "geometry": [
                        {"lat": 0.0004, "lon": -0.001},
                        {"lat": 0.0004, "lon": 0.001},
                    ],
                },
                {
                    "type": "way",
                    "id": 3,
                    "tags": {"building": "apartments", "name": "Synthetic building"},
                    "geometry": ring,
                },
                {
                    "type": "node",
                    "id": 4,
                    "lat": 0.002,
                    "lon": 0,
                    "tags": {"place": "neighbourhood", "name": "Synthetic quarter"},
                },
            ],
        },
        indent=2,
    ).encode()


@override_settings(
    PANDECT_MAP_CONTEXT_URL="https://maps.example.test/api/interpreter",
    PANDECT_MAP_CONTEXT_NAME="Synthetic reference source",
    PANDECT_MAP_CONTEXT_VERSION="synthetic-reference-1",
    PANDECT_MAP_CONTEXT_ATTRIBUTION="Synthetic fixtures, CC0",
    PANDECT_MAP_CONTEXT_USER_AGENT="Pandect browser fixture",
    PANDECT_MAP_CONTEXT_LOCAL=False,
    PANDECT_MAP_CONTEXT_TIMEOUT=10,
    PANDECT_MAP_CONTEXT_HISTORY_START="2012-01-01",
)
class ContextBrowserTests(BrowserTestCase):
    artifact_subdirectory = "context-browser"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("context-browser-owner")
        self.viewer = self.make_user("context-browser-viewer")
        self.case = Case.objects.create(owner=self.owner, name="Synthetic context case")
        CaseMembership.objects.create(case=self.case, user=self.viewer, role="viewer")
        self.image_bytes = synthetic_page()
        self.image = ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic OCR page",
            file=ContentFile(self.image_bytes, name="synthetic-page.jpeg"),
        )
        self.locations = [
            LocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Synthetic location sample {index + 1}",
                latitude=Decimal("0.000000"),
                longitude=longitude,
                observed_at=datetime(2020, 1, 2, 12, tzinfo=UTC)
                + timedelta(minutes=index),
            )
            for index, longitude in enumerate(
                (Decimal("0.000000"), Decimal("0.000100"))
            )
        ]
        self.map_bytes = synthetic_reference()
        self.map_lookup = self.enterContext(
            patch(
                "geo.analysis.external.geographic_context.ContextClient.lookup",
                return_value=self.map_bytes,
            )
        )
        self.gateway_request = self.enterContext(
            patch("core.analysis.gateway.GatewayClient.request")
        )

    def _complete(self, run_id, operation):
        process_run(run_id)
        run = AnalysisRun.objects.get(pk=run_id)
        self.assertEqual(run.operation, operation)
        self.assertEqual(run.status, "complete", run.error_code)
        self.assertEqual(run.external_processing, operation != "ocr")
        return run.parameters

    def _choose(self, page, operation):
        page.goto(
            self.url("core:analysis:create", case_id=self.case.pk)
            + f"?operation={operation}"
        )
        summary = page.locator(".analysis-assumptions > summary")
        summary.focus()
        expect(summary).to_be_focused()
        summary.press("Space")

    def _approve_and_finish(self, page, operation):
        if operation != "ocr":
            page.get_by_role("button", name="Start analysis", exact=True).click()
            expect(page.locator("[data-error-summary]")).to_be_visible()
            expect(
                page.get_by_text(
                    "Approve external processing for the selected operation.",
                    exact=True,
                )
            ).to_be_visible()
            approval = page.get_by_role(
                "checkbox",
                name="I approve external processing of the selected evidence",
            )
            approval.focus()
            approval.press("Space")
            expect(approval).to_be_checked()
        else:
            expect(
                page.get_by_role(
                    "checkbox",
                    name="I approve external processing of the selected evidence",
                )
            ).to_have_count(0)
        page.get_by_role("button", name="Start analysis", exact=True).click()
        expect(
            page.get_by_role("status").filter(has_text=re.compile(r"^Queued$"))
        ).to_be_visible()
        run_id = UUID(page.url.rstrip("/").split("/")[-1])
        parameters = self.db(self._complete, run_id, operation)
        page.get_by_role("link", name="Refresh status", exact=True).click()
        completion = page.locator(".analysis-run-details").get_by_text(
            re.compile(r"^Complete ·")
        )
        expect(completion).to_be_hidden()
        page.locator("#run-details").press("Enter")
        expect(completion).to_be_visible()
        page.locator("#run-details").press("Enter")
        return run_id, parameters

    def _capture_layouts(self, page, engine, name):
        for width, height, label in (
            (1440, 900, "desktop"),
            (1440, 1024, "tall-desktop"),
            (768, 1024, "tablet"),
            (390, 844, "mobile"),
            (320, 844, "reflow"),
        ):
            page.set_viewport_size({"width": width, "height": height})
            self.assertLessEqual(
                page.evaluate("document.documentElement.scrollWidth"),
                page.evaluate("document.documentElement.clientWidth") + 1,
            )
            page.evaluate("window.scrollTo(0, 0)")
            self.capture(page, engine, f"{name}-{label}")
        page.emulate_media(color_scheme="dark")
        self.capture(page, engine, f"{name}-dark")
        page.emulate_media(
            color_scheme="light", forced_colors="active", reduced_motion="reduce"
        )
        self.capture(page, engine, f"{name}-forced-colors")
        page.emulate_media(forced_colors="none", reduced_motion="no-preference")
        page.set_viewport_size({"width": 1440, "height": 900})

    def test_unrecognized_image_and_nul_terminated_xmp_results(self):
        stream = io.BytesIO()
        xmp = (
            '<x xmlns:claim="https://example.test/claims" '
            'claim:Software="Synthetic camera"/>\x00'
        )
        with (
            Image.new("RGB", (400, 300), "white") as primary,
            Image.new("RGB", (32, 24), "gray") as auxiliary,
        ):
            primary.save(
                stream, format="MPO", append_images=[auxiliary], xmp=xmp.encode()
            )
        source = ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic image without recognized text",
            file=ContentFile(stream.getvalue(), name="synthetic-camera.jpeg"),
        )
        runs = {}
        for operation in ("ocr", "image_metadata"):
            run, _ = start_run(
                self.owner,
                self.case.pk,
                operation,
                [{"kind": "image", "id": str(source.pk)}],
            )
            process_run(run.pk)
            run.refresh_from_db()
            self.assertEqual(run.status, "partial", run.error_code)
            runs[operation] = run.pk
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(
                    self.url(
                        "core:analysis:detail", case_id=self.case.pk, run_id=runs["ocr"]
                    )
                )
                expect(
                    page.get_by_role(
                        "heading",
                        name="No text recognized. Inspect the original images.",
                        exact=True,
                    )
                ).to_be_visible()
                page.get_by_text("Processing details", exact=True).press("Enter")
                summary = page.locator("summary").filter(has_text="Processing summary")
                summary.focus()
                expect(summary).to_be_focused()
                summary.press("Space")
                metric = page.locator(".analysis-measurements > div").filter(
                    has=page.locator("dt", has_text="Recognized text regions")
                )
                expect(metric.locator("dd")).to_have_text("0")
                self._capture_layouts(page, engine, "local-ocr-no-text")
                page.goto(
                    self.url(
                        "core:analysis:detail",
                        case_id=self.case.pk,
                        run_id=runs["image_metadata"],
                    )
                )
                expect(page.get_by_text("Malformed XMP", exact=False)).to_have_count(0)
                page.get_by_role(
                    "link", name=re.compile(r"^Interpreted XMP attributes")
                ).click()
                expect(
                    page.get_by_role("heading", name="Interpreted XMP attributes")
                ).to_be_visible()
                self._capture_layouts(page, engine, "image-nul-xmp")
        self.gateway_request.assert_not_called()

    def test_local_printed_ocr_regions_search_and_private_original(
        self,
    ):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                self._choose(page, "ocr")
                profile = page.get_by_label("Text profile", exact=True)
                expect(profile).to_have_value("printed")
                profile.focus()
                expect(profile).to_be_focused()
                self.assertNotEqual(
                    profile.evaluate("node => getComputedStyle(node).outlineStyle"),
                    "none",
                )
                expect(
                    page.get_by_text(
                        "Processed locally. Handwriting and mixed text "
                        "are unsupported.",
                        exact=False,
                    )
                ).to_be_visible()
                page.get_by_label("Image · Synthetic OCR page", exact=True).check()
                self._capture_layouts(page, engine, "local-ocr-form")
                run_id, parameters = self._approve_and_finish(page, "ocr")
                self.assertEqual(parameters["mode"], "printed")
                finding = page.locator("#finding-1")
                expect(
                    finding.get_by_text("Synthetic printed pickup 07:30", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_text(
                        "OCR can miss text. Compare candidates with the original.",
                        exact=True,
                    )
                ).to_be_visible()
                limits = page.locator("summary").filter(
                    has_text=re.compile(r"^Limitations$")
                )
                expect(page.locator(".analysis-result-limits")).to_have_attribute(
                    "open", ""
                )
                expect(
                    page.get_by_text(
                        "Printed English OCR runs locally; no image is sent "
                        "to an external service.",
                        exact=False,
                    )
                ).to_be_visible()
                limits.press("Enter")
                preview = finding.locator("svg image")
                expect(preview).to_have_count(1)
                preview_url = urljoin(page.url, preview.get_attribute("href"))
                response = page.request.get(preview_url)
                self.assertEqual(response.status, 200)
                self.assertIn("private", response.headers["cache-control"])
                self.assertEqual(response.headers["content-type"], "image/jpeg")
                self._capture_layouts(page, engine, "local-ocr-result")
                page.goto(self.url("core:evidence:list", case_id=self.case.pk))
                search = page.get_by_label("Search evidence", exact=True)
                search.fill("printed pickup")
                search.press("Enter")
                row = page.locator(".evidence-row").filter(
                    has_text="Synthetic OCR page"
                )
                expect(row).to_have_count(1)
                expect(row).to_contain_text("printed pickup")
                row.click()
                expect(
                    page.get_by_role("heading", name="Read text in images", exact=True)
                ).to_be_visible()
                page.locator("#finding-1").get_by_role(
                    "link", name="Synthetic OCR page", exact=True
                ).click()
                expect(
                    page.get_by_role("heading", name="Synthetic OCR page", exact=True)
                ).to_be_visible()
                with page.expect_download() as waiting:
                    page.get_by_role(
                        "link", name="Download original", exact=True
                    ).first.click()
                self.assertEqual(
                    Path(waiting.value.path()).read_bytes(), self.image_bytes
                )
                self.assertEqual(
                    self.db(
                        lambda identifier=run_id: (
                            AnalysisRun.objects.get(pk=identifier).method_version
                        )
                    ),
                    "5",
                )
        self.gateway_request.assert_not_called()
        self.map_lookup.assert_not_called()

    def _revoke_viewer(self):
        CaseMembership.objects.filter(case=self.case, user=self.viewer).delete()

    def test_historical_map_context_snapshots_viewer_access_and_revocation(self):
        for engine in self.engines:
            CaseMembership.objects.get_or_create(
                case=self.case, user=self.viewer, defaults={"role": "viewer"}
            )
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                self._choose(page, "geographic_context")
                for record in self.locations:
                    page.get_by_label(f"Location · {record.title}", exact=True).check()
                page.get_by_label("Place grouping distance (metres)", exact=True).fill(
                    "100"
                )
                page.get_by_label(
                    "Map context search radius (metres)", exact=True
                ).fill("500")
                page.get_by_label(
                    "Boundary ambiguity distance (metres)", exact=True
                ).fill("10")
                page.get_by_label(
                    "Historical map snapshot (UTC, optional)", exact=True
                ).fill("2020-01-02T12:00")
                self._capture_layouts(page, engine, "geographic-context-form")
                run_id, parameters = self._approve_and_finish(
                    page, "geographic_context"
                )
                self.assertEqual(
                    parameters["context_as_of"], "2020-01-02T12:00:00+00:00"
                )
                self.assertEqual(parameters["context_radius_m"], 500)
                references = page.locator("summary").filter(
                    has_text=re.compile(r"^Retained map references$")
                )
                expect(references).to_be_visible()
                expect(
                    page.get_by_role("link", name="Download reference JSON", exact=True)
                ).to_be_hidden()
                self._capture_layouts(page, engine, "geographic-context-result")
                references.focus()
                references.press("Enter")
                for name in (
                    "Synthetic archive cafe",
                    "Synthetic lane",
                    "Synthetic building",
                    "Synthetic quarter",
                ):
                    expect(
                        page.get_by_role("link", name=re.compile(re.escape(name))).first
                    ).to_be_visible()
                for feature, relation in (
                    ("Synthetic building", "Inside retrieved polygon"),
                    ("Synthetic quarter", "Nearby map label; no boundary supplied"),
                ):
                    finding = (
                        page.locator(".analysis-finding")
                        .filter(has=page.get_by_role("link", name=re.compile(feature)))
                        .first
                    )
                    expect(finding.get_by_text(relation, exact=True)).to_be_visible()
                page.get_by_text("Processing details", exact=True).press("Enter")
                page.locator("summary").filter(
                    has_text=re.compile(r"^Processing summary$")
                ).click()
                expect(
                    page.get_by_text("Historical map snapshot", exact=True).first
                ).to_be_visible()
                self.capture(page, engine, "geographic-context-expanded-details")
                reference = page.get_by_role(
                    "link", name="Download reference JSON", exact=True
                )
                reference_url = urljoin(page.url, reference.get_attribute("href"))
                reference.focus()
                expect(reference).to_be_focused()
                with page.expect_download() as waiting:
                    reference.press("Enter")
                artifact_id = reference_url.rstrip("/").split("/")[-1]
                self.assertEqual(
                    waiting.value.suggested_filename, f"reference-{artifact_id}.json"
                )
                self.assertEqual(
                    Path(waiting.value.path()).read_bytes(), self.map_bytes
                )
                result_url = page.url
            with (
                self.subTest(engine=engine, role="viewer"),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(result_url)
                expect(
                    page.locator("summary").filter(
                        has_text=re.compile(r"^Retained map references$")
                    )
                ).to_be_visible()
                expect(
                    page.get_by_role("button", name="Record review", exact=True)
                ).to_have_count(0)
                response = page.request.get(reference_url)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.body(), self.map_bytes)
                self.assertIn("private", response.headers["cache-control"])
                self.assertTrue(
                    response.headers["content-disposition"].startswith("attachment;")
                )
                self.db(self._revoke_viewer)
                self.assertEqual(page.request.get(reference_url).status, 404)
                self.assertEqual(page.reload().status, 404)
                expect(
                    page.get_by_text("Synthetic archive cafe", exact=True)
                ).to_have_count(0)
        self.gateway_request.assert_not_called()
        self.assertEqual(self.map_lookup.call_count, len(self.engines))
        for call in self.map_lookup.call_args_list:
            self.assertIn('[date:"2020-01-02T12:00:00Z"]', call.args[0])
            self.assertNotIn("is_in", call.args[0])
