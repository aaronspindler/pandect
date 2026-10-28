"""Synthetic journeys: run explicitly with manage.py test analysis.browser."""

import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.files.base import ContentFile
from django.test import override_settings
from playwright.sync_api import expect

from analysis.models import AnalysisResult, AnalysisReview, AnalysisRun
from analysis.services import process_run, start_run
from cases.models import Case, CaseMembership
from config.tests import browser as core_browser
from evidence.models import (
    DocumentEvidence,
    PersonLocationEvidence,
    PhoneCallEvidence,
    TextEvidence,
    TextMessageEvidence,
)
from people.models import Person


@override_settings(
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
    ACCOUNT_RATE_LIMITS=False,
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
)
class AnalysisBrowserTests(StaticLiveServerTestCase):
    password = "synthetic-browser-correct-horse-742!"
    # Reuse the established browser/thread/database helpers without inheriting
    # the unrelated core test journeys.
    make_user = core_browser.CoreInterfaceBrowserTests.make_user
    url = core_browser.CoreInterfaceBrowserTests.url
    db = core_browser.CoreInterfaceBrowserTests.db
    browser_page = core_browser.CoreInterfaceBrowserTests.browser_page
    sign_in = core_browser.CoreInterfaceBrowserTests.sign_in
    capture = core_browser.CoreInterfaceBrowserTests.capture

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engines = tuple(
            item.strip()
            for item in os.environ.get(
                "PANDECT_BROWSER_ENGINES", "chromium,webkit"
            ).split(",")
            if item.strip()
        )
        if not cls.engines or set(cls.engines) - {"chromium", "webkit"}:
            raise ValueError(
                "Select chromium and/or webkit in PANDECT_BROWSER_ENGINES."
            )

    def setUp(self):
        super().setUp()
        self.playwright = None
        self.browsers = {}
        self.browser_context_number = 0
        self.database_worker = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.database_worker.shutdown)
        self.media_directory = tempfile.TemporaryDirectory(
            prefix="pandect-analysis-browser-"
        )
        self.addCleanup(self.media_directory.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=self.media_directory.name))
        self.artifacts = Path(
            os.environ.get(
                "PANDECT_BROWSER_ARTIFACTS", "data/validation/analysis-browser"
            )
        ).resolve()
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.owner = self.make_user("analysis-browser-owner")
        self.viewer = self.make_user("analysis-browser-viewer")
        self.case = Case.objects.create(
            owner=self.owner, name="Synthetic analysis case"
        )
        CaseMembership.objects.create(case=self.case, user=self.viewer, role="viewer")
        self.document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic retained report",
            file=ContentFile(
                b"Synthetic source text.\n"
                b"Contact source@analysis.test for the proposed meeting.\n"
                b"<iframe>source wording</iframe>\n",
                name="report.txt",
            ),
        )
        self.text = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic captured notes",
            body="A proposed meeting remains unconfirmed. Contact note@analysis.test.",
        )
        person = Person.objects.create(
            case=self.case, first_name="Synthetic participant"
        )
        PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic metadata-only call",
            sender=person,
            recipient=person,
        )

    def _complete(self, run_id):
        process_run(run_id)
        run = AnalysisRun.objects.get(pk=run_id)
        if run.status != "complete":
            raise AssertionError(f"Synthetic extraction failed: {run.error_code}")

    def _revoke_viewer(self):
        CaseMembership.objects.filter(case=self.case, user=self.viewer).delete()

    def _assert_reflow(self, page):
        self.assertLessEqual(
            page.evaluate("document.documentElement.scrollWidth"),
            page.evaluate("document.documentElement.clientWidth") + 1,
        )

    def _review_findings(self):
        run, _ = start_run(
            self.owner,
            self.case.pk,
            "entities",
            [{"kind": "text", "id": str(self.text.pk)}],
        )
        AnalysisRun.objects.filter(pk=run.pk).update(status="complete", progress=100)
        AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data={
                "summary": "Synthetic findings for review recovery",
                "items": [
                    {
                        "label": f"Synthetic finding {index + 1}",
                        "text": "A reported statement requiring investigator review.",
                        "source_ids": [run.inputs[0]["id"]],
                    }
                    for index in range(60)
                ],
            },
        )
        return run.pk

    def test_review_errors_preserve_text_focus_and_later_finding_page(self):
        for engine in self.engines:
            run_id = self._review_findings()
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(self.url("analysis:create", case_id=self.case.pk))
                page.get_by_role("button", name="Queue analysis", exact=True).click()
                expect(page.locator("[data-error-summary]")).to_be_focused()
                page.goto(
                    self.url("analysis:detail", case_id=self.case.pk, run_id=run_id)
                    + "?page=2"
                )
                finding = page.locator("#finding-51")
                finding.locator("summary").filter(
                    has_text=re.compile(r"^Review this finding$")
                ).click()
                reason = finding.get_by_label("Reason", exact=True)
                # Exercise server validation even when a client omits its UI bound.
                reason.evaluate("node => node.removeAttribute('maxlength')")
                entered = "Synthetic review " + "x" * 4000
                reason.fill(entered)
                finding.get_by_role(
                    "button", name="Record finding review", exact=True
                ).click()
                expect(
                    page.get_by_role("heading", name="Review not saved", exact=True)
                ).to_be_visible()
                expect(page.locator("[data-error-summary]")).to_be_focused()
                expect(page.get_by_label("Reason", exact=True)).to_have_value(entered)
                expect(
                    page.get_by_role("link", name="Back to finding 51", exact=True)
                ).to_have_attribute("href", re.compile(r"\?page=2#finding-51$"))
                for width, height, label in (
                    (1440, 900, "desktop"),
                    (390, 844, "mobile"),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    self.capture(page, engine, f"analysis-review-error-{label}")
                page.get_by_label("Reason", exact=True).fill(
                    "Synthetic corrected assessment."
                )
                page.get_by_label("Decision", exact=True).select_option("accepted")
                page.get_by_role("button", name="Record review", exact=True).click()
                expect(page).to_have_url(re.compile(r"\?page=2#finding-51$"))
                expect(
                    page.locator("#finding-51").get_by_text(
                        "Finding review: Accepted", exact=False
                    )
                ).to_be_visible()

    def test_native_extraction_review_source_anchors_and_responsive_layout(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("evidence:list", case_id=self.case.pk))
                page.get_by_role("link", name="Analysis", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Analysis", exact=True)
                ).to_be_visible()
                page.get_by_role("link", name="Start analysis", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Start analysis", exact=True)
                ).to_be_visible()
                source = page.get_by_label(
                    "Document · Synthetic retained report", exact=True
                )
                source.focus()
                expect(source).to_be_focused()
                self.assertNotEqual(
                    source.evaluate("node => getComputedStyle(node).outlineStyle"),
                    "none",
                )
                source.press("Space")
                expect(source).to_be_checked()
                page.get_by_role("button", name="Queue analysis", exact=True).click()
                expect(
                    page.get_by_role(
                        "heading", name="Extract document text", exact=True
                    )
                ).to_be_visible()
                expect(
                    page.get_by_role("status").filter(has_text=re.compile(r"^Queued$"))
                ).to_be_visible()
                run_id = UUID(page.url.rstrip("/").split("/")[-1])
                self.db(self._complete, run_id)
                page.get_by_role("link", name="Refresh status", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Native UTF-8 text", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_text("source@analysis.test", exact=False)
                ).to_be_visible()
                expect(page.locator("iframe")).to_have_count(0)
                expect(
                    page.locator("#finding-1").get_by_role(
                        "link", name="Synthetic retained report", exact=True
                    )
                ).to_have_attribute("href", re.compile(r"/evidence/document/"))
                for width, height, label in (
                    (1440, 900, "desktop"),
                    (1440, 1024, "tall-desktop"),
                    (768, 1024, "tablet"),
                    (390, 844, "mobile"),
                    (320, 844, "reflow"),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    self.capture(page, engine, f"analysis-result-{label}")
                page.emulate_media(color_scheme="dark")
                self.capture(page, engine, "analysis-result-dark")
                page.emulate_media(
                    color_scheme="light",
                    forced_colors="active",
                    reduced_motion="reduce",
                )
                self.capture(page, engine, "analysis-result-forced-colors")
                page.emulate_media(forced_colors="none")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.get_by_label("Decision", exact=True).select_option("accepted")
                global_review = page.get_by_role(
                    "heading", name="Investigator review", exact=True
                ).locator("..")
                global_review.get_by_label("Reason", exact=True).fill(
                    "Synthetic investigator review; source inspected."
                )
                page.get_by_role("button", name="Record review", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Accepted", exact=True)
                ).to_be_visible()
                with page.expect_download() as waiting:
                    page.get_by_role(
                        "link", name="Download result and provenance", exact=True
                    ).click()
                download = waiting.value
                data = json.loads(Path(download.path()).read_text())
                self.assertEqual(data["operation"], "native_text")
                self.assertEqual(data["inputs"][0]["sha256"], self.document.sha256)
                self.assertIn(
                    "source@analysis.test", data["result"]["items"][0]["text"]
                )
                page.locator("#finding-1").get_by_role(
                    "link", name="Synthetic retained report", exact=True
                ).click()
                expect(
                    page.get_by_role(
                        "heading", name="Synthetic retained report", exact=True
                    )
                ).to_be_visible()

    def test_viewer_reports_sources_and_access_removal_without_javascript(self):
        for engine in self.engines:
            # Each browser engine receives fresh viewer membership after the prior
            # engine's revocation has been checked.
            CaseMembership.objects.get_or_create(
                case=self.case, user=self.viewer, defaults={"role": "viewer"}
            )
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(self.url("analysis:index", case_id=self.case.pk))
                expect(
                    page.get_by_role("link", name="Start analysis", exact=True)
                ).to_have_count(0)
                page.get_by_role("link", name="Call patterns", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Recorded activity", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role(
                        "link",
                        name="Phone call · Synthetic metadata-only call",
                        exact=True,
                    )
                ).to_be_visible()
                page.get_by_label("State", exact=True).select_option("all")
                page.get_by_role("button", name="Update report", exact=True).click()
                expect(
                    page.get_by_role(
                        "link",
                        name="Phone call · Synthetic metadata-only call",
                        exact=True,
                    )
                ).to_be_visible()
                self.capture(page, engine, "analysis-viewer-call-report")
                self.db(self._revoke_viewer)
                response = page.reload()
                self.assertEqual(response.status, 404)
                expect(
                    page.get_by_text("Synthetic metadata-only call", exact=True)
                ).to_have_count(0)

    def _phase3_sources(self):
        writer = Person.objects.create(case=self.case, first_name="Synthetic writer")
        recipient = Person.objects.create(
            case=self.case, first_name="Synthetic recipient"
        )
        start = datetime(2026, 10, 1, 12, tzinfo=UTC)
        locations = [
            PersonLocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Synthetic visit sample {index + 1}",
                person=writer,
                latitude=Decimal("45.501700"),
                longitude=Decimal("-73.567300"),
                observed_at=start + timedelta(minutes=minute),
            )
            for index, minute in enumerate((0, 10, 180, 190))
        ]
        excluded = "COPIED SOURCE INTRODUCTION"
        wording = (
            "Hello, our team recorded the proposed harbour delivery before the "
            "scheduled meeting. Please check the package address and confirm "
            "which entrance remains available tomorrow. We also need the full "
            "inspection notes because several separate questions are still "
            "unresolved. Thanks for sending the original documents promptly."
        )
        writing = [
            [
                TextMessageEvidence.objects.create(
                    case=self.case,
                    uploaded_by=self.owner,
                    title=f"Synthetic writing {group} {index + 1}",
                    sender=writer,
                    recipient=recipient,
                    body=excluded
                    + " "
                    + (wording.lower() if group == "A" else wording.upper()),
                    occurred_at=start
                    + timedelta(days=0 if group == "A" else 5, minutes=index * 2),
                )
                for index in range(3)
            ]
            for group in ("A", "B")
        ]
        exchanges = [
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Synthetic exchange {index + 1}",
                sender=recipient if index == 1 else writer,
                recipient=writer if index == 1 else recipient,
                body=body,
                occurred_at=start + timedelta(days=10, minutes=minute),
            )
            for index, (body, minute) in enumerate(
                (
                    ("Can you confirm the separate proposal?", 0),
                    ("That proposal remains under review.", 2),
                    ("A later selected note about the proposal.", 300),
                )
            )
        ]
        originals = [
            (type(record), record.pk, record.sha256, record.original_bytes())
            for record in locations + writing[0] + writing[1] + exchanges
        ]
        return locations, writing, exchanges, excluded, originals

    def _assert_originals_retained(self, originals):
        for model, identifier, checksum, original_bytes in originals:
            current = model.objects.get(pk=identifier)
            self.assertEqual(current.sha256, checksum)
            self.assertEqual(current.original_bytes(), original_bytes)

    def _choose_analysis(self, page, operation):
        page.goto(self.url("analysis:create", case_id=self.case.pk))
        page.get_by_label("Analysis", exact=True).select_option(operation)
        page.get_by_role(
            "button", name="Choose analysis and sources", exact=True
        ).click()
        page.locator("summary").filter(
            has_text=re.compile(r"^Analysis assumptions$")
        ).click()

    def _queue_and_finish(self, page, operation):
        page.get_by_role("button", name="Queue analysis", exact=True).click()
        expect(
            page.get_by_role("status").filter(has_text=re.compile(r"^Queued$"))
        ).to_be_visible()
        run_id = UUID(page.url.rstrip("/").split("/")[-1])
        self.db(self._complete, run_id)
        page.get_by_role("link", name="Refresh status", exact=True).click()
        self.assertEqual(
            self.db(lambda: AnalysisRun.objects.get(pk=run_id).operation), operation
        )
        return run_id

    def _assert_phase3_run(self, run_id, operation, records):
        run = AnalysisRun.objects.select_related("result").get(pk=run_id)
        self.assertEqual(run.operation, operation)
        self.assertEqual(run.status, "complete", run.error_code)
        self.assertFalse(run.external_processing)
        data = run.result.data
        by_evidence = {
            selected["evidence_id"]: selected["id"] for selected in run.inputs
        }
        if operation == "candidate_visits":
            self.assertEqual(data["metrics"]["Candidate visits"], 2)
            self.assertEqual(data["metrics"]["Excluded sampling gaps"], 1)
            self.assertEqual(len(run.inputs), 4)
        elif operation == "style_change":
            for letter, selected in zip(("a", "b"), records, strict=True):
                expected = {by_evidence[str(record.pk)] for record in selected}
                self.assertEqual(set(run.parameters[f"group_{letter}"]), expected)
                coverage = next(
                    item
                    for item in data["items"]
                    if item["label"] == f"Group {letter.upper()} coverage"
                )
                self.assertGreaterEqual(coverage["values"]["words"], 100)
                self.assertEqual(coverage["values"]["sources_usable"], 3)
                self.assertEqual(len(coverage["values"]["excluded_spans"]), 3)
                self.assertTrue(
                    all(
                        run.parameters["contexts"][key]["language"] == "en"
                        for key in expected
                    )
                )
            self.assertIsNotNone(data["metrics"]["possible_change_interval"])
        else:
            self.assertEqual(
                run.parameters["break_before"], [by_evidence[str(records[1].pk)]]
            )
            self.assertEqual(
                run.parameters["join_before"], [by_evidence[str(records[2].pk)]]
            )
            self.assertIn("compare_split_at", run.parameters)
            self.assertEqual(data["metrics"]["inferred_sessions"], 2)
            self.assertEqual(data["metrics"]["response_intervals_seconds"], [298 * 60])
            self.assertTrue(
                any(
                    item["label"] == "At/after selected split" for item in data["items"]
                )
            )

    def _run_identifiers(self, run_id):
        run = AnalysisRun.objects.get(pk=run_id)
        return [
            identifier
            for selected in run.inputs
            for identifier in (selected["id"], selected["evidence_id"])
        ]

    def _assert_readable_findings(self, page, run_id):
        visible = page.locator("body").inner_text()
        for raw_label in ("source_id", "sender_id", "recipient_id", "[{'", "{'source"):
            self.assertNotIn(raw_label, visible)
        for identifier in self.db(self._run_identifiers, run_id):
            self.assertNotIn(identifier, visible)

    def _capture_phase3(self, page, engine, name):
        for width, height, label in ((1440, 900, "desktop"), (390, 844, "mobile")):
            page.set_viewport_size({"width": width, "height": height})
            self._assert_reflow(page)
            page.evaluate("window.scrollTo(0, 0)")
            self.capture(page, engine, f"phase3-{name}-{label}")
            page.locator("#finding-1").screenshot(
                path=str(self.artifacts / f"{engine}-phase3-{name}-finding-{label}.png")
            )
        page.set_viewport_size({"width": 1440, "height": 900})

    def test_phase3_visits_style_and_adjusted_communication_forms(self):
        locations, writing, exchanges, excluded, originals = self._phase3_sources()
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                self._choose_analysis(page, "candidate_visits")
                for record in locations:
                    page.get_by_label(
                        f"Person location · {record.title}", exact=True
                    ).check()
                page.get_by_label("Place grouping distance (metres):", exact=True).fill(
                    "100"
                )
                page.get_by_label(
                    "Observation/session gap (minutes):", exact=True
                ).fill("60")
                page.get_by_label("Minimum observed stop (minutes):", exact=True).fill(
                    "5"
                )
                visits = self._queue_and_finish(page, "candidate_visits")
                self.db(self._assert_phase3_run, visits, "candidate_visits", locations)
                expect(
                    page.get_by_role(
                        "heading", name="Selected observation positions", exact=True
                    )
                ).to_be_visible()
                first = page.locator("#finding-1")
                expect(
                    first.get_by_role(
                        "heading", name="Candidate visit to place 1", exact=True
                    )
                ).to_be_visible()
                first.locator("summary").filter(
                    has_text=re.compile(r"^Review this finding$")
                ).click()
                first.get_by_label("Review this finding", exact=True).select_option(
                    "accepted"
                )
                first.get_by_label("Reason", exact=True).fill(
                    "Synthetic candidate reviewed against both observed samples."
                )
                first.get_by_role(
                    "button", name="Record finding review", exact=True
                ).click()
                expect(
                    page.get_by_role("heading", name="Accepted · Finding 1", exact=True)
                ).to_be_visible()
                self.assertTrue(
                    self.db(
                        lambda run_id=visits: AnalysisReview.objects.filter(
                            result__run_id=run_id, item_index=0, decision="accepted"
                        ).exists()
                    )
                )
                expect(
                    page.locator("#finding-1").get_by_text(
                        "Synthetic writer", exact=True
                    )
                ).to_be_visible()
                self._assert_readable_findings(page, visits)
                self._capture_phase3(page, engine, "visits")

                self._choose_analysis(page, "style_change")
                for record in writing[0] + writing[1]:
                    page.get_by_label(
                        f"Text message · {record.title}", exact=True
                    ).check()
                page.get_by_label("First writing sample:", exact=True).select_option(
                    [f"text-message:{record.pk}" for record in writing[0]]
                )
                page.get_by_label("Second writing sample:", exact=True).select_option(
                    [f"text-message:{record.pk}" for record in writing[1]]
                )
                page.get_by_label(
                    "Quoted or copied wording to exclude:", exact=True
                ).fill(excluded)
                page.get_by_label("Writing sample context:", exact=True).select_option(
                    "original"
                )
                page.get_by_label("Reported language or languages:", exact=True).fill(
                    "en"
                )
                page.get_by_label("Writing channel:", exact=True).fill(
                    "Synthetic message export"
                )
                page.get_by_label("Writing topic:", exact=True).fill("Logistics")
                page.get_by_label("Second sample channel:", exact=True).fill(
                    "Synthetic message export"
                )
                page.get_by_label("Second sample topic:", exact=True).fill(
                    "Formal logistics"
                )
                style = self._queue_and_finish(page, "style_change")
                self.db(self._assert_phase3_run, style, "style_change", writing)
                expect(
                    page.get_by_role(
                        "heading",
                        name="Possible observed-style change interval",
                        exact=True,
                    )
                ).to_be_visible()
                expect(
                    page.get_by_role("heading", name="Group A coverage", exact=True)
                ).to_be_visible()
                self._assert_readable_findings(page, style)
                examples = page.locator("#finding-1 .analysis-example")
                quotes = examples.locator("blockquote").all_text_contents()
                self.assertIn("hello", quotes)
                self.assertIn("H", quotes)
                expected_links = {
                    record.title: self.url(
                        "evidence:detail",
                        case_id=self.case.pk,
                        kind="text-message",
                        evidence_id=record.pk,
                    ).removeprefix(self.live_server_url)
                    for group in writing
                    for record in group
                }
                example_links = examples.get_by_role("link").all()
                self.assertGreaterEqual(len(example_links), 2)
                for link in example_links:
                    title = link.inner_text()
                    self.assertIn(title, expected_links)
                    self.assertEqual(link.get_attribute("href"), expected_links[title])
                self._capture_phase3(page, engine, "style")

                self._choose_analysis(page, "comm_patterns")
                for record in exchanges:
                    page.get_by_label(
                        f"Text message · {record.title}", exact=True
                    ).check()
                page.get_by_label(
                    "Observation/session gap (minutes):", exact=True
                ).fill("120")
                page.get_by_label(
                    "Start an inferred session before:", exact=True
                ).select_option(f"text-message:{exchanges[1].pk}")
                page.get_by_label(
                    "Continue an inferred session before:", exact=True
                ).select_option(f"text-message:{exchanges[2].pk}")
                page.get_by_label(
                    "Compare communications before and after (UTC):", exact=True
                ).fill("2026-10-11T12:01")
                timing = self._queue_and_finish(page, "comm_patterns")
                self.db(self._assert_phase3_run, timing, "comm_patterns", exchanges)
                expect(
                    page.get_by_role("heading", name="Observed session 2", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role(
                        "heading", name="At/after selected split", exact=True
                    )
                ).to_be_visible()
                self._assert_readable_findings(page, timing)
                first_exchange = page.locator("#finding-1")
                expect(
                    first_exchange.get_by_text("Synthetic writer", exact=True)
                ).to_be_visible()
                expect(
                    first_exchange.get_by_text("Synthetic recipient", exact=True)
                ).to_be_visible()
                self._capture_phase3(page, engine, "communications")
                self.db(self._assert_originals_retained, originals)
