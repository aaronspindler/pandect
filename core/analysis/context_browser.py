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
from core.analysis.services import start_run
from core.analysis.source_selection import snapshot_sources
from core.analysis.testing import external_grant_kwargs
from core.analysis.tests.test_openai_gateway import OPENAI, openai_reply
from core.analysis.worker import process_run
from core.case_search.tests.fixtures import indexed
from core.models import AnalysisResult, AnalysisRun
from document.models import DocumentEvidence
from geo.models import LocationEvidence
from image.models import ImageEvidence
from image.testing import printed_layout


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
    PANDECT_AI_GATEWAY=OPENAI,
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
        self.assertTrue(run.external_processing)
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

    def _start_and_finish(self, page, operation):
        expect(page.get_by_label("Processing location", exact=True)).to_have_count(0)
        expect(page.locator('input[name="external_processing"]')).to_have_count(0)
        page.get_by_role("button", name="Start analysis", exact=True).click()
        page.wait_for_url(re.compile(r"/analysis/[0-9a-f-]{36}/$"))
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
        self.gateway_request.return_value = openai_reply({"text": "", "partial": True})
        runs = {}
        for operation in ("ocr", "image_metadata"):
            run, _ = start_run(
                self.owner,
                self.case.pk,
                operation,
                [{"kind": "image", "id": str(source.pk)}],
                **(
                    external_grant_kwargs(self.case, operation)
                    if operation == "ocr"
                    else {}
                ),
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
                        "heading", name="Image · candidate text", exact=True
                    )
                ).to_be_visible()
                expect(page.locator("#finding-1")).to_contain_text(
                    "Uncertain or unreadable"
                )
                page.get_by_text("Analysis run details", exact=True).press("Enter")
                summary = page.locator("summary").filter(has_text="Processing summary")
                summary.focus()
                expect(summary).to_be_focused()
                summary.press("Space")
                metric = page.locator(".analysis-measurements > div").filter(
                    has=page.locator("dt", has_text="Images read")
                )
                expect(metric.locator("dd")).to_have_text("1")
                self._capture_layouts(page, engine, "hosted-ocr-no-text")
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
        self.gateway_request.assert_called_once()

    def test_hosted_ocr_search_and_private_original(
        self,
    ):
        self.gateway_request.return_value = openai_reply(
            {"text": "Synthetic printed pickup 07:30", "partial": False}
        )
        for engine in self.engines:
            self.db(external_grant_kwargs, self.case, "ocr")
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
                        "The approved vision model reads prepared images "
                        "through Cloudflare.",
                        exact=False,
                    )
                ).to_be_visible()
                page.get_by_label("Image · Synthetic OCR page", exact=True).check()
                self._capture_layouts(page, engine, "hosted-ocr-form")
                run_id, parameters = self._start_and_finish(page, "ocr")
                self.assertEqual(parameters["mode"], "printed")
                finding = page.locator("#finding-1")
                expect(
                    finding.get_by_text("Synthetic printed pickup 07:30", exact=True)
                ).to_be_visible()
                expect(finding).to_contain_text(
                    "Compare candidate text with the original"
                )
                preview = finding.locator("svg image")
                expect(preview).to_have_count(1)
                preview_url = urljoin(page.url, preview.get_attribute("href"))
                response = page.request.get(preview_url)
                self.assertEqual(response.status, 200)
                self.assertIn("private", response.headers["cache-control"])
                self.assertEqual(response.headers["content-type"], "image/jpeg")
                self._capture_layouts(page, engine, "hosted-ocr-result")
                from core.models import ExtractedPassage

                passages = self.db(
                    list,
                    ExtractedPassage.objects.filter(
                        case=self.case, result__run_id=run_id
                    ).values_list("pk", flat=True),
                )
                self.db(
                    self.enterContext,
                    indexed(self.case, [("passage", "", key) for key in passages]),
                )
                page.goto(self.url("core:evidence:list", case_id=self.case.pk))
                search = page.get_by_label("Filter evidence", exact=True)
                search.fill("printed pickup")
                search.press("Enter")
                row = page.locator(".evidence-row").filter(
                    has_text="Synthetic OCR page"
                )
                expect(row).to_have_count(1)
                expect(row).to_contain_text("printed pickup")
                expect(row).to_have_attribute("href", re.compile(r"/evidence/image/"))
                page.get_by_role(
                    "link", name="Open matching passage for Synthetic OCR page"
                ).click()
                expect(
                    page.get_by_role(
                        "heading", name="Synthetic printed pickup 07:30", exact=True
                    )
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
                    "7",
                )
        self.assertEqual(self.gateway_request.call_count, len(self.engines))
        self.map_lookup.assert_not_called()

    def test_historical_document_layout_with_omitted_preview(self):
        # A retained v6 result records its own regions, words and reading orders.
        form, _ = printed_layout("form")
        long_value = "LongReferenceWindowLongReferenceWindow"
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic scanned form",
            file=ContentFile(form, name="scanned-form.png"),
        )
        columns, _ = printed_layout("columns")
        image = ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic columns",
            file=ContentFile(columns, name="columns.png"),
        )
        inputs = snapshot_sources(
            self.case,
            [
                {"kind": "document", "id": str(document.pk)},
                {"kind": "image", "id": str(image.pk)},
            ],
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="ocr",
            method_version="6",
            inputs=inputs,
            status="partial",
        )
        items = []
        layouts = {}
        for source, fields in zip(
            inputs,
            (
                ["River Hall", long_value],
                ["Morning", "East", "Evening", "West"],
            ),
            strict=True,
        ):
            words, candidates = [], []
            for field in fields:
                refs = []
                for token in field.split():
                    refs.append(len(words))
                    words.append(
                        {
                            "item": len(items),
                            "start": 0,
                            "end": len(token),
                            "region": [0.1, 0.1, 0.5, 0.2],
                            "confidence": 90,
                        }
                    )
                    items.append(
                        {
                            "label": "OCR finding",
                            "text": token,
                            "source_ids": [source["id"]],
                            "anchors": [{"source_id": source["id"]}],
                            "values": {},
                        }
                    )
                candidates.append(
                    {
                        "kind": "form_field",
                        "fields": [{"role": "Text", "words": refs, "confidence": 90}],
                        "notes": [],
                    }
                )
            orders = []
            if source["kind"] == "image":
                orders = [
                    {"label": "Across rows", "candidates": [0, 1, 2, 3]},
                    {"label": "Down columns", "candidates": [0, 2, 1, 3]},
                ]
            layouts[source["id"]] = {
                "words": words,
                "candidates": candidates,
                "orders": orders,
                "notes": [],
            }
        AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data={
                "summary": "Saved text",
                "outcome": "partial",
                "items": items,
                "visible_structure": layouts,
            },
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(
                    self.url(
                        "core:evidence:detail",
                        case_id=self.case.pk,
                        kind="document",
                        evidence_id=document.pk,
                    )
                )
                page.get_by_role("link", name="Visible structure").press("Enter")
                layout = page.get_by_role("region", name="Visible structure")
                expect(layout.get_by_role("link", name=document.title)).to_be_visible()
                field = layout.locator("dd").filter(has_text="River Hall")
                expect(field.locator(".layout-reading")).to_have_text("River Hall")
                field.locator("summary").press("Enter")
                expect(
                    field.get_by_text("The prepared preview was omitted.", exact=False)
                ).to_be_visible()
                expect(
                    field.get_by_role("link", name=re.compile(r"River · OCR finding"))
                ).to_be_visible()
                expect(
                    field.get_by_role("link", name=re.compile(r"Hall · OCR finding"))
                ).to_be_visible()
                expect(layout.locator("svg")).to_have_count(0)
                self.capture(page, engine, "visible-structure-form-omitted-preview")
                page.set_viewport_size({"width": 320, "height": 844})
                reading = layout.get_by_text(long_value, exact=True)
                expect(reading).to_be_visible()
                self.assertLessEqual(
                    reading.evaluate("node => node.scrollWidth"),
                    reading.evaluate("node => node.clientWidth") + 1,
                )
                reading.scroll_into_view_if_needed()
                self.capture(page, engine, "visible-structure-long-field-reflow")
                other_source = layout.get_by_role(
                    "navigation", name="Layout sources"
                ).get_by_role("link")
                other_source.press("Enter")
                expect(layout.get_by_role("link", name=image.title)).to_be_visible()
                readings = layout.locator(".layout-reading")
                expect(readings).to_have_text(["Morning", "East", "Evening", "West"])
                layout.get_by_role("link", name="Down columns").press("Enter")
                expect(readings).to_have_text(["Morning", "Evening", "East", "West"])
                self.capture(page, engine, "visible-structure-alternative-order")
        self.gateway_request.assert_not_called()

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
                self.db(external_grant_kwargs, self.case, "geographic_context")
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
                _, parameters = self._start_and_finish(page, "geographic_context")
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
                    finding.get_by_text("Details", exact=True).press("Enter")
                    expect(
                        finding.get_by_text("Historical map snapshot", exact=True)
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
                with self.browser_page(engine, java_script_enabled=False) as (
                    _,
                    viewer_page,
                ):
                    self.sign_in(viewer_page, self.viewer)
                    viewer_page.goto(page.url)
                    expect(
                        viewer_page.locator("summary").filter(
                            has_text=re.compile(r"^Retained map references$")
                        )
                    ).to_be_visible()
                    expect(
                        viewer_page.get_by_role(
                            "button", name="Record review", exact=True
                        )
                    ).to_have_count(0)
                    response = viewer_page.request.get(reference_url)
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.body(), self.map_bytes)
                    self.assertIn("private", response.headers["cache-control"])
                    self.assertTrue(
                        response.headers["content-disposition"].startswith(
                            "attachment;"
                        )
                    )
                    self.db(self._revoke_viewer)
                    self.assertEqual(viewer_page.request.get(reference_url).status, 404)
                    self.assertEqual(viewer_page.reload().status, 404)
                    expect(
                        viewer_page.get_by_text("Synthetic archive cafe", exact=True)
                    ).to_have_count(0)
        self.gateway_request.assert_not_called()
        self.assertEqual(self.map_lookup.call_count, len(self.engines))
        for call in self.map_lookup.call_args_list:
            self.assertIn('[date:"2020-01-02T12:00:00Z"]', call.args[0])
            self.assertNotIn("is_in", call.args[0])
