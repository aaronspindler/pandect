"""Conversation inspection and exact evidence navigation with native links."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from django.core.files.base import ContentFile
from django.urls import reverse
from playwright.sync_api import expect

from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase
from core.analysis.catalogue import project_result
from core.analysis.operations import get_operation
from core.analysis.services import snapshot_sources
from core.models import AnalysisResult, AnalysisReview, AnalysisRun
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
                page.get_by_role("link", name="Later messages", exact=True).click()
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
                expect(page).to_have_url(
                    self.live_server_url
                    + self.evidence_path(target)
                    + f"?conversation_source={self.export.pk}"
                )
                reader = page.locator("#evidence-reader")
                expect(
                    reader.get_by_role(
                        "heading",
                        level=1,
                        name=re.compile(
                            "^" + re.escape(target.body.split(":", 1)[0] + ":")
                        ),
                    )
                ).to_be_visible()
                original = reader.locator(".original-text")
                expect(original).to_be_visible()
                self.assertEqual(original.text_content(), target.body)
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

    def test_scrolling_appends_filtered_messages_without_navigation(self):
        instant = self.messages[-1].occurred_at + timedelta(days=1)
        for number in range(55, 105):
            message = TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                sender=self.alex,
                recipient=self.blair,
                occurred_at=instant + timedelta(minutes=number),
                title=f"Synthetic message {number:03}",
                body=f"Original message {number:03} on the following day.",
            )
            CommunicationContext.objects.create(
                case=self.case,
                message=message,
                source_document=self.export,
                source_sha256=self.export.sha256,
                source_row=number,
                source_start=number - 1,
                source_end=number,
                parser_version="synthetic-v1",
                timezone="America/Toronto",
                raw_sender="Alex",
                native_message_id=f"synthetic-{number}",
            )
            self.messages.append(message)
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                url = (
                    self.live_server_url
                    + self.reader_path
                    + f"?source={self.export.pk}&list_page=2"
                )
                page.goto(url)
                page.evaluate("window.conversationDocument = document")
                messages = page.locator(".conversation-message")
                expect(messages).to_have_count(50)
                self.assertLessEqual(messages.first.bounding_box()["height"], 190)
                edge = page.locator('[data-conversation-edge="next"]')
                with page.expect_request(
                    lambda request: request.resource_type == "fetch"
                ) as requested:
                    edge.scroll_into_view_if_needed()
                params = parse_qs(urlsplit(requested.value.url).query)
                self.assertEqual(
                    params,
                    {
                        "source": [str(self.export.pk)],
                        "list_page": ["2"],
                        "page": ["2"],
                    },
                )
                expect(messages).to_have_count(100)
                expect(edge.locator("a")).to_have_attribute(
                    "href", re.compile(r"&page=3$")
                )
                edge.scroll_into_view_if_needed()
                expect(messages).to_have_count(104)
                expect(edge).to_contain_text("End of messages.")
                expect(edge.locator("a")).to_be_hidden()
                expect(page.locator(".conversation-date")).to_have_count(2)
                expect(page).to_have_url(url)
                self.assertTrue(
                    page.evaluate("window.conversationDocument === document")
                )
                ids = messages.evaluate_all("rows => rows.map(row => row.id)")
                self.assertEqual(ids, [f"message-{row.pk}" for row in self.messages])
                target = self.messages[-1]
                details = page.locator(f"#message-details-{target.pk}")
                details.locator(":scope > summary").press("Enter")
                expect(details).to_have_attribute("open", "")
                expect(details).to_contain_text("synthetic-104")
                expect(
                    page.locator(f"#message-{target.pk}").get_by_role(
                        "link", name="Open evidence", exact=False
                    )
                ).to_have_attribute(
                    "href",
                    self.evidence_path(target)
                    + f"?conversation_source={self.export.pk}&conversation_list_page=2",
                )
                self.capture(page, engine, "scrolled-filtered-messages")

    def test_scrolling_prepends_without_moving_linked_message_or_focus(self):
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                pending = []

                def hold_earlier_page(route, _request, pending=pending):
                    if route.request.resource_type == "fetch":
                        pending.append(route)
                    else:
                        route.continue_()

                page.route("**" + self.reader_path + "?**", hold_earlier_page)
                target = self.messages[51]
                page.goto(
                    self.live_server_url
                    + self.reader_path
                    + f"?source={self.export.pk}&message={target.pk}"
                    + f"#message-{target.pk}"
                )
                edge = page.locator('[data-conversation-edge="previous"]')
                edge.scroll_into_view_if_needed()
                expect(edge).to_contain_text("Loading messages")
                summary = page.locator(f"#message-details-{target.pk} > summary")
                summary.focus()
                position = summary.bounding_box()["y"]
                self.assertEqual(len(pending), 1)
                pending[0].continue_()
                expect(page.locator(".conversation-message")).to_have_count(54)
                expect(summary).to_be_focused()
                self.assertAlmostEqual(summary.bounding_box()["y"], position, delta=2)
                expect(page.locator(".conversation-date")).to_have_count(1)
                expect(page.locator("[data-selected-message]")).to_have_count(1)
                expect(page.locator(f"#message-details-{target.pk}")).to_have_attribute(
                    "open", ""
                )
                expect(edge).to_contain_text("Start of messages.")

    def test_failed_scroll_recovers_with_full_page_navigation(self):
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)

                def fail_fetch(route):
                    if route.request.resource_type == "fetch":
                        route.abort()
                    else:
                        route.continue_()

                page.route("**" + self.reader_path + "?page=2", fail_fetch)
                page.goto(self.live_server_url + self.reader_path)
                edge = page.locator('[data-conversation-edge="next"]')
                edge.scroll_into_view_if_needed()
                expect(edge).to_contain_text("Could not load messages.")
                expect(page.locator(".conversation-message")).to_have_count(50)
                link = edge.get_by_role("link", name="Later messages", exact=True)
                link.focus()
                page.keyboard.press("Enter")
                expect(page).to_have_url(
                    self.live_server_url + self.reader_path + "?page=2"
                )
                expect(
                    page.locator(f"#message-{self.manual_message.pk}")
                ).to_be_attached()

    def test_revoked_access_blocks_the_next_scroll_batch(self):
        for engine in self.engines:
            # Restore the synthetic membership before checking the next engine.
            CaseMembership.objects.get_or_create(
                case=self.case, user=self.viewer, defaults={"role": "viewer"}
            )
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                page.goto(self.live_server_url + self.reader_path)
                self.db(
                    lambda: CaseMembership.objects.filter(
                        case=self.case, user=self.viewer
                    ).delete()
                )
                edge = page.locator('[data-conversation-edge="next"]')
                edge.scroll_into_view_if_needed()
                expect(edge).to_contain_text("Could not load messages.")
                expect(page.locator(".conversation-message")).to_have_count(50)
                expect(page.locator(f"#message-{self.messages[-1].pk}")).to_have_count(
                    0
                )

    def test_conversation_list_scrolls_and_retains_link_page_context(self):
        for number in range(26):
            person = Person.objects.create(
                case=self.case, first_name=f"Correspondent {number:02}"
            )
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                sender=self.alex,
                recipient=person,
                title=f"Additional pair {number}",
                body="Synthetic correspondence for list scrolling.",
            )
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                page.goto(self.url("phone:conversation_list", case_id=self.case.pk))
                rows = page.locator("[data-conversation-items] > a")
                expect(rows).to_have_count(25)
                self.capture(page, engine, "compact-conversation-list")
                edge = page.locator('[data-conversation-edge="next"]')
                edge.scroll_into_view_if_needed()
                expect(rows).to_have_count(27)
                expect(edge).to_contain_text("End of conversations.")
                ids = rows.evaluate_all("rows => rows.map(row => row.id)")
                self.assertEqual(len(set(ids)), 27)
                expect(rows.last).to_have_attribute(
                    "href", re.compile(r"\?list_page=2$")
                )
                rows.last.click()
                expect(
                    page.get_by_role("link", name="All conversations")
                ).to_have_attribute(
                    "href",
                    reverse("phone:conversation_list", args=(self.case.pk,))
                    + "?page=2",
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

    def make_saved_findings(self):
        """Retain synthetic mixed-method results without invoking inference."""
        first, last = self.messages[0], self.messages[51]
        inputs = snapshot_sources(self.case, [{"kind": "text-message", "id": first.pk}])
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="text_topics",
            method_version=get_operation("text_topics").version,
            inputs=inputs,
            status="partial",
        )
        items = [
            {
                "label": f"Retained observation {number + 1:02}",
                "text": (
                    "A possible harbour meeting is suggested."
                    if number == 1
                    else f"Literal wording observation {number + 1:02}: "
                    "<script> remains quoted text, not executable content."
                ),
                "source_ids": [inputs[0]["id"]],
                "values": {"status": "unresolved"} if number == 1 else {},
            }
            for number in range(28)
        ]
        result = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data={"summary": "Synthetic saved wording observations", "items": items},
        )
        project_result(result)
        AnalysisReview.objects.create(
            case=self.case,
            result=result,
            reviewed_by=self.owner,
            item_index=1,
            decision="rejected",
            comment="Synthetic suggestion needs more context.",
        )
        AnalysisReview.objects.create(
            case=self.case,
            result=result,
            reviewed_by=self.owner,
            decision="unresolved",
        )
        shared_inputs = snapshot_sources(
            self.case,
            [
                {"kind": "text-message", "id": first.pk},
                {"kind": "text-message", "id": last.pk},
            ],
        )
        shared_run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="session_summaries",
            method_version=get_operation("session_summaries").version,
            inputs=shared_inputs,
            status="complete",
        )
        shared_result = AnalysisResult.objects.create(
            case=self.case,
            run=shared_run,
            data={
                "summary": "Synthetic source-cited session suggestions",
                "items": [
                    {
                        "label": f"Shared exchange {number + 1:02}",
                        "text": "The selected messages mention a possible meeting.",
                        "source_ids": [source["id"] for source in shared_inputs],
                        "values": {"status": "unresolved"},
                    }
                    for number in range(27)
                ],
            },
        )
        project_result(shared_result)

    def test_saved_findings_without_javascript_paging_and_message_links(self):
        self.make_saved_findings()
        run_count = AnalysisRun.objects.count()
        first, target = self.messages[0], self.messages[51]
        url = (
            self.live_server_url
            + self.reader_path
            + f"?source={self.export.pk}&list_page=2"
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(url)
                inline = page.locator(f"#message-analysis-{first.pk}")
                shared = page.locator("#conversation-analysis")
                summary = inline.locator(":scope > summary")
                expect(summary).to_contain_text("Findings · 28")
                expect(inline.locator(".conversation-analysis-preview")).to_be_visible()
                expect(inline).not_to_have_attribute("open", "")
                summary.focus()
                page.keyboard.press("Enter")
                expect(inline).to_have_attribute("open", "")
                expect(summary).to_be_focused()
                expect(page).to_have_url(url)
                expect(inline.locator(".conversation-analysis-preview")).to_be_hidden()
                findings = inline.locator(".conversation-finding")
                expect(findings).to_have_count(3)
                expect(findings.nth(0)).not_to_contain_text("AI suggestion")
                expect(findings.nth(0)).to_contain_text("<script>")
                expect(findings.nth(0).locator("script")).to_have_count(0)
                expect(findings.nth(1)).to_contain_text("AI suggestion")
                expect(findings.nth(1)).to_contain_text("Rejected")
                expect(findings.nth(1)).to_contain_text("Whole result: Unresolved")
                expect(findings.nth(1)).to_contain_text("Partial coverage")
                expect(inline).not_to_contain_text("Shared exchange")
                expect(shared.locator(":scope > summary")).to_contain_text(
                    "Conversation findings · 27"
                )
                summary.scroll_into_view_if_needed()
                self.capture(page, engine, "saved-findings-inline-preview")
                inline.get_by_role("link", name="More findings", exact=False).click()
                params = parse_qs(urlsplit(page.url).query)
                self.assertEqual(params["source"], [str(self.export.pk)])
                self.assertEqual(params["list_page"], ["2"])
                self.assertEqual(params["message"], [str(first.pk)])
                self.assertEqual(params["analysis_page"], ["1"])
                expect(inline).to_have_attribute("open", "")
                expect(findings).to_have_count(25)
                inline.get_by_role("link", name="Next findings", exact=True).click()
                expect(findings).to_have_count(3)
                expect(inline).to_contain_text("Retained observation 28")
                page.go_back()
                expect(findings).to_have_count(25)
                findings.first.get_by_role(
                    "link", name="Open finding", exact=False
                ).click()
                expect(page.locator(".analysis-finding")).to_be_visible()
                page.go_back()
                expect(inline).to_have_attribute("open", "")
                expect(findings).to_have_count(25)
                shared.locator(":scope > summary").press("Enter")
                expect(shared.locator(".conversation-finding")).to_have_count(25)
                shared.get_by_role("link", name="Next findings", exact=True).click()
                expect(shared).to_have_attribute("open", "")
                expect(shared.locator(".conversation-finding")).to_have_count(2)
                expect(findings).to_have_count(25)
                shared.get_by_role("link", name="Previous findings", exact=True).click()
                shared.locator(f'a[href*="message={target.pk}"]').first.click()
                expect(page.locator(f"#message-{target.pk}")).to_be_in_viewport()
                expect(page.locator(f"#message-details-{target.pk}")).to_have_attribute(
                    "open", ""
                )
                params = parse_qs(urlsplit(page.url).query)
                self.assertEqual(params["message"], [str(target.pk)])
                self.assertEqual(params["source"], [str(self.export.pk)])
                self.assertEqual(params["list_page"], ["2"])
                self.assertEqual(self.db(AnalysisRun.objects.count), run_count)

    def test_saved_findings_reflow_focus_and_scrolling_preserve_context(self):
        self.make_saved_findings()
        first = self.messages[0]
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                url = (
                    self.live_server_url
                    + self.reader_path
                    + f"?source={self.export.pk}&list_page=2"
                    + f"&message={first.pk}&analysis_page=1"
                    + "&conversation_analysis_page=1"
                    + f"#message-analysis-{first.pk}"
                )
                page.goto(url)
                inline = page.locator(f"#message-analysis-{first.pk}")
                shared = page.locator("#conversation-analysis")
                summary = inline.locator(":scope > summary")
                expect(inline).to_have_attribute("open", "")
                expect(shared).to_have_attribute("open", "")
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    summary.scroll_into_view_if_needed()
                    self.assert_reflow(page, width)
                    self.capture(page, engine, f"saved-findings-{width}-{height}")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                summary.focus()
                page.keyboard.press("Enter")
                expect(inline).not_to_have_attribute("open", "")
                expect(summary).to_be_focused()
                self.capture(page, engine, "saved-findings-dark-reduced-motion")
                page.emulate_media(forced_colors="active")
                page.keyboard.press("Enter")
                expect(inline).to_have_attribute("open", "")
                expect(summary).to_be_focused()
                self.capture(page, engine, "saved-findings-forced-colors")
                page.emulate_media(color_scheme="light", forced_colors="none")
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self.assert_reflow(page, 1440)
                self.capture(page, engine, "saved-findings-text-200-percent")
                page.evaluate("document.documentElement.style.fontSize = ''")
                page.set_viewport_size({"width": 360, "height": 225})
                self.assert_reflow(page, 360)
                self.capture(page, engine, "saved-findings-400-percent-reflow")
                page.set_viewport_size({"width": 1440, "height": 900})
                edge = page.locator('[data-conversation-edge="next"]')
                with page.expect_request(
                    lambda request: request.resource_type == "fetch"
                ) as requested:
                    edge.scroll_into_view_if_needed()
                self.assertEqual(
                    parse_qs(urlsplit(requested.value.url).query),
                    {
                        "source": [str(self.export.pk)],
                        "list_page": ["2"],
                        "page": ["2"],
                    },
                )
                expect(page.locator(".conversation-message")).to_have_count(54)
                expect(page).to_have_url(url)
