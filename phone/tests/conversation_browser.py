"""Conversation inspection and exact evidence navigation with native links."""

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from playwright.sync_api import expect

from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase, DrainableBrowserLiveServerThread
from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.catalogue import project_result
from core.analysis.navigation import finding_url
from core.analysis.operations import get_operation
from core.analysis.source_resolution import resolve_inputs
from core.analysis.source_selection import snapshot_sources
from core.analysis.tests.test_gateway import CONFIG
from core.analysis.tests.testing import external_grant_kwargs
from core.evidence.retention import set_evidence_archive
from core.models import AnalysisResult, AnalysisReview, AnalysisRun, CaseAnalysisBatch
from document.models import DocumentEvidence
from people.models import Person
from phone.models import CommunicationContext, TextMessageEvidence


class ConversationBrowserTests(BrowserTestCase):
    artifact_subdirectory = "conversations-browser"
    server_thread_class = DrainableBrowserLiveServerThread

    def _fixture_teardown(self):
        with self.server_thread.httpd.drained_connections():
            super()._fixture_teardown()

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

    def test_editors_can_add_messages_from_empty_and_populated_conversations(self):
        editor = self.make_user("conversation-browser-editor")
        empty_case = Case.objects.create(
            owner=self.owner, name="Synthetic empty conversations"
        )
        for case in (self.case, empty_case):
            CaseMembership.objects.create(case=case, user=editor, role="editor")

        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, editor)
                for case, state in ((self.case, "populated"), (empty_case, "empty")):
                    page.goto(self.url("phone:conversation_list", case_id=case.pk))
                    add = page.get_by_role("link", name="Add text message", exact=True)
                    upload = page.get_by_role(
                        "link", name="Import message CSV", exact=True
                    )
                    expect(add).to_have_attribute(
                        "href",
                        reverse("core:evidence:create", args=(case.pk,))
                        + "?kind=text-message",
                    )
                    expect(upload).to_have_attribute(
                        "href", reverse("phone:imessage_upload", args=(case.pk,))
                    )
                    for width, height in ((1440, 900), (390, 844), (320, 844)):
                        page.set_viewport_size({"width": width, "height": height})
                        expect(add).to_be_visible()
                        expect(upload).to_be_visible()
                        self.assertLessEqual(
                            page.evaluate("document.documentElement.scrollWidth"),
                            width + 1,
                        )
                        self.capture(page, engine, f"entry-actions-{state}-{width}")

    def test_case_shell_stays_mounted_between_conversations_and_chronology(self):
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                page.goto(self.url("core:analysis:index", case_id=self.case.pk))
                page.evaluate("window.rail = document.querySelector('.app-navigation')")
                page.evaluate("window.search = document.getElementById('case-search')")
                page.get_by_role("link", name="Conversations", exact=True).click()
                page.locator(f'a[href="{self.reader_path}"]').click()
                panel = page.locator("[data-message-panel]")
                page.locator("[data-message-select]").first.click()
                expect(panel).to_be_visible()
                page.go_back()
                expect(panel).to_be_hidden()
                page.get_by_role("link", name="Case Tools", exact=True).click()
                page.get_by_role("link", name="Chronology", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Daily timeline")
                ).to_be_visible()
                page.get_by_role("link", name="Processing", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Processing", exact=True)
                ).to_be_visible()
                page.go_back()
                expect(
                    page.get_by_role("heading", name="Daily timeline")
                ).to_be_visible()
                self.assertTrue(
                    page.evaluate(
                        "window.rail.isConnected && window.search.isConnected"
                    )
                )

    def test_newer_reader_actions_keep_selection_and_navigation(self):
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                page.goto(self.live_server_url + self.reader_path)
                later = page.locator('[data-conversation-edge="next"] a')
                pending = []

                def hold(route, _request, pending=pending):
                    pending.append(route)

                page.route("**" + self.reader_path + "?page=2", hold)
                with page.expect_request(lambda req: req.url.endswith("?page=2")):
                    later.evaluate("link => link.click()")
                page.locator("[data-message-select]").first.click()
                heading = page.locator("[data-message-panel] h2")
                expect(heading).to_be_focused()
                pending.pop().continue_()
                expect(page.locator(".conversation-message")).to_have_count(55)
                expect(heading).to_be_focused()

                timeline = self.url("core:evidence:chronology", case_id=self.case.pk)
                processing = self.url("core:analysis:processing", case_id=self.case.pk)
                page.goto(f"{timeline}?view=day&day=2026-11-10")
                page.route(processing, hold)
                rail_link = page.get_by_role("link", name="Processing", exact=True)
                with page.expect_request(processing):
                    rail_link.click()

                dates = []

                def hold_date(route, _request, dates=dates):
                    dates.append(route)

                page.route(f"{timeline}?**day=2026-11-12*", hold_date, times=1)
                date = page.get_by_label("Date", exact=True)
                with page.expect_request(lambda req: "day=2026-11-12" in req.url):
                    date.fill("2026-11-12")
                expect(page.locator("[data-workspace-status]")).not_to_have_attribute(
                    "aria-busy", "true"
                )
                pending.pop().continue_()
                dates.pop().continue_()
                expect(page.locator('[data-timeline-day="2026-11-12"]')).to_be_visible()
                expect(page).to_have_url(re.compile("day=2026-11-12"))

                page.route(f"{timeline}?**day=2026-11-13*", hold_date, times=1)
                with page.expect_request(lambda req: "day=2026-11-13" in req.url):
                    date.fill("2026-11-13")
                with page.expect_request(processing):
                    rail_link.click()
                dates.pop().continue_()
                pending.pop().continue_()
                expect(
                    page.get_by_role("heading", name="Processing", exact=True)
                ).to_be_visible()
                page.go_back()
                expect(page.locator('[data-timeline-day="2026-11-12"]')).to_be_visible()

    def test_no_javascript_unified_messages_and_exact_evidence_roundtrip(self):
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
                for label in ("Add text message", "Import message CSV"):
                    expect(
                        page.get_by_role("link", name=label, exact=True)
                    ).to_have_count(0)
                page.locator(f'a[href="{self.reader_path}"]').click()
                panel = page.get_by_role("complementary", name="Message details")
                expect(panel).to_be_hidden()
                expect(page.get_by_role("combobox", name="Source")).to_have_count(0)
                page.get_by_role("link", name="Later messages", exact=True).click()
                target = self.messages[51]
                target_row = page.locator(f"#message-{target.pk}")
                target_link = target_row.locator("[data-message-select]")
                target_link.press("Enter")
                expect(panel).to_be_visible()
                expect(panel.locator(".conversation-message-body")).to_have_text(
                    target.body
                )
                expect(target_link).to_have_attribute("aria-current", "true")
                source = panel.get_by_text("Message source", exact=True)
                metadata = panel.get_by_role("link", name="View source metadata")
                expect(metadata).to_be_hidden()
                source.press("Enter")
                expect(panel.get_by_text("Row 52", exact=False)).to_be_visible()
                metadata.press("Enter")
                expect(page.locator(".evidence-info")).to_have_attribute("open", "")
                expect(page.locator(".evidence-info")).to_contain_text("synthetic-52")
                page.get_by_role("link", name="View in conversation").press("Enter")
                expect(target_link).to_have_attribute("aria-current", "true")
                panel.get_by_role("link", name="Open evidence", exact=False).click()
                expect(page).to_have_url(
                    self.live_server_url + self.evidence_path(target)
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
                self.assertNotIn("source", params)
                self.assertEqual(params["message"], [str(target.pk)])
                expect(target_row).to_be_in_viewport()
                expect(panel).to_be_visible()
                expect(target_link).to_have_attribute("aria-current", "true")
                expect(
                    panel.get_by_role("link", name="Open evidence", exact=False)
                ).to_have_attribute(
                    "href",
                    self.evidence_path(target),
                )
                self.capture(page, engine, "no-javascript-exact-message")
                page.set_viewport_size({"width": 390, "height": 844})
                expect(panel).to_be_visible()
                expect(panel.locator(".conversation-message-body")).to_have_text(
                    target.body
                )
                self.assert_reflow(page, 390)
                self.capture(page, engine, "mobile-message-details")
                panel.get_by_role("link", name="Back to messages").press("Enter")
                expect(panel).to_be_hidden()
                expect(target_link).to_be_in_viewport()
                self.assertNotIn("source", parse_qs(urlsplit(page.url).query))

    def evidence_path(self, message):
        return reverse(
            "core:evidence:detail",
            kwargs={
                "case_id": self.case.pk,
                "kind": "text-message",
                "evidence_id": message.pk,
            },
        )

    def test_scrolling_appends_unified_messages_without_navigation(self):
        self.save_sessions(self.messages[48:52])
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
                url = self.live_server_url + self.reader_path + "?list_page=2"
                page.goto(url)
                page.evaluate("window.conversationDocument = document")
                messages = page.locator(".conversation-message")
                expect(messages).to_have_count(50)
                boundary_tint = messages.nth(49).get_attribute("data-session-tint")
                self.assertTrue(boundary_tint)
                expect(messages.nth(48)).to_contain_text("Session starts")
                expect(
                    messages.nth(49).locator(".conversation-session-marker")
                ).to_have_count(0)
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
                        "list_page": ["2"],
                        "page": ["2"],
                    },
                )
                expect(messages).to_have_count(100)
                expect(messages.nth(50)).to_have_attribute(
                    "data-session-tint", boundary_tint
                )
                expect(
                    messages.nth(50).locator(".conversation-session-marker")
                ).to_have_count(0)
                expect(messages.nth(51)).to_contain_text("Session ends")
                expect(edge.locator("a")).to_have_attribute(
                    "href", re.compile(r"&page=3$")
                )
                edge.scroll_into_view_if_needed()
                expect(messages).to_have_count(105)
                expect(edge).to_contain_text("End of messages.")
                expect(edge.locator("a")).to_be_hidden()
                expect(page.locator(".conversation-date")).to_have_count(3)
                expect(page).to_have_url(url)
                self.assertTrue(
                    page.evaluate("window.conversationDocument === document")
                )
                ids = messages.evaluate_all("rows => rows.map(row => row.id)")
                self.assertEqual(
                    ids,
                    [
                        f"message-{row.pk}"
                        for row in [*self.messages, self.manual_message]
                    ],
                )
                target = self.messages[-1]
                page.locator(f"#message-{target.pk} [data-message-select]").press(
                    "Enter"
                )
                panel = page.get_by_role("complementary", name="Message details")
                expect(panel).to_be_visible()
                expect(panel.locator(".conversation-message-body")).to_have_text(
                    target.body
                )
                self.assertTrue(
                    page.evaluate("window.conversationDocument === document")
                )
                expect(
                    panel.get_by_role("link", name="Open evidence")
                ).to_have_attribute(
                    "href",
                    self.evidence_path(target) + "?conversation_list_page=2",
                )
                self.capture(page, engine, "scrolled-messages")

                # A refresh retains the selected message but discards earlier pages.
                page.reload()
                items = page.locator("[data-conversation-items]")
                expect(items).to_have_attribute("data-page", "3")
                expect(panel.locator(".conversation-message-body")).to_have_text(
                    target.body
                )
                page.go_back()
                expect(page).to_have_url(url)
                expect(items).to_have_attribute("data-page", "1")
                expect(page.locator(f"#message-{self.messages[0].pk}")).to_be_attached()
                expect(panel).to_be_hidden()

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
                    + f"?message={target.pk}"
                    + f"#message-{target.pk}"
                )
                edge = page.locator('[data-conversation-edge="previous"]')
                edge.scroll_into_view_if_needed()
                expect(edge).to_contain_text("Loading messages")
                selected = page.locator(f"#message-{target.pk} [data-message-select]")
                selected.focus()
                position = selected.bounding_box()["y"]
                self.assertEqual(len(pending), 1)
                pending[0].continue_()
                expect(page.locator(".conversation-message")).to_have_count(55)
                expect(selected).to_be_focused()
                self.assertAlmostEqual(selected.bounding_box()["y"], position, delta=2)
                expect(page.locator(".conversation-date")).to_have_count(2)
                expect(page.locator("[data-selected-message]")).to_have_count(1)
                expect(selected).to_have_attribute("aria-current", "true")
                expect(
                    page.get_by_role("complementary", name="Message details")
                ).to_be_visible()
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

                page.route("**" + self.reader_path + "?**", fail_fetch)
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

    def test_archive_and_restore_require_reload_before_scrolling(self):
        first = self.messages[0]
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                page.goto(self.live_server_url + self.reader_path)
                first_row = page.locator(f"#message-{first.pk}")
                messages = page.locator(".conversation-message")
                edge = page.locator('[data-conversation-edge="next"]')
                expect(first_row).to_have_count(1)
                for archived in (True, False):
                    self.db(
                        set_evidence_archive,
                        self.owner,
                        self.case.pk,
                        "text-message",
                        first.pk,
                        archived=archived,
                    )
                    edge.get_by_role("link", name="Later messages", exact=True).press(
                        "Enter"
                    )
                    expect(edge).to_contain_text("Messages changed.")
                    expect(messages).to_have_count(50)
                    expect(first_row).to_have_count(int(archived))
                    edge.get_by_role("link", name="Reload conversation").press("Enter")
                    expect(first_row).to_have_count(int(not archived))
                    expect(
                        edge.get_by_role("link", name="Later messages", exact=True)
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
                expect(page.locator(".conversation-message")).to_have_count(50)
                self.db(
                    lambda: CaseMembership.objects.filter(
                        case=self.case, user=self.viewer
                    ).delete()
                )
                page.evaluate(
                    "document.querySelector('[data-conversation-edge=next]')"
                    "?.scrollIntoView()"
                )
                expect(
                    page.get_by_role("heading", name="Your session or access changed")
                ).to_be_focused()
                expect(page.locator(".conversation-message")).to_have_count(0)
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

    def save_sessions(self, messages, *, gap_minutes=15):
        operation = get_operation("sessions")
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="sessions",
            method_version=operation.version,
            inputs=snapshot_sources(
                self.case, [{"kind": "text-message", "id": m.pk} for m in messages]
            ),
            status="complete",
            parameters={"gap_minutes": gap_minutes},
        )
        data = operation.function(resolve_inputs(run, self.case), run.parameters)
        project_result(
            AnalysisResult.objects.create(case=self.case, run=run, data=data)
        )

    @override_settings(PANDECT_ANALYSIS_MAX_SOURCES=5)
    def test_regroup_publishes_one_session_and_opens_its_delayed_reply(self):
        self.save_sessions(self.messages[:2])
        parent = self.messages[-1]
        reply = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            sender=self.alex,
            recipient=self.blair,
            occurred_at=parent.occurred_at + timedelta(hours=2),
            title="Delayed reply",
            body="Yes, I can meet by the harbour.",
        )
        CommunicationContext.objects.create(
            case=self.case,
            message=reply,
            source_document=self.export,
            source_sha256=self.export.sha256,
            source_row=55,
            source_start=54,
            source_end=55,
            parser_version="synthetic-v1",
            timezone="UTC",
            raw_sender="Alex",
            native_message_id="synthetic-55",
            reply_to_id="synthetic-54",
        )
        reader_url = self.live_server_url + self.reader_path + "?tab=instances"
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
                patch("phone.grouping_jobs.PAGE_SIZE", 54),
            ):
                self.sign_in(page)
                page.goto(reader_url)
                old = page.locator(".conversation-instance").filter(
                    has=page.get_by_role("heading", name=re.compile(r"· 2 messages$"))
                )
                expect(old).to_be_visible()
                page.get_by_role("link", name="Group this conversation").press("Enter")
                page.get_by_role("button", name="Group messages").press("Enter")
                page.get_by_role("link", name="Processing", exact=True).press("Enter")
                job = page.get_by_role(
                    "link", name=re.compile("^Grouping conversation")
                )
                job.press("Enter")
                progress = page.locator("#processing-batch-progress")
                expect(progress).to_contain_text("0 of 56 messages grouped")
                batch_id = parse_qs(urlsplit(page.url).query)["batch"][0]
                self.assertEqual(
                    self.db(prepare_case_analysis_batch, batch_id), "queued"
                )
                page.reload()
                expect(progress).to_contain_text("54 of 56 messages grouped")
                self.assertEqual(
                    self.db(prepare_case_analysis_batch, batch_id), "complete"
                )
                page.reload()
                expect(progress).to_contain_text("Sessions are ready.")
                progress.get_by_role("link", name="Open sessions & summaries").press(
                    "Enter"
                )
                expect(old).to_be_visible()
                row = page.locator(".conversation-instance").filter(
                    has=page.get_by_role("heading", name=re.compile(r"· 55 messages$"))
                )
                expect(row).to_have_count(1)
                row.get_by_role("link", name="Grouping details").press("Enter")
                expect(
                    page.get_by_role("heading", name="Replies after a pause")
                ).to_be_visible()
                expect(
                    page.get_by_text("The reply came 120 minutes later.", exact=False)
                ).to_be_visible()
                earlier = page.get_by_role("link", name="Earlier message")
                answer = page.get_by_role("link", name="Reply · message 55")
                expect(earlier).to_have_attribute("href", self.evidence_path(parent))
                expect(answer).to_have_attribute("href", self.evidence_path(reply))
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assert_reflow(page, width)
                    if width in (1440, 390) and height in (900, 844):
                        self.capture(page, engine, f"reply-grouping-details-{width}")
                answer.press("Enter")
                expect(page.locator(".original-text")).to_contain_text(reply.body)
                page.go_back()
                page.get_by_role("link", name="Back to conversation session").press(
                    "Enter"
                )
                row.get_by_role("link", name="Later messages").press("Enter")
                expect(row.locator(".instance-message-list li")).to_have_count(5)
                expect(row.locator(".instance-message-list li").last).to_contain_text(
                    reply.body
                )
                row.get_by_role("link", name="Summary coverage").press("Enter")
                expect(
                    page.get_by_role("heading", name="Session summary coverage")
                ).to_be_visible()
                page.get_by_role("link", name="Later messages").press("Enter")
                expect(page.get_by_text("Messages 51–55")).to_be_visible()
                expect(page.get_by_text("Awaiting summary")).to_be_visible()
                self.assert_reflow(page, 320)
                self.capture(page, engine, "reply-summary-coverage-320")
            with self.browser_page(engine, java_script_enabled=False) as (_, viewer):
                self.sign_in(viewer, self.viewer)
                viewer.goto(reader_url)
                expect(
                    viewer.get_by_role("heading", name=re.compile(r"· 55 messages$"))
                ).to_be_visible()
                expect(
                    viewer.get_by_role("link", name="Group this conversation")
                ).to_have_count(0)

    def test_jump_to_date_scrolls_both_ways_in_messages_and_instances(self):
        midnight = datetime(2026, 11, 11, tzinfo=UTC)
        later = [
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                sender=self.alex,
                recipient=self.blair,
                occurred_at=midnight + timedelta(minutes=number),
                title=f"Next day message {number}",
                body=f"Original next day message {number}",
            )
            for number in range(52)
        ]
        self.save_sessions(self.messages[:1])
        self.save_sessions([*self.messages[1:], *later], gap_minutes=1440)
        self.save_sessions([self.manual_message])
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                patch("phone.conversation_instances.INSTANCES_PER_PAGE", 1),
                self.browser_page(engine) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                for tab in ("messages", "instances"):
                    with self.subTest(tab=tab):
                        path = self.reader_path + "?list_page=2"
                        if tab == "instances":
                            path += "&tab=instances"
                        page.set_viewport_size({"width": 1440, "height": 900})
                        page.goto(self.live_server_url + path)
                        self.capture(page, engine, f"{tab}-date-picker-desktop")
                        page.set_viewport_size({"width": 390, "height": 844})
                        field = page.get_by_label("Jump to date")
                        field.fill("2026-11-11")
                        page.get_by_role("button", name="Go", exact=True).press("Enter")
                        jump = page.locator(
                            "[data-conversation-items] #conversation-day-result"
                        )
                        expect(jump).to_be_in_viewport()
                        expect(field).to_have_value("2026-11-11")
                        self.assert_reflow(page, 390)
                        self.capture(page, engine, f"{tab}-date-jump-mobile")
                        page.evaluate("window.dateJumpDocument = document")
                        if tab == "instances":
                            reader = page.locator(".instance-originals")
                            records = reader.locator(".instance-message-list > li")
                            expected_count = 105
                            first_message = self.messages[1]
                        else:
                            reader = page.locator(".conversation-reader")
                            records = reader.locator(".conversation-message")
                            expected_count = 107
                            first_message = self.messages[0]

                        for direction in ("previous", "next"):
                            reader.locator(
                                f':scope > [data-conversation-edge="{direction}"]'
                            ).scroll_into_view_if_needed()
                        expect(records).to_have_count(expected_count)
                        expect(records.first).to_contain_text(first_message.body)
                        expect(
                            reader.get_by_text(later[-1].body, exact=True)
                        ).to_be_attached()
                        if tab == "instances":
                            for direction in ("previous", "next"):
                                page.locator(
                                    "#instances-reader > "
                                    f'[data-conversation-edge="{direction}"]'
                                ).scroll_into_view_if_needed()
                            expect(
                                page.locator(".conversation-instance")
                            ).to_have_count(3)
                        self.assertTrue(
                            page.evaluate("window.dateJumpDocument === document")
                        )

    def test_search_messages_jumps_between_pages_with_surrounding_context(self):
        phrase = "Meet at the north gate"
        first, second = [
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                sender=self.alex,
                recipient=self.blair,
                occurred_at=self.messages[index].occurred_at + timedelta(seconds=30),
                title="Synthetic search fixture",
                body=f"{phrase} at {time}.",
            )
            for index, time in ((3, "noon"), (50, "dusk"))
        ]
        url = self.live_server_url + self.reader_path + "?list_page=2"
        for engine in self.engines:
            for javascript in (True, False):
                with (
                    self.subTest(engine=engine, javascript=javascript),
                    self.browser_page(engine, java_script_enabled=javascript) as (
                        _,
                        page,
                    ),
                ):
                    self.sign_in(page, self.viewer)
                    page.goto(url)
                    reader = page.get_by_role("region", name="Messages", exact=True)
                    search = page.get_by_label("Search messages", exact=True)
                    first_row = reader.locator(f"#message-{first.pk}")
                    second_row = reader.locator(f"#message-{second.pk}")
                    expect(second_row).to_have_count(0)

                    search.fill(phrase.lower())
                    search.press("Enter")
                    expect(first_row).to_be_in_viewport()
                    expect(search).to_have_value(phrase.lower())
                    expect(
                        page.get_by_text("Match 1 of 2", exact=True)
                    ).to_be_in_viewport()
                    page.get_by_role("link", name="Next match", exact=True).press(
                        "Enter"
                    )
                    expect(second_row).to_be_in_viewport()
                    status = page.get_by_text("Match 2 of 2", exact=True)
                    expect(status).to_be_in_viewport()
                    for neighbor in self.messages[50:52]:
                        neighbor_row = reader.locator(f"#message-{neighbor.pk}")
                        expect(neighbor_row).to_be_visible()
                        expect(neighbor_row).to_contain_text(
                            neighbor.body.splitlines()[0]
                        )
                    page.get_by_role("link", name="Previous match", exact=True).press(
                        "Enter"
                    )
                    expect(first_row).to_be_in_viewport()
                    page.go_back()
                    expect(second_row).to_be_in_viewport()
                    highlight = second_row.locator("mark")
                    expect(status).to_be_in_viewport()
                    expect(highlight).to_be_in_viewport()
                    if javascript:
                        for width, height in (
                            (1440, 900),
                            (1440, 1024),
                            (768, 1024),
                            (390, 844),
                            (320, 844),
                        ):
                            page.set_viewport_size({"width": width, "height": height})
                            # Frame both the controls and the highlight for captures.
                            second_row.scroll_into_view_if_needed()
                            self.assert_reflow(page, width)
                            if width in (1440, 390):
                                self.capture(
                                    page, engine, f"message-search-{width}-{height}"
                                )
                        for scheme, forced_colors in (
                            ("dark", "none"),
                            ("light", "active"),
                        ):
                            page.emulate_media(
                                color_scheme=scheme,
                                forced_colors=forced_colors,
                                reduced_motion="reduce",
                            )
                            second_row.scroll_into_view_if_needed()
                            self.assert_reflow(page, 320)
                            expect(status).to_be_in_viewport()
                            expect(highlight).to_be_in_viewport()
                            self.capture(
                                page,
                                engine,
                                f"message-search-320-{scheme}-{forced_colors}",
                            )
                        page.emulate_media(color_scheme="light", forced_colors="none")

                    page.goto(url)
                    search.fill("north gate at dusk")
                    search.press("Enter")
                    expect(second_row).to_be_in_viewport()
                    expect(
                        page.get_by_text("Match 1 of 1", exact=True)
                    ).to_be_in_viewport()
                    page.get_by_role("link", name="Clear search", exact=True).press(
                        "Enter"
                    )
                    expect(search).to_have_value("")
                    expect(second_row).to_be_in_viewport()
                    self.assertEqual(
                        parse_qs(urlsplit(page.url).query).get("list_page"), ["2"]
                    )

    def test_instance_summaries_and_exact_members_without_javascript(self):
        grouped = self.messages[:52]
        self.save_sessions(grouped)
        self.save_sessions(self.messages[52:])
        operation = get_operation("session_summaries")
        inputs = snapshot_sources(
            self.case, [{"kind": "text-message", "id": m.pk} for m in grouped]
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="session_summaries",
            method_version=operation.version,
            inputs=inputs,
            status="complete",
        )
        statement = "Alex suggests a possible meeting by the harbour."
        saved_statement = (
            f"The reported sender **{grouped[0].sender_id}** "
            "suggests a possible meeting by the harbour."
        )
        detail = "No final meeting time was confirmed."
        first_statement = (
            f"{statement} One person suggests the morning because they are working "
            "later. The other person asks for an afternoon meeting and offers to check "
            "the bus schedule. They agree to check in again once they know their "
            f"shifts. {detail}"
        )
        data = operation.function(
            resolve_inputs(run, self.case),
            {},
            infer=lambda prompt, schema: {
                "items": [
                    {
                        "text": text,
                        "citations": [
                            {
                                "source_id": inputs[0]["id"],
                                "quote": "Shall we meet by the harbour?",
                            }
                        ],
                    }
                    for text in (
                        first_statement.replace(statement, saved_statement, 1),
                        "One person proposes meeting on a weekday.",
                        "They discuss getting there by bus.",
                        "One person asks about changing the location.",
                    )
                ]
            },
        )
        AnalysisResult.objects.create(case=self.case, run=run, data=data)
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(self.live_server_url + self.reader_path + "?list_page=2")
                tabs = page.get_by_role("navigation", name="Conversation views")
                tabs.get_by_role("link", name="Sessions & summaries").press("Enter")
                expect(
                    tabs.get_by_role("link", name="Sessions & summaries")
                ).to_have_attribute("aria-current", "page")
                rows = page.locator(".conversation-instance")
                expect(rows).to_have_count(2)
                preview = rows.first.locator(".instance-summary-preview")
                expect(preview).to_be_visible()
                expect(preview).to_contain_text(statement)
                expect(preview).not_to_contain_text("reported sender")
                expect(preview).not_to_contain_text(detail)
                expect(rows.first).to_contain_text("AI suggestion")
                expect(
                    rows.first.get_by_role("link", name="View cited report")
                ).to_be_visible()
                expect(rows.last).to_contain_text("No full summary available")
                expect(
                    rows.last.locator("summary", has_text="Full summary")
                ).to_have_count(0)
                expect(page.get_by_role("link", name="Request summary")).to_have_count(
                    0
                )
                expect(
                    page.get_by_role("link", name="Summarize missing sessions")
                ).to_have_count(0)
                expect(page.locator(".instance-message-list")).to_have_count(0)
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assert_reflow(page, width)
                    self.capture(page, engine, f"instances-{width}-{height}")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "instances-dark")
                disclosure = rows.first.locator("summary", has_text="Full summary")
                statements = rows.first.locator("details .instance-summary")
                expect(statements.first).to_be_hidden()
                url = page.url
                disclosure.press("Enter")
                expect(disclosure).to_be_focused()
                expect(page).to_have_url(url)
                expect(preview).to_be_hidden()
                expect(statements.first).to_be_visible()
                expect(statements.first).to_have_text(first_statement)
                expect(statements.last).to_have_text(
                    "One person asks about changing the location."
                )
                expect(
                    rows.first.get_by_role(
                        "link", name="View citations for summary statement 1"
                    )
                ).to_have_attribute("href", finding_url(self.case.pk, run.pk, 0))
                self.assert_reflow(page, 320)
                self.capture(page, engine, "instance-summary-expanded-mobile")
                disclosure.press("Space")
                expect(statements.first).to_be_hidden()
                expect(preview).to_be_visible()
                page.emulate_media(forced_colors="active")
                show = rows.first.get_by_role("link", name="Show messages")
                show.focus()
                expect(show).to_be_focused()
                self.capture(page, engine, "instances-forced-colors")
                page.emulate_media(color_scheme="light", forced_colors="none")
                rows.first.get_by_role("link", name="Show messages").press("Enter")
                messages = rows.first.locator(".instance-message-list li")
                expect(messages).to_have_count(50)
                expect(messages.first).to_contain_text(grouped[0].body)
                expect(messages.first).to_contain_text("Row 1")
                self.assert_reflow(page, 320)
                self.capture(page, engine, "instance-expanded-mobile")
                rows.first.get_by_role("link", name="Later messages").press("Enter")
                expect(messages).to_have_count(2)
                expect(messages.last).to_contain_text(grouped[-1].body)
                messages.last.get_by_role("link", name="Message details").press("Enter")
                expect(page.locator("[data-selected-message]")).to_have_attribute(
                    "id", f"message-{grouped[-1].pk}"
                )
                page.go_back()
                expect(messages).to_have_count(2)
                rows.first.get_by_role("link", name="Hide messages").press("Enter")
                expect(page.locator(".instance-message-list")).to_have_count(0)
                expect(
                    page.get_by_role("link", name="All conversations")
                ).to_have_attribute("href", re.compile(r"\?page=2$"))

    def test_long_instance_summary_loads_on_expansion_without_javascript(self):
        message = self.messages[0]
        self.save_sessions([message])
        operation = get_operation("session_summaries")
        inputs = snapshot_sources(
            self.case, [{"kind": "text-message", "id": message.pk}]
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="session_summaries",
            method_version=operation.version,
            inputs=inputs,
            status="complete",
        )
        detail = "A final meeting location was never confirmed."
        passage = "Synthetic report detail. " * 135
        source_id = inputs[0]["id"]
        AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data={
                "items": [
                    {
                        "text": f"{number}. {passage}{detail}",
                        "source_ids": [source_id],
                        "values": {
                            "status": "unresolved",
                            "inferred_session": 1,
                            "chunk_source_ids": [source_id],
                        },
                    }
                    for number in range(3)
                ]
            },
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(self.live_server_url + self.reader_path + "?tab=instances")
                row = page.locator(".conversation-instance")
                expect(row.locator(".instance-summary-preview")).to_be_visible()
                expect(row).not_to_contain_text(detail)
                row.get_by_role("link", name="Full summary").press("Enter")
                self.assertEqual(urlsplit(page.url).path, self.reader_path)
                expect(
                    row.locator("details[open] .instance-summary").last
                ).to_contain_text(detail)
                row.locator("summary", has_text="Full summary").press("Space")
                expect(row.locator(".instance-summary-preview")).to_be_visible()

    def test_matching_copies_link_to_the_exact_original(self):
        source = self.messages[0]
        export = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Second synthetic export",
            file=ContentFile(b"another export", name="second.csv"),
        )
        copy = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Retained copy",
            sender=source.sender,
            recipient=source.recipient,
            occurred_at=source.occurred_at,
            body=source.body,
        )
        CommunicationContext.objects.create(
            case=self.case,
            message=copy,
            source_document=export,
            source_sha256=export.sha256,
            source_row=4,
            source_start=0,
            source_end=1,
            parser_version="synthetic-v1",
            timezone="America/Toronto",
            raw_sender="Alex",
            native_message_id=source.communication_context.native_message_id,
        )
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                page.goto(self.live_server_url + self.reader_path)
                row = page.locator(".conversation-message").first
                row.get_by_role("link", name=re.compile("retained copies", re.I)).press(
                    "Enter"
                )
                link = row.get_by_role(
                    "link", name=re.compile("Second synthetic export")
                )
                link.press("Enter")
                panel = page.get_by_role("complementary", name="Message details")
                expect(
                    panel.get_by_role("link", name="Open evidence")
                ).to_have_attribute("href", self.evidence_path(copy))
                expect(page.locator("[data-selected-message]")).to_have_attribute(
                    "id", f"message-{copy.pk}"
                )
                panel.get_by_text("Message source", exact=True).press("Enter")
                expect(panel).to_contain_text("Row 4")
                page.go_back()
                expect(panel).to_be_hidden()
                expect(link).to_be_visible()
                link.press("Enter")
                panel.get_by_role("link", name="Open evidence").press("Enter")
                page.get_by_role("link", name="View in conversation").press("Enter")
                expect(page.locator("[data-selected-message]")).to_have_attribute(
                    "id", f"message-{copy.pk}"
                )
                expect(
                    panel.get_by_role("link", name="Open evidence")
                ).to_have_attribute("href", self.evidence_path(copy))

    @override_settings(PANDECT_AI_GATEWAY=CONFIG)
    def test_owner_opens_summary_review_for_the_expanded_instance(self):
        external_grant_kwargs(self.case, "session_summaries")
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.owner)
                page.goto(self.live_server_url + self.reader_path + "?tab=instances")
                if engine == self.engines[0]:
                    expect(
                        page.get_by_role("heading", name="No saved sessions")
                    ).to_be_visible()
                    page.get_by_role("link", name="Group this conversation").press(
                        "Enter"
                    )
                    expect(
                        page.get_by_role("heading", name="Group this conversation")
                    ).to_be_visible()
                    expect(page.get_by_label("Minutes between groups")).to_have_value(
                        "60"
                    )
                    self.capture(page, engine, "group-conversation-review")
                    page.get_by_role("link", name="Cancel").press("Enter")
                    self.db(self.save_sessions, self.messages[:2])
                    page.reload()
                page.get_by_role("link", name="Show messages").press("Enter")
                page.get_by_role("link", name="Request summary").press("Enter")
                expect(
                    page.get_by_role("heading", name="Request session summary")
                ).to_be_visible()
                expect(
                    page.get_by_text(
                        f"External AI · {CONFIG['models']['text']}", exact=True
                    )
                ).to_be_visible()
                page.get_by_text(
                    "Messages included in this summary", exact=False
                ).press("Enter")
                expect(
                    page.get_by_role("link", name=self.messages[0].title)
                ).to_be_visible()
                expect(
                    page.get_by_role("link", name=self.messages[1].title)
                ).to_be_visible()
                page.get_by_role("link", name="Cancel").press("Enter")
                expect(page.locator(".instance-message-list li")).to_have_count(2)
                self.assertFalse(
                    self.db(
                        AnalysisRun.objects.filter(operation="session_summaries").exists
                    )
                )

    @override_settings(PANDECT_AI_GATEWAY=CONFIG)
    def test_owner_requests_all_instance_summaries_without_javascript(self):
        external_grant_kwargs(self.case, "session_summaries")
        self.save_sessions(self.messages[:2])
        self.save_sessions(self.messages[30:32])
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.owner)
                page.goto(self.live_server_url + self.reader_path + "?tab=instances")
                action = page.get_by_role("link", name="Summarize missing sessions")
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assert_reflow(page, width)
                    self.capture(page, engine, f"instances-owner-{width}-{height}")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "instances-owner-dark")
                page.emulate_media(forced_colors="active")
                action.focus()
                expect(action).to_be_focused()
                self.capture(page, engine, "instances-owner-forced-colors")
                page.emulate_media(color_scheme="light", forced_colors="none")
                action.press("Enter")
                expect(
                    page.get_by_role("heading", name="Summarize missing sessions")
                ).to_be_visible()
                expect(
                    page.get_by_text("Sessions included · 2", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_text(
                        f"External AI · {CONFIG['models']['text']}", exact=True
                    )
                ).to_be_visible()
                self.assert_reflow(page, 320)
                self.capture(page, engine, "instances-bulk-review-mobile")
                page.get_by_role("link", name="Cancel").press("Enter")
                expect(action).to_be_visible()
                action.press("Enter")
                page.get_by_role("button", name="Request summaries").press("Enter")
                expect(
                    page.get_by_role("heading", name="Processing", exact=True)
                ).to_be_visible()
                expect(page.get_by_text("Summaries are being queued.")).to_be_visible()
                page.get_by_role("link", name="Back to sessions & summaries").press(
                    "Enter"
                )
                expect(action).to_be_visible()
                action.press("Enter")
                expect(
                    page.get_by_text("Instance summaries are already in progress.")
                ).to_be_visible()
                self.db(CaseAnalysisBatch.objects.filter(case=self.case).delete)

    def test_reader_selection_history_groupings_reflow_and_keyboard(self):
        self.save_sessions(self.messages[:4])
        self.save_sessions(self.messages[3:6])
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                url = self.live_server_url + self.reader_path
                page.goto(url)
                page.evaluate("window.conversationDocument = document")
                rows = page.locator(".conversation-message")
                first = rows.nth(0)
                second = rows.nth(1)
                conflict = rows.nth(3)
                first_link = first.locator("[data-message-select]")
                second_link = second.locator("[data-message-select]")
                panel = page.get_by_role("complementary", name="Message details")
                expect(panel).to_be_hidden()
                expect(first.get_by_text("Session starts", exact=False)).to_be_visible()
                tint = first.get_attribute("data-session-tint")
                self.assertTrue(tint)
                expect(second).to_have_attribute("data-session-tint", tint)
                expect(
                    conflict.get_by_text("Multiple groupings", exact=True)
                ).to_be_visible()
                self.assertIsNone(conflict.get_attribute("data-session-tint"))
                expect(
                    rows.nth(5).get_by_text("Session ends", exact=False)
                ).to_be_visible()
                self.assertIsNone(rows.nth(6).get_attribute("data-session-tint"))

                # A selection drag must leave the original available for copying.
                original = first.locator(".conversation-message-body")
                self.assertEqual(original.text_content(), self.messages[0].body)
                original.scroll_into_view_if_needed()
                box = original.bounding_box()
                x, y = box["x"] + 2, box["y"] + 9
                page.mouse.move(x, y)
                page.mouse.down()
                page.mouse.move(x + 90, y, steps=12)
                page.mouse.up()
                self.assertIn(
                    "Original", page.evaluate("window.getSelection().toString()")
                )
                expect(panel).to_be_hidden()
                page.evaluate("window.getSelection().removeAllRanges()")

                first_link.press("Enter")
                expect(page.locator("#message-panel-title")).to_be_focused()
                expect(first_link).to_have_attribute("aria-current", "true")
                body = panel.locator(".conversation-message-body")
                self.assertEqual(
                    body.text_content(),
                    self.messages[0].body,
                )
                first_url = page.url
                second.locator(".conversation-message-body").click()
                expect(second_link).to_have_attribute("aria-current", "true")
                expect(body).to_have_text(self.messages[1].body)
                second_url = page.url
                self.assertNotEqual(first_url, second_url)
                page.go_back()
                expect(page).to_have_url(first_url)
                expect(first_link).to_have_attribute("aria-current", "true")
                expect(body).to_have_text(self.messages[0].body)
                page.go_forward()
                expect(page).to_have_url(second_url)
                expect(second_link).to_have_attribute("aria-current", "true")
                expect(body).to_have_text(self.messages[1].body)
                page.keyboard.press("Escape")
                expect(panel).to_be_hidden()
                expect(second_link).to_be_focused()
                self.assertTrue(
                    page.evaluate("window.conversationDocument === document")
                )
                self.assertNotIn("message", parse_qs(urlsplit(page.url).query))
                conflict.locator("[data-message-select]").click()
                expect(body).to_have_text(self.messages[3].body)
                groupings = panel.get_by_text("Multiple groupings", exact=True)
                sessions = panel.locator(".conversation-session-groups")
                expect(sessions).to_be_hidden()
                groupings.press("Enter")
                expect(sessions).to_be_visible()
                expect(sessions.get_by_role("link")).to_have_count(2)
                panel.locator("[data-message-close]").click()
                expect(conflict.locator("[data-message-select]")).to_be_focused()

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
                    first_link.click()
                    self.assert_reflow(page, width)
                    self.capture(page, engine, f"message-panel-{width}-{height}")
                    if width < 768:
                        expect(first_link).to_be_hidden()
                        close = panel.get_by_role("link", name="Back to messages")
                    else:
                        close = panel.locator("[data-message-close]")
                    close.click()
                    expect(first_link).to_be_focused()
                    expect(first_link).to_be_in_viewport()
                page.goto(
                    self.live_server_url
                    + self.reader_path
                    + f"?message={self.manual_message.pk}"
                    + f"#message-{self.manual_message.pk}"
                )
                expect(panel).to_be_visible()
                self.assert_reflow(page, 320)
                self.capture(page, engine, "unknown-time-long-identifier-320")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.goto(first_url)
                expect(panel).to_be_visible()
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "reader-dark-reduced-motion")
                page.emulate_media(forced_colors="active")
                first_link.focus()
                expect(first_link).to_be_focused()
                expect(first.get_by_text("Session starts", exact=False)).to_be_visible()
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
            operation="content_events",
            method_version=get_operation("content_events").version,
            inputs=inputs,
            status="partial",
        )
        items = [
            {
                "label": f"Reported sender **{first.sender_id}**"
                if number == 1
                else f"Retained observation {number + 1:02}",
                "text": (
                    f"Reported sender **{first.sender_id}** suggests "
                    f"a possible meeting with reported recipient {first.recipient_id}."
                    if number == 1
                    else f"Literal wording observation {number + 1:02}: "
                    "<script> remains quoted text, not executable content."
                    + (
                        " This synthetic observation records the wording of the"
                        " first retained message and refers the reader to the"
                        " source for the complete exchange. End of retained"
                        " observation."
                        if number == 0
                        else ""
                    )
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
            operation="conversation_link",
            method_version=get_operation("conversation_link").version,
            inputs=shared_inputs,
            status="complete",
            parameters={"automatic_batch": str(first.pk), "import_run_id": str(run.pk)},
        )
        self.shared_run = shared_run
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
                        "values": {
                            "status": "unresolved",
                            "native_thread_references": [
                                f"{self.export.pk}:synthetic-thread"
                            ],
                        },
                    }
                    for number in range(27)
                ],
            },
        )
        project_result(shared_result)

    @patch("core.analysis.ranking.MAX_MATCH_KEYS", 1)
    def test_open_message_loads_its_ranked_panel_when_visible_keys_exceed_budget(self):
        first, peer, other = self.messages[:3]
        inputs = snapshot_sources(
            self.case,
            [
                {"kind": "text-message", "id": message.pk}
                for message in (first, peer, other)
            ],
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="entities",
            method_version=get_operation("entities").version,
            inputs=inputs,
            status="complete",
        )
        items = [
            {
                "label": "Background",
                "text": "Routine detail",
                "source_ids": [inputs[0]["id"]],
            },
            {
                "label": "Identifier candidate",
                "text": "BOOK-7418",
                "source_ids": [inputs[0]["id"]],
            },
            {
                "label": "Identifier candidate",
                "text": "BOOK-7418",
                "source_ids": [inputs[1]["id"]],
            },
            {
                "label": "Email",
                "text": "contact@example.test",
                "source_ids": [inputs[2]["id"]],
            },
        ]
        project_result(
            AnalysisResult.objects.create(
                case=self.case, run=run, data={"items": items}
            )
        )
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                page.goto(self.live_server_url + self.reader_path)
                row = page.locator(f"#message-{first.pk}")
                expect(row).to_have_attribute("data-load-panel-on-open", "")
                row.locator("[data-message-select]").click()
                panel = page.locator("[data-message-panel]")
                top = panel.locator(".conversation-finding").first
                expect(top).to_be_visible()
                expect(top).to_contain_text("BOOK-7418")
                expect(top).to_contain_text("another original")
                expect(row).not_to_have_attribute("data-load-panel-on-open", "")
                page.go_back()
                expect(row).to_have_attribute("data-load-panel-on-open", "")
                page.go_forward()
                expect(top).to_contain_text("another original")

    def test_saved_findings_without_javascript_paging_and_message_links(self):
        self.make_saved_findings()
        run_count = AnalysisRun.objects.count()
        first, target = self.messages[0], self.messages[51]
        url = self.live_server_url + self.reader_path + "?list_page=2"
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(url)
                row = page.locator(f"#message-{first.pk}")
                expect(row).not_to_contain_text("Literal wording observation")
                shared = page.locator("#conversation-analysis")
                shared_summary = shared.locator("summary")
                expect(shared_summary).to_have_text("Conversation findings (27)")
                expect(shared.locator(".conversation-finding").first).to_be_hidden()
                shared_summary.press("Enter")
                expect(shared.locator(".conversation-finding")).to_have_count(3)
                expect(shared.locator(".conversation-finding").first).to_be_visible()
                expect(
                    shared.get_by_role("link", name="Show message", exact=False).first
                ).to_be_visible()
                page.set_viewport_size({"width": 390, "height": 844})
                shared_summary.scroll_into_view_if_needed()
                self.assert_reflow(page, 390)
                self.capture(page, engine, "shared-findings-preview-mobile")
                page.set_viewport_size({"width": 1440, "height": 900})
                row.locator("[data-message-select]").press("Enter")
                panel = page.get_by_role("complementary", name="Message details")
                inline = panel.locator("[data-message-analysis]")
                shared = page.locator("#conversation-analysis")
                summary = inline.locator("summary")
                expect(summary).to_have_text("Findings · 28")
                findings = inline.locator(".conversation-finding")
                expect(inline).to_have_attribute("open", "")
                expect(findings.first).to_be_visible()
                expect(findings).to_have_count(3)
                expect(findings.nth(0)).not_to_contain_text("AI suggestion")
                expect(findings.nth(0)).to_contain_text("<script>")
                expect(findings.nth(0).locator("script")).to_have_count(0)
                excerpt = findings.first.get_by_text(
                    re.compile(r"^Literal wording observation 01:.*…$")
                )
                expect(excerpt).to_be_visible()
                self.assertLessEqual(len(excerpt.inner_text()), 160)
                full_text_end = "End of retained observation."
                expect(findings.first).not_to_contain_text(full_text_end)
                expect(inline).not_to_contain_text("Suggest claims and commitments")
                expect(findings.nth(1)).to_contain_text("AI suggestion")
                expect(findings.nth(1)).to_contain_text("Alex suggests")
                expect(findings.nth(1)).not_to_contain_text("Reported sender Alex")
                expect(findings.nth(1)).to_contain_text("reported recipient Blair")
                expect(findings.nth(1)).not_to_contain_text(str(first.sender_id))
                expect(findings.nth(1)).not_to_contain_text("Rejected")
                expect(findings.nth(1)).not_to_contain_text("Whole result: Unresolved")
                expect(findings.nth(1)).to_contain_text("Partial coverage")
                expect(inline).not_to_contain_text("Shared exchange")
                summary.scroll_into_view_if_needed()
                self.capture(page, engine, "saved-findings-panel")
                page.set_viewport_size({"width": 390, "height": 844})
                summary.scroll_into_view_if_needed()
                self.assert_reflow(page, 390)
                self.capture(page, engine, "saved-findings-preview-mobile")
                page.set_viewport_size({"width": 1440, "height": 900})
                findings.first.get_by_role(
                    "link", name="Open finding: Retained observation 01", exact=True
                ).click()
                expect(page.locator(".analysis-finding")).to_contain_text(full_text_end)
                page.go_back()
                inline.get_by_role("link", name="More findings", exact=False).click()
                params = parse_qs(urlsplit(page.url).query)
                self.assertNotIn("source", params)
                self.assertEqual(params["list_page"], ["2"])
                self.assertEqual(params["message"], [str(first.pk)])
                self.assertEqual(params["analysis_page"], ["1"])
                expect(inline).to_have_attribute("open", "")
                expect(findings).to_have_count(25)
                expect(findings.first).to_contain_text(full_text_end)
                expect(inline).not_to_contain_text("Suggest claims and commitments")
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
                expect(inline).to_be_visible()
                expect(findings).to_have_count(25)
                shared.locator("summary").press("Enter")
                shared.get_by_role("link", name="More findings", exact=True).press(
                    "Enter"
                )
                expect(shared.locator(".conversation-finding")).to_have_count(25)
                shared.get_by_role("link", name="Next findings", exact=True).click()
                expect(inline).to_contain_text("Alex suggests")
                expect(inline).not_to_contain_text(str(first.sender_id))
                expect(shared.locator(".conversation-finding")).to_have_count(2)
                expect(findings).to_have_count(25)
                shared.get_by_role("link", name="Previous findings", exact=True).click()
                shared.locator(f'a[href*="message={target.pk}"]').first.click()
                expect(page.locator(f"#message-{target.pk}")).to_be_in_viewport()
                expect(panel).to_be_visible()
                expect(panel.locator(".conversation-message-body")).to_have_text(
                    target.body
                )
                params = parse_qs(urlsplit(page.url).query)
                self.assertEqual(params["message"], [str(target.pk)])
                self.assertNotIn("source", params)
                self.assertEqual(params["list_page"], ["2"])
                self.assertEqual(self.db(AnalysisRun.objects.count), run_count)

                page.goto(
                    self.url("core:analysis:browse", case_id=self.case.pk)
                    + "?method=content_events"
                )
                suggestion = page.locator(".analysis-finding-rows > li").filter(
                    has_text="Alex suggests"
                )
                expect(suggestion).not_to_contain_text("Reported sender Alex")
                expect(suggestion).not_to_contain_text(str(first.sender_id))
                self.capture(page, engine, "participant-names-catalogue")
                suggestion.get_by_role("link", name=re.compile(r"^Alex\b")).click()
                expect(
                    page.locator(".analysis-finding").get_by_role(
                        "heading", name="AI suggestion · Alex", exact=True
                    )
                ).to_be_visible()
                text = page.locator(".analysis-finding-text")
                expect(text).to_contain_text("Alex suggests")
                expect(text).to_contain_text("reported recipient Blair")
                expect(text).not_to_contain_text(str(first.sender_id))
                self.capture(page, engine, "participant-names-detail")

                page.goto(
                    self.url(
                        "core:analysis:detail",
                        case_id=self.case.pk,
                        run_id=self.shared_run.pk,
                    )
                    + "?finding=1"
                )
                thread = page.get_by_text(
                    "Synthetic message export · Thread synthetic-thread", exact=True
                )
                if not thread.is_visible():
                    page.locator("#finding-1").get_by_text(
                        "Finding details", exact=True
                    ).press("Enter")
                expect(thread).to_be_visible()
                assumptions = page.locator(".analysis-run-details")
                expect(assumptions).not_to_contain_text(str(first.pk))
                self.capture(page, engine, "readable-session-references")

    def test_saved_findings_reflow_focus_and_scrolling_preserve_context(self):
        self.make_saved_findings()
        first = self.messages[0]
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                url = (
                    self.live_server_url
                    + self.reader_path
                    + "?list_page=2"
                    + f"&message={first.pk}&analysis_page=1"
                    + "&conversation_analysis_page=1"
                    + f"#message-analysis-{first.pk}"
                )
                page.goto(url)
                panel = page.get_by_role("complementary", name="Message details")
                inline = panel.locator("[data-message-analysis]")
                shared = page.locator("#conversation-analysis")
                summary = inline.locator("summary")
                expect(inline).to_have_attribute("open", "")
                expect(shared.locator(".conversation-finding")).to_have_count(25)
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
                close = panel.locator("[data-message-close]")
                close.focus()
                expect(close).to_be_focused()
                self.capture(page, engine, "saved-findings-dark-reduced-motion")
                page.emulate_media(forced_colors="active")
                expect(inline).to_be_visible()
                expect(close).to_be_focused()
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
                        "list_page": ["2"],
                        "page": ["2"],
                    },
                )
                expect(page.locator(".conversation-message")).to_have_count(55)
                expect(page).to_have_url(url)
