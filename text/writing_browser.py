"""Exercise attributed writing comparison through the person and result pages."""

import re
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

from playwright.sync_api import expect

from cases.models import Case
from config.tests.browser_base import BrowserTestCase
from core.analysis.worker import process_run
from core.models import AnalysisRun
from people.models import Person
from phone.models import TextMessageEvidence


class PersonWritingBrowserTests(BrowserTestCase):
    artifact_subdirectory = "writing-style-browser"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("writing-investigator")
        self.case = Case.objects.create(
            owner=self.owner, name="Synthetic writing review"
        )
        self.person = Person.objects.create(case=self.case, first_name="Sam Example")
        recipient = Person.objects.create(case=self.case, first_name="Jo Example")
        self.samples = [
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                sender=self.person,
                recipient=recipient,
                title=title,
                occurred_at=datetime(2026, month, 12, 15, tzinfo=UTC),
                body=body,
            )
            for month, title, body in (
                (
                    1,
                    "January field notes",
                    "Hello Jo. I checked the north gate this morning and left the "
                    "green folder under the counter beside a spare key. We can ask "
                    "the caretaker about the closed trail after lunch and walk "
                    "back together before the next rain arrives. Thanks.",
                ),
                (
                    3,
                    "March field notes",
                    "HEY! Found the north entrance. No signs? Checked the west "
                    "path, asked the caretaker, and called dispatch. Bring two "
                    "flashlights, a backup map, the small ladder, and your winter "
                    "gloves! We should hurry before it starts snowing. Meet at noon!",
                ),
            )
        ]

    def check_form_layout(self, page, engine):
        for width, height, label in (
            (1440, 900, "desktop"),
            (1440, 1024, "tall-desktop"),
            (768, 1024, "tablet"),
            (390, 844, "mobile"),
            (320, 844, "reflow"),
        ):
            page.set_viewport_size({"width": width, "height": height})
            self.assertLessEqual(
                page.evaluate("document.documentElement.scrollWidth"), width + 1
            )
            self.capture(page, engine, f"compare-{label}")
        page.evaluate("document.documentElement.style.fontSize = '200%'")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 321)
        page.evaluate("document.documentElement.style.fontSize = ''")
        page.emulate_media(color_scheme="dark", reduced_motion="reduce")
        self.capture(page, engine, "compare-dark")
        page.emulate_media(color_scheme="light", forced_colors="active")
        self.capture(page, engine, "compare-forced-colors")
        page.emulate_media(forced_colors="none", reduced_motion="no-preference")

    def finish_comparison(self, run_id):
        process_run(run_id)
        run = AnalysisRun.objects.get(pk=run_id)
        self.assertEqual(run.status, AnalysisRun.Status.COMPLETE, run.error_code)
        self.assertEqual(
            {source["evidence_id"] for source in run.inputs},
            {str(sample.pk) for sample in self.samples},
        )

    def test_choose_periods_and_read_saved_comparison_without_javascript(self):
        case_id, person_id = self.case.pk, self.person.pk
        person_url = self.url("people:detail", case_id=case_id, person_id=person_id)
        writing_url = self.url(
            "people:writing_style", case_id=case_id, person_id=person_id
        )
        compare_url = f"{writing_url}?mode=compare"
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(person_url)
                page.get_by_role("link", name="Writing style", exact=True).press(
                    "Enter"
                )
                expect(page).to_have_url(writing_url)
                expect(
                    page.get_by_role("heading", name="Writing style", exact=True)
                ).to_be_visible()
                page.get_by_role(
                    "navigation", name="Writing analysis methods"
                ).get_by_role(
                    "link", name="Compare writing over time", exact=True
                ).press("Enter")
                expect(page).to_have_url(compare_url)
                self.check_form_layout(page, engine)

                fields = {
                    name: page.locator(f"input[name='{name}']")
                    for name in (
                        "first_start",
                        "first_end",
                        "second_start",
                        "second_end",
                    )
                }
                fields["first_start"].fill("2026-01-01")
                fields["first_end"].fill("2026-03-01")
                fields["second_start"].fill("2026-03-01")
                fields["second_end"].fill("2026-03-31")
                start = page.get_by_role("button", name="Start analysis", exact=True)
                start.press("Enter")
                expect(page.get_by_role("alert")).to_be_visible()
                expect(fields["first_end"]).to_have_value("2026-03-01")
                fields["first_end"].fill("2026-01-31")
                start.press("Enter")
                expect(
                    page.get_by_role("status").filter(has_text=re.compile(r"^Queued$"))
                ).to_be_visible()
                result_url = page.url
                run_id = UUID(urlsplit(result_url).path.rstrip("/").rsplit("/", 1)[-1])
                self.db(self.finish_comparison, run_id)
                page.get_by_role("link", name="Refresh status", exact=True).press(
                    "Enter"
                )
                expect(
                    page.get_by_role(
                        "heading", name="Compared descriptive writing features."
                    )
                ).to_be_visible()
                expect(
                    page.get_by_text(
                        "Differences do not prove a change of author.", exact=False
                    )
                ).to_be_visible()
                page.set_viewport_size({"width": 390, "height": 844})
                self.capture(page, engine, "comparison-result-mobile")
                page.set_viewport_size({"width": 1440, "height": 900})
                self.capture(page, engine, "comparison-result-desktop")

                page.goto(writing_url)
                saved_run = page.locator(f'a[href="{urlsplit(result_url).path}"]')
                expect(saved_run).to_be_visible()
                saved_run.press("Enter")
                expect(
                    page.get_by_role(
                        "heading", name="Compared descriptive writing features."
                    )
                ).to_be_visible()
