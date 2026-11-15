"""Synthetic shared-selection, portable-brief, and cited-synthesis journeys."""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from playwright.sync_api import expect

from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase
from core.analysis.selections import finding_member
from core.analysis.services import process_run, start_run
from core.models import AnalysisRun
from text.analysis.synthesis import finding_text
from text.models import TextEvidence


class SelectionBrowserTests(BrowserTestCase):
    artifact_subdirectory = "selection-browser"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("selection-browser-owner")
        self.viewer = self.make_user("selection-browser-viewer")
        self.case = Case.objects.create(
            owner=self.owner, name="Synthetic meeting review"
        )
        CaseMembership.objects.create(case=self.case, user=self.viewer)
        self.source = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Retained cancellation message",
            body="The meeting is cancelled. Contact person@example.test. "
            "<script>untrusted source</script>",
        )
        run, _ = start_run(
            self.owner,
            self.case.pk,
            "entities",
            [{"kind": "text", "id": str(self.source.pk)}],
        )
        process_run(run.pk)
        self.prior_result = run.result
        self.finding = finding_member(self.case, self.prior_result.pk, 0)

    def _finish_synthesis(self, run_id):
        run = AnalysisRun.objects.get(pk=run_id)
        quote = "person@example.test"
        surface = finding_text(self.prior_result.data["items"][0])
        result = {
            "items": [
                {
                    "section": "contrary",
                    "text": "The selected text reports cancellation.",
                    "citations": [
                        {
                            "record_id": "source:"
                            + run.parameters["text_source_ids"][0],
                            "quote": "The meeting is cancelled.",
                            "start": 0,
                        }
                    ],
                },
                {
                    "section": "supported",
                    "text": "A prior finding extracted a possible email contact.",
                    "citations": [
                        {
                            "record_id": f"finding:{self.prior_result.pk}:0",
                            "quote": quote,
                            "start": surface.index(quote),
                        }
                    ],
                },
            ],
            "unknowns": [
                "Whether any meeting occurred is not established by this selection."
            ],
        }
        with patch(
            "core.analysis.local.LocalClient",
            return_value=SimpleNamespace(infer=Mock(return_value=result), usage={}),
        ):
            process_run(run_id)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        return str(run.result.pk)

    def _reflow(self, page):
        self.assertLessEqual(
            page.evaluate("document.documentElement.scrollWidth"),
            page.evaluate("document.documentElement.clientWidth") + 1,
        )

    def _captures(self, page, engine, name):
        for width, height, label in (
            (1440, 900, "desktop"),
            (1440, 1024, "tall-desktop"),
            (768, 1024, "tablet"),
            (390, 844, "mobile"),
            (320, 844, "reflow"),
        ):
            page.set_viewport_size({"width": width, "height": height})
            self._reflow(page)
            self.capture(page, engine, f"{name}-{label}")
        page.evaluate("document.documentElement.style.fontSize='200%'")
        self._reflow(page)
        self.capture(page, engine, f"{name}-large-text")
        page.evaluate("document.documentElement.style.fontSize=''")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.emulate_media(color_scheme="dark", reduced_motion="reduce")
        self.capture(page, engine, f"{name}-dark")
        page.emulate_media(color_scheme="light", forced_colors="active")
        self.capture(page, engine, f"{name}-forced-colors")
        page.emulate_media(forced_colors="none")

    def test_shared_selection_brief_and_synthesis_without_javascript(self):
        for engine in self.engines:
            name = f"Meeting review {engine}"
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(
                    self.url(
                        "core:evidence:detail",
                        case_id=self.case.pk,
                        kind="text",
                        evidence_id=self.source.pk,
                    )
                )
                panel = self.open_evidence_panel(page, "more")
                panel.get_by_role("link", name="Save to selection", exact=True).press(
                    "Enter"
                )
                page.get_by_role("link", name="Create selection", exact=True).press(
                    "Enter"
                )
                page.get_by_label("Selection name", exact=True).fill(name)
                button = page.get_by_role("button", name="Create selection", exact=True)
                button.focus()
                expect(button).to_be_focused()
                button.press("Enter")
                expect(
                    page.get_by_role("heading", name=f"Add to {name}", exact=True)
                ).to_be_visible()
                page.get_by_role("button", name="Add to selection", exact=True).press(
                    "Enter"
                )
                expect(
                    page.get_by_role("heading", name=name, exact=True)
                ).to_be_visible()
                selection_id = UUID(page.url.rstrip("/").split("/")[-1])
                page.goto(
                    self.url("core:analysis:selection_pick", case_id=self.case.pk)
                    + f"?result={self.prior_result.pk}&item=0"
                )
                page.get_by_role("link", name=name, exact=True).press("Enter")
                page.get_by_role("button", name="Add to selection", exact=True).press(
                    "Enter"
                )
                expect(
                    page.get_by_text("2 members · Revision 3.", exact=False)
                ).to_be_visible()
                expect(
                    page.get_by_text("<script>untrusted source</script>", exact=False)
                ).to_be_visible()
                for remove in page.get_by_role("link", name="Remove member").all():
                    self.assertGreaterEqual(remove.bounding_box()["width"], 75)
                self._captures(page, engine, "selection")
                ask = page.get_by_role("link", name="Ask a question", exact=True)
                ask.focus()
                expect(ask).to_be_focused()
                ask.press("Enter")
                page.get_by_label("Question", exact=True).fill(
                    "Was the meeting held, and what contact was extracted?"
                )
                self._captures(page, engine, "synthesis-form")
                page.get_by_role("button", name="Start synthesis", exact=True).press(
                    "Enter"
                )
                expect(
                    page.get_by_role("status").filter(has_text=re.compile(r"^Queued$"))
                ).to_be_visible()
                run_id = UUID(page.url.rstrip("/").split("/")[-1])
                synthesis_result_id = self.db(self._finish_synthesis, run_id)
                page.get_by_role("link", name="Refresh status", exact=True).press(
                    "Enter"
                )
                expect(
                    page.get_by_text(
                        "The selected text reports cancellation.", exact=True
                    )
                ).to_be_visible()
                expect(
                    page.get_by_text("Quoted prior findings", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_text(
                        "Whether any meeting occurred is not established "
                        "by this selection.",
                        exact=True,
                    )
                ).to_be_visible()
                self._captures(page, engine, "synthesis-result")
                page.goto(
                    self.url("core:analysis:selection_pick", case_id=self.case.pk)
                    + f"?result={synthesis_result_id}&item=1"
                )
                page.get_by_role("link", name=name, exact=True).press("Enter")
                page.get_by_role("button", name="Add to selection", exact=True).press(
                    "Enter"
                )
                expect(
                    page.get_by_text("3 members · Revision 4.", exact=False)
                ).to_be_visible()

            with self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                page.goto(
                    self.url(
                        "core:analysis:selection_detail",
                        case_id=self.case.pk,
                        selection_id=selection_id,
                    )
                )
                expect(
                    page.get_by_role("link", name="Add sources", exact=True)
                ).to_have_count(0)
                expect(
                    page.get_by_role("link", name="Ask a question", exact=True)
                ).to_have_count(0)
                with page.expect_download() as download:
                    page.get_by_role("link", name="Download brief", exact=True).press(
                        "Enter"
                    )
                brief_path = self.artifacts / f"{engine}-selection-brief.html"
                download.value.save_as(brief_path)
                page.goto(brief_path.as_uri())
                expect(
                    page.get_by_role("heading", name=name, exact=True)
                ).to_be_visible()
                expected = page.locator("#provenance-manifest").text_content()
                manifest = json.loads(expected)
                self.assertEqual(manifest["selection"]["id"], str(selection_id))
                self.assertEqual(len(manifest["members"]), 3)
                self.assertEqual(
                    manifest["members"][2]["finding_citations"][0]["result_id"],
                    str(self.prior_result.pk),
                )
                expect(
                    page.get_by_role(
                        "heading", name="Quoted prior findings", exact=True
                    )
                ).to_be_visible()
                self._captures(page, engine, "selection-brief")
                page.set_viewport_size({"width": 320, "height": 844})
                page.get_by_role(
                    "heading", name="Quoted prior findings", exact=True
                ).scroll_into_view_if_needed()
                self.capture(page, engine, "selection-brief-prior-quote-reflow")
                with page.expect_download() as download:
                    page.get_by_role(
                        "button", name="Download provenance manifest", exact=True
                    ).press("Enter")
                downloaded = Path(download.value.path()).read_bytes()
                self.assertEqual(downloaded, expected.encode())
