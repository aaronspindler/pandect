"""Follow a case evidence reader through claim selection and back."""

from urllib.parse import urlencode

from django.test import override_settings
from playwright.sync_api import expect

from cases.models import Case
from config.tests.browser_base import BrowserTestCase
from core.analysis.tests.test_execution import LOCAL
from people.models import Person
from phone.models import TextMessageEvidence
from text.models import TextEvidence


@override_settings(PANDECT_LOCAL_AI=LOCAL)
class ClaimComparisonBrowserTests(BrowserTestCase):
    artifact_subdirectory = "claim-comparison-browser"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("claim-investigator")
        self.case = Case.objects.create(owner=self.owner, name="Synthetic claim review")
        person = Person.objects.create(
            case=self.case, first_name="Synthetic participant"
        )
        self.quote = "Reservation R41 was cancelled."
        self.message = TextMessageEvidence.objects.create(
            case=self.case,
            sender=person,
            recipient=person,
            uploaded_by=self.owner,
            body=self.quote,
            title="Synthetic reservation message",
        )
        self.notice = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            body="The notice reports cancellation for reservation R41.",
            title="Synthetic cancellation notice",
        )

    def test_keyboard_return_to_filtered_evidence_without_javascript(self):
        query = urlencode({"q": "reservation", "type": "text-message", "page": 2})
        reader = (
            self.url(
                "core:evidence:detail",
                case_id=self.case.pk,
                kind="text-message",
                evidence_id=self.message.pk,
            )
            + f"?{query}"
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(reader)
                page.locator(".evidence-analyze > summary").press("Enter")
                page.get_by_role("link", name="Choose analysis", exact=True).press(
                    "Enter"
                )
                page.get_by_role("link", name="Compare a claim", exact=True).press(
                    "Enter"
                )
                claim = page.get_by_role("checkbox", name=self.message.title)
                notice = page.get_by_role("checkbox", name=self.notice.title)
                expect(claim).to_be_checked()
                notice.press("Space")
                choose = page.get_by_role("button", name="Choose claim", exact=True)
                choose.press("Enter")
                quote = page.get_by_label("Exact quotation", exact=True)
                compare = page.get_by_role("button", name="Compare claim", exact=True)
                for width, height in ((1440, 900), (768, 1024), (390, 844), (320, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"),
                        page.evaluate("document.documentElement.clientWidth") + 1,
                    )
                page.set_viewport_size({"width": 390, "height": 844})
                expect(
                    page.get_by_role("link", name="Cancel", exact=True)
                ).to_have_attribute(
                    "href",
                    reader.removeprefix(self.live_server_url) + "#evidence-reader",
                )
                quote.fill("This wording is absent.")
                compare.press("Enter")
                expect(page.get_by_role("alert")).to_contain_text(
                    "Copy the exact wording"
                )
                expect(quote).to_have_value("This wording is absent.")
                self.capture(page, engine, "quote-recovery-390")
                compare.scroll_into_view_if_needed()
                self.capture(page, engine, "quote-actions-390")
                page.get_by_role("button", name="Back to evidence selection").press(
                    "Enter"
                )
                expect(claim).to_be_checked()
                expect(notice).to_be_checked()
                choose.press("Enter")
                quote.fill(self.quote)
                compare.press("Enter")
                back = page.get_by_role("link", name="Back to evidence", exact=True)
                expect(back).to_have_attribute(
                    "href",
                    reader.removeprefix(self.live_server_url) + "#evidence-reader",
                )
                back.press("Enter")
                expect(page).to_have_url(reader + "#evidence-reader")
                expect(page.get_by_role("heading", name=self.quote)).to_be_visible()
