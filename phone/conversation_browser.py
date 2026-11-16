"""Conversation inspection and exact evidence navigation with native links."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from django.core.files.base import ContentFile
from django.urls import reverse
from playwright.sync_api import expect

from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase
from document.models import DocumentEvidence
from people.models import Person
from phone.models import CommunicationContext, TextMessageEvidence


class ConversationBrowserTests(BrowserTestCase):
    artifact_subdirectory = "conversations-browser"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("conversation-browser-owner")
        self.viewer = self.make_user("conversation-browser-viewer")
        self.case = Case.objects.create(
            owner=self.owner, name="Synthetic correspondence"
        )
        CaseMembership.objects.create(case=self.case, user=self.viewer, role="viewer")
        self.alex = Person.objects.create(case=self.case, first_name="Alex")
        self.blair = Person.objects.create(case=self.case, first_name="Blair")
        self.export = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic message export",
            file=ContentFile(b"Synthetic source rows", name="synthetic.csv"),
        )
        self.messages = []
        instant = datetime(2026, 11, 10, 12, tzinfo=UTC)
        for number in range(54):
            sender, recipient = (
                (self.alex, self.blair) if number % 2 == 0 else (self.blair, self.alex)
            )
            message = TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                sender=sender,
                recipient=recipient,
                occurred_at=instant + timedelta(minutes=number),
                title=f"Synthetic message {number + 1:03}",
                body=(
                    f"Original message {number + 1:03}: Shall we meet by the harbour?\n"
                    "Keep this line and its spacing."
                ),
            )
            CommunicationContext.objects.create(
                case=self.case,
                message=message,
                source_document=self.export,
                source_sha256=self.export.sha256,
                source_row=number + 1,
                source_start=number,
                source_end=number + 1,
                parser_version="synthetic-v1",
                timezone="America/Toronto",
                raw_sender=sender.first_name,
                native_message_id=f"synthetic-{number + 1}",
                source_context={"raw_fields": {"Status": "Read"}},
            )
            self.messages.append(message)
        self.manual_message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            sender=self.alex,
            recipient=self.blair,
            title="Individually retained message",
            body="Unbroken identifier: " + "long-value-" * 45,
        )
        first, second = sorted([self.alex.pk, self.blair.pk])
        self.reader_path = reverse(
            "phone:conversation_detail",
            kwargs={
                "case_id": self.case.pk,
                "first_person_id": first,
                "second_person_id": second,
            },
        )

    def test_no_javascript_inline_details_filter_and_exact_evidence_roundtrip(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(self.url("core:analysis:index", case_id=self.case.pk))
                page.get_by_role("link", name="Conversations", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Conversations", exact=True)
                ).to_be_visible()
                page.locator(f'a[href="{self.reader_path}"]').click()
                expect(page.locator("[data-message-details][open]")).to_have_count(0)
                for message in self.messages[:2]:
                    self.assert_inline_details_do_not_navigate(page, message)
                page.locator('select[name="source"]').select_option(str(self.export.pk))
                page.locator(
                    'form:has(select[name="source"]) button[type="submit"]'
                ).click()
                expect(page).to_have_url(
                    self.live_server_url
                    + self.reader_path
                    + f"?source={self.export.pk}"
                )
                page.get_by_role("link", name="Later", exact=True).click()
                target = self.messages[51]
                target_row = page.locator(f"#message-{target.pk}")
                target_details = page.locator(f"#message-details-{target.pk}")
                target_summary = target_details.locator(":scope > summary")
                target_summary.click()
                expect(target_details).to_have_attribute("open", "")
                expect(target_details).to_contain_text("synthetic-52")
                target_row.get_by_role(
                    "link", name="Open evidence", exact=False
                ).click()
                expect(
                    page.get_by_role("heading", name=target.title, exact=True)
                ).to_be_visible()
                reverse_link = page.get_by_role(
                    "link", name="View in conversation", exact=True
                )
                reverse_link.focus()
                reverse_url = reverse_link.get_attribute("href")
                page.keyboard.press("Enter")
                expect(page).to_have_url(self.live_server_url + reverse_url)
                params = parse_qs(urlsplit(page.url).query)
                self.assertEqual(params["source"], [str(self.export.pk)])
                self.assertEqual(params["message"], [str(target.pk)])
                expect(target_row).to_be_in_viewport()
                expect(target_details).to_have_attribute("open", "")
                expect(
                    target_row.get_by_role("link", name="Open evidence", exact=False)
                ).to_have_attribute(
                    "href",
                    self.evidence_path(target)
                    + f"?conversation_source={self.export.pk}",
                )
                expect(page.locator("[data-message-details][open]")).to_have_count(1)
                self.capture(page, engine, "no-javascript-exact-message")
                page.set_viewport_size({"width": 390, "height": 844})
                target_summary.focus()
                page.keyboard.press("Enter")
                expect(target_details).not_to_have_attribute("open", "")
                expect(target_summary).to_be_focused()
                page.keyboard.press("Enter")
                expect(target_details).to_have_attribute("open", "")
                expect(target_summary).to_be_focused()
                self.assert_reflow(page, 390)
                self.capture(page, engine, "mobile-inline-message-details")

    def assert_inline_details_do_not_navigate(self, page, message):
        page.wait_for_load_state("networkidle")
        details = page.locator(f"#message-details-{message.pk}")
        summary = details.locator(":scope > summary")
        requests = []
        navigations = []

        def record_request(request):
            requests.append(request.url)

        def record_navigation(frame):
            if frame == page.main_frame:
                navigations.append(frame.url)

        page.on("request", record_request)
        page.on("framenavigated", record_navigation)
        page.evaluate("window.inlineConversationDocument = document")
        original_url = page.url
        try:
            expect(details).not_to_have_attribute("open", "")
            summary.focus()
            position = summary.bounding_box()["y"]
            page.keyboard.press("Enter")
            expect(details).to_have_attribute("open", "")
            expect(summary).to_be_focused()
            self.assertAlmostEqual(summary.bounding_box()["y"], position, delta=2)
            expect(details).to_contain_text(
                f"synthetic-{self.messages.index(message) + 1}"
            )
            page.keyboard.press("Enter")
            expect(details).not_to_have_attribute("open", "")
            expect(summary).to_be_focused()
            expect(page).to_have_url(original_url)
            self.assertTrue(
                page.evaluate("window.inlineConversationDocument === document")
            )
            self.assertEqual(requests, [], "Inspecting a message issued a request")
            self.assertEqual(navigations, [], "Inspecting a message navigated")
        finally:
            page.remove_listener("request", record_request)
            page.remove_listener("framenavigated", record_navigation)

    def evidence_path(self, message):
        return reverse(
            "core:evidence:detail",
            kwargs={
                "case_id": self.case.pk,
                "kind": "text-message",
                "evidence_id": message.pk,
            },
        )

    def test_reader_inline_details_reflow_themes_and_keyboard_focus(self):
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                page.goto(self.live_server_url + self.reader_path)
                for message in self.messages[:2]:
                    self.assert_inline_details_do_not_navigate(page, message)
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assert_reflow(page, width)
                    self.capture(page, engine, f"reader-{width}-{height}")
                first_details = page.locator(f"#message-details-{self.messages[0].pk}")
                first_details.locator(":scope > summary").click()
                expect(first_details).to_have_attribute("open", "")
                self.assert_reflow(page, 320)
                self.capture(page, engine, "inline-details-320")
                page.goto(self.live_server_url + self.reader_path + "?page=2")
                manual_details = page.locator(
                    f"#message-details-{self.manual_message.pk}"
                )
                manual_details.locator(":scope > summary").click()
                expect(manual_details).to_have_attribute("open", "")
                self.assert_reflow(page, 320)
                self.capture(page, engine, "unknown-time-long-identifier-320")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.goto(
                    self.live_server_url
                    + self.reader_path
                    + f"?message={self.messages[0].pk}#message-{self.messages[0].pk}"
                )
                first_summary = first_details.locator(":scope > summary")
                expect(first_details).to_have_attribute("open", "")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "reader-dark-reduced-motion")
                page.emulate_media(forced_colors="active")
                first_summary.focus()
                expect(first_summary).to_be_focused()
                self.capture(page, engine, "reader-forced-colors")
                page.emulate_media(color_scheme="light", forced_colors="none")
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self.assert_reflow(page, 1440)
                self.capture(page, engine, "reader-text-200-percent")
                page.evaluate("document.documentElement.style.fontSize = ''")
                page.set_viewport_size({"width": 360, "height": 225})
                self.assert_reflow(page, 360)
                self.capture(page, engine, "reader-400-percent-reflow")

    def assert_reflow(self, page, width):
        self.assertLessEqual(
            page.evaluate("document.documentElement.scrollWidth"), width + 1
        )
