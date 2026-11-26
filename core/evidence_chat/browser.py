"""Shared chat from a question through clarification, source inspection and notes."""

import json
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from django.core.files.base import ContentFile
from django.db import connections
from django.test import override_settings
from django.urls import reverse
from playwright.sync_api import expect

from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase
from core.analysis.errors import AnalysisError
from core.analysis.services import process_run
from core.evidence_chat import ai, jobs, services
from core.evidence_chat.tests.test_decisions_worker import answer_from_quotes
from core.evidence_chat.tests.test_engine import decision
from core.evidence_chat.tests.test_views import LOCAL_AI
from core.models import AnalysisRun, ChatTurn
from document.models import DocumentEvidence
from document.tests.test_analysis import pdf_bytes
from text.models import TextEvidence


@override_settings(PANDECT_LOCAL_AI=LOCAL_AI)
class ChatBrowserTests(BrowserTestCase):
    artifact_subdirectory = "evidence-chat"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("chat-owner")

    def _finish(self, case):
        turn = ChatTurn.objects.get(case=case, status="queued")
        with patch.object(ai, "infer", side_effect=self._respond):
            jobs.process_turn(turn.pk, turn.delivery_token)
        turn.refresh_from_db()
        self.assertEqual(turn.status, "complete", turn.error_code)

    @staticmethod
    def _respond(turn, authorize, prompt, schema, *, clarify=True):
        authorize()
        data = json.loads(prompt.split("\n")[-1])
        if clarify and not data["history"]:
            return {
                "action": "ask",
                "question": "Which pickup do you mean?",
                "clarification": "",
                "claims": [],
                "lead": "",
            }
        quote = "The harbour pickup moved to Friday."
        record = next(item for item in data["records"] if quote in item["text"])
        return {
            "action": "answer",
            "question": "",
            "clarification": "",
            "lead": "Check whether the dispatcher confirmed the new date.",
            "claims": [
                {
                    "text": "The witness reported a change to Friday.",
                    "citations": [
                        {
                            "id": record["id"],
                            "quote": quote,
                            "start": record["text"].index(quote),
                        }
                    ],
                }
            ],
        }

    def _source(self, case):
        return TextEvidence.objects.create(
            case=case,
            uploaded_by=self.owner,
            title="Witness statement",
            body="The harbour pickup moved to Friday.",
        )

    def _finish_count(self, case):
        turn = ChatTurn.objects.get(case=case, status="queued")
        for _ in range(5):
            jobs.process_turn(turn.pk, turn.delivery_token)
            turn.refresh_from_db()
            if turn.status == "complete":
                return
        self.fail(f"Count stopped in {turn.status} ({turn.error_code}).")

    def test_approve_one_pdf_and_read_the_linked_answer_without_javascript(self):
        quote = "Parcel collected at 15:15."

        def respond(turn, authorize, prompt, schema):
            authorize()
            records = json.loads(prompt.split("\n", 1)[1])["records"]
            if any(quote in record["text"] for record in records):
                return answer_from_quotes(records, [quote])
            document = next(r for r in records if r["type"] == "original/document")
            return decision(
                "propose_extraction", missing="Read the collection time on the receipt."
            ) | {"source": document["id"]}

        def ask(case):
            turn = services.submit_turn(
                self.owner,
                case.pk,
                "What time does the parcel receipt show?",
                submission_id=uuid4(),
            )
            jobs.process_turn(turn.pk, turn.delivery_token)
            turn.refresh_from_db()
            return turn

        def finish(case):
            run = AnalysisRun.objects.get(case=case, operation="native_text")
            process_run(run.pk)
            child = ChatTurn.objects.get(case=case, status="queued")
            jobs.process_turn(child.pk, child.delivery_token)

        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                patch.object(ai, "infer", side_effect=respond),
            ):
                case = self.db(
                    Case.objects.create, owner=self.owner, name="Parcel collection"
                )
                self.db(
                    DocumentEvidence.objects.create,
                    case=case,
                    uploaded_by=self.owner,
                    title="Parcel receipt",
                    file=ContentFile(pdf_bytes(quote), name="receipt.pdf"),
                )
                with self.browser_page(engine, java_script_enabled=False) as (_, page):
                    self.sign_in(page)
                    for approve in (False, True):
                        turn = self.db(ask, case)
                        reply = self.url(
                            "core:chat:reply",
                            case_id=case.pk,
                            chat_id=turn.chat_id,
                            turn_id=turn.pk,
                        )
                        page.goto(reply)
                        page.get_by_role("link", name="Review proposed check").press(
                            "Enter"
                        )
                        expect(
                            page.get_by_role("heading", name="Extract document text")
                        ).to_be_visible()
                        if approve:
                            for width, height in [
                                (1440, 900),
                                (1440, 1024),
                                (768, 1024),
                                (390, 844),
                                (320, 800),
                            ]:
                                page.set_viewport_size(
                                    {"width": width, "height": height}
                                )
                                self.assertLessEqual(
                                    page.evaluate(
                                        "document.documentElement.scrollWidth - "
                                        "document.documentElement.clientWidth"
                                    ),
                                    1,
                                )
                                if width in (1440, 390) and height != 1024:
                                    self.capture(page, engine, f"pdf-check-{width}")
                        button = page.get_by_role(
                            "button",
                            name="Approve and run"
                            if approve
                            else "Keep current answer",
                        )
                        button.press("Enter")
                        expect(page).to_have_url(reply)
                        if not approve:
                            self.assertFalse(
                                self.db(AnalysisRun.objects.filter(case=case).exists)
                            )
                    self.db(finish, case)
                    page.reload()
                    page.get_by_role(
                        "link", name="Open linked reply after extraction"
                    ).press("Enter")
                    expect(page.get_by_text(quote, exact=True).first).to_be_visible()
                    page.get_by_text(
                        "Model usage for this question and check", exact=True
                    ).press("Enter")
                    expect(
                        page.get_by_text("Local PDF extraction", exact=True)
                    ).to_be_visible()
                    expect(page.get_by_text("Total so far", exact=True)).to_be_visible()

    @override_settings(PANDECT_DIRECT_AI={"openai_api_key": "synthetic-key"})
    def test_owner_approves_and_revokes_routing_without_javascript(self):
        case = self.db(Case.objects.create, owner=self.owner, name="Routing review")
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("cases:case_settings", case_id=case.pk))
                section = page.get_by_role(
                    "region", name="Evidence chat source selection"
                )
                approve = section.get_by_role(
                    "button", name="Approve OpenAI source selection"
                )
                expect(section).to_contain_text("gpt-6-luna")
                approve.focus()
                expect(approve).to_be_focused()
                section.scroll_into_view_if_needed()
                self.capture(page, engine, "routing-desktop")
                for width, height in [
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 800),
                ]:
                    page.set_viewport_size({"width": width, "height": height})
                    expect(approve).to_be_visible()
                    self.assertLessEqual(
                        section.evaluate("node => node.scrollWidth - node.clientWidth"),
                        1,
                    )
                    if width == 390:
                        section.scroll_into_view_if_needed()
                        self.capture(page, engine, "routing-mobile")
                approve.press("Enter")
                expect(
                    section.get_by_text("Approved for this case.", exact=True)
                ).to_be_visible()
                page.emulate_media(
                    color_scheme="dark", reduced_motion="reduce", forced_colors="active"
                )
                revoke = section.get_by_role(
                    "button", name="Revoke OpenAI source selection"
                )
                revoke.focus()
                revoke.press("Enter")
                expect(approve).to_be_visible()

    def test_live_activity_shows_progress_and_linked_evidence(self):
        for browser in self.engines:
            with self.subTest(browser=browser):
                case = self.db(
                    Case.objects.create,
                    owner=self.owner,
                    name=f"Harbour activity ({browser})",
                )
                source = self.db(self._source, case)
                with self.browser_page(browser) as (_, page):
                    self.sign_in(page)
                    page.goto(self.url("core:chat:list", case_id=case.pk))
                    page.get_by_role("textbox", name="Question", exact=True).fill(
                        "When did the harbour pickup move?"
                    )
                    page.get_by_role("button", name="Ask question").press("Enter")
                    activity = page.locator("details.chat-activity")
                    summary = activity.locator("summary")
                    expect(summary).to_have_text("Queued")
                    summary.press("Enter")
                    expect(activity).to_have_js_property("open", True)
                    expect(summary).to_be_focused()

                    turn = self.db(ChatTurn.objects.get, case=case, status="queued")
                    release = Event()

                    def paused_answer(*args, release=release):
                        response = self._respond(*args, clarify=False)
                        if not release.wait(timeout=60):
                            raise TimeoutError(
                                "Browser did not release the mock answer"
                            )
                        return response

                    def process(turn_id, token):
                        try:
                            jobs.process_turn(turn_id, token)
                        finally:
                            connections.close_all()

                    with patch.object(ai, "infer", side_effect=paused_answer):
                        work = self.database_worker.submit(
                            process, turn.pk, turn.delivery_token
                        )
                        try:
                            expect(summary).to_contain_text("Preparing an answer")
                            expect(summary).to_be_focused()
                            expect(activity).to_have_js_property("open", True)
                            expect(
                                activity.locator('[aria-current="step"]')
                            ).to_contain_text("Preparing an answer")
                            expect(
                                activity.get_by_text("Searched case evidence")
                            ).to_be_visible()
                            evidence = activity.get_by_role(
                                "link", name="Text · Witness statement", exact=True
                            )
                            expect(evidence).to_be_visible()
                            expect(evidence).to_have_attribute(
                                "href",
                                reverse(
                                    "core:evidence:detail",
                                    args=(case.pk, "text", source.pk),
                                ),
                            )
                            self.capture(page, browser, "desktop-activity")
                            page.emulate_media(
                                color_scheme="dark", reduced_motion="reduce"
                            )
                            self.capture(page, browser, "desktop-activity-dark")
                            for width, height in [
                                (1440, 1024),
                                (768, 1024),
                                (390, 844),
                                (320, 800),
                            ]:
                                page.set_viewport_size(
                                    {"width": width, "height": height}
                                )
                                self.assertLessEqual(
                                    page.evaluate(
                                        "document.documentElement.scrollWidth"
                                    ),
                                    page.evaluate(
                                        "document.documentElement.clientWidth"
                                    )
                                    + 1,
                                )
                                if width == 390:
                                    evidence.scroll_into_view_if_needed()
                                    self.capture(page, browser, "mobile-activity")
                            page.evaluate(
                                "document.documentElement.style.fontSize = '200%'"
                            )
                            page.emulate_media(forced_colors="active")
                            self.assertLessEqual(
                                page.evaluate("document.documentElement.scrollWidth"),
                                page.evaluate("document.documentElement.clientWidth")
                                + 1,
                            )
                            evidence.focus()
                            expect(evidence).to_be_focused()
                        finally:
                            release.set()
                            work.result(timeout=15)

                    expect(summary).to_have_text("Review activity")
                    expect(activity).to_have_js_property("open", True)
                    expect(activity.get_by_text("Worked on a reply")).to_be_visible()
                    expect(evidence).to_be_focused()
                    expect(
                        page.get_by_text("The witness reported a change to Friday.")
                    ).to_be_visible()

    def test_live_full_review_and_paged_evidence(self):
        for browser in self.engines:
            with self.subTest(browser=browser):
                case = self.db(
                    Case.objects.create,
                    owner=self.owner,
                    name=f"Witnesses ({browser})",
                )
                self.db(self._source, case)
                with self.browser_page(browser) as (_, page):
                    self.sign_in(page, self.owner)
                    page.goto(self.url("core:chat:list", case_id=case.pk))
                    page.get_by_role("textbox", name="Question", exact=True).fill(
                        "How many text records are there?"
                    )
                    page.get_by_role("button", name="Ask question").press("Enter")
                    expect(page.get_by_text("Queued", exact=True)).to_be_visible()
                    expect(
                        page.get_by_role("button", name="Ask follow-up")
                    ).to_have_count(0)
                    self.db(self._finish_count, case)
                    matches = page.get_by_role(
                        "link", name="Open reply and cited matches"
                    )
                    expect(matches).to_be_visible(timeout=4500)
                    expect(
                        page.get_by_role("button", name="Ask follow-up")
                    ).to_be_visible()
                    matches.press("Enter")
                    expect(
                        page.get_by_role("heading", name="Cited matches")
                    ).to_be_visible()
                    expect(
                        page.get_by_text("1 cited match", exact=True)
                    ).to_be_visible()
                    page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                    page.set_viewport_size({"width": 320, "height": 800})
                    page.evaluate("document.documentElement.style.fontSize = '200%'")
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"),
                        page.evaluate("document.documentElement.clientWidth") + 1,
                    )
                    page.emulate_media(forced_colors="active")
                    expect(
                        page.get_by_role("link", name="Ask a follow-up")
                    ).to_be_visible()

    def test_clarify_open_source_mark_and_reopen(self):
        viewer = self.make_user("chat-viewer")
        for browser in self.engines:
            with self.subTest(browser=browser):
                case = self.db(
                    Case.objects.create,
                    owner=self.owner,
                    name=f"Harbour case ({browser})",
                )
                self.db(
                    TextEvidence.objects.create,
                    case=case,
                    uploaded_by=self.owner,
                    title="Pickup schedule",
                    body="There were two pickups, one at the harbour and one inland.",
                )
                self.db(CaseMembership.objects.create, case=case, user=viewer)
                with self.browser_page(browser, java_script_enabled=False) as (_, page):
                    self.sign_in(page, self.owner)
                    page.goto(self.url("core:analysis:index", case_id=case.pk))
                    page.get_by_role("link", name="Evidence chat", exact=True).press(
                        "Enter"
                    )
                    expect(
                        page.get_by_role("checkbox", name="Include archived evidence")
                    ).to_have_count(0)
                    page.get_by_role("textbox", name="Question", exact=True).fill(
                        "When was the pickup?"
                    )
                    page.get_by_role("button", name="Ask question").press("Enter")
                    expect(page.get_by_text("Queued", exact=True)).to_be_visible()
                    self.db(self._finish, case)
                    page.get_by_role("link", name="Refresh status").press("Enter")
                    expect(
                        page.get_by_text("Which pickup do you mean?", exact=True)
                    ).to_be_visible()
                    activity = page.locator("details.chat-activity")
                    activity.locator("summary").press("Enter")
                    expect(
                        activity.get_by_text("Searched case evidence")
                    ).to_be_visible()
                    self.db(self._source, case)
                    page.get_by_role("link", name="Jump to follow-up").press("Enter")
                    page.wait_for_url("**/#follow-up")
                    page.get_by_role("textbox", name="Question", exact=True).fill(
                        "The harbour pickup."
                    )
                    page.get_by_role("button", name="Ask follow-up").press("Enter")
                    expect(page.get_by_text("Queued", exact=True)).to_be_visible()
                    turn = self.db(ChatTurn.objects.get, case=case, status="queued")
                    with patch.object(
                        ai, "infer", side_effect=AnalysisError("local_ai_unavailable")
                    ):
                        self.db(jobs.process_turn, turn.pk, turn.delivery_token)
                    page.get_by_role("link", name="Refresh status").press("Enter")
                    failed = page.locator(".chat-turn").last
                    expect(
                        failed.get_by_text("Needs attention", exact=True)
                    ).to_be_visible()
                    failed.get_by_role("link", name="Open reply", exact=True).press(
                        "Enter"
                    )
                    page.get_by_role("button", name="Retry question").press("Enter")
                    expect(page.get_by_text("Queued", exact=True)).to_be_visible()
                    self.db(self._finish, case)
                    page.get_by_role("link", name="Refresh status").press("Enter")
                    quote = page.get_by_text(
                        "The harbour pickup moved to Friday.", exact=True
                    )
                    expect(quote).to_be_visible()
                    page.get_by_role("link", name="Back to conversation").press("Enter")
                    conversation_url = self.url(
                        "core:chat:detail", case_id=case.pk, chat_id=turn.chat_id
                    )
                    cite = page.get_by_role("link", name="Reference 1:", exact=False)
                    expect(cite).to_be_visible()
                    expect(quote).to_be_hidden()
                    sources = page.locator("summary").filter(has_text="Sources (1)")
                    sources.press("Enter")
                    expect(quote).to_be_visible()
                    transcript = page.get_by_role("region", name="Conversation")
                    first_turn = transcript.locator(".chat-turn").first
                    question = first_turn.locator(".chat-question .chat-bubble")
                    answer = first_turn.locator(".chat-reply .chat-answer")
                    expect(
                        question.get_by_text("When was the pickup?", exact=True)
                    ).to_be_visible()
                    expect(answer).to_have_text("Which pickup do you mean?")
                    question_box = question.bounding_box()
                    answer_box = answer.bounding_box()
                    self.assertGreater(question_box["x"], answer_box["x"])
                    self.assertGreaterEqual(
                        answer_box["y"], question_box["y"] + question_box["height"]
                    )
                    composer = page.get_by_role("textbox", name="Question", exact=True)
                    expect(transcript).to_have_attribute("tabindex", "0")
                    cite.focus()
                    expect(cite).to_be_focused()
                    expect(cite).to_be_in_viewport(ratio=1)
                    expect(composer).to_be_in_viewport(ratio=1)
                    self.assertGreaterEqual(
                        page.locator("#follow-up").bounding_box()["y"],
                        transcript.bounding_box()["y"]
                        + transcript.bounding_box()["height"],
                    )
                    self.capture(page, browser, "desktop-answer")
                    page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                    self.capture(page, browser, "desktop-answer-dark")
                    page.emulate_media(color_scheme="light")
                    for width, height in [
                        (1440, 1024),
                        (768, 1024),
                        (390, 844),
                        (320, 800),
                    ]:
                        page.set_viewport_size({"width": width, "height": height})
                        self.assertLessEqual(
                            page.evaluate("document.documentElement.scrollWidth"),
                            page.evaluate("document.documentElement.clientWidth") + 1,
                        )
                        expect(composer).to_be_visible()
                        if width == 390:
                            cite.scroll_into_view_if_needed()
                            self.capture(page, browser, "mobile-transcript")
                            page.locator("#follow-up").scroll_into_view_if_needed()
                            self.capture(page, browser, "mobile-composer")
                    page.emulate_media(color_scheme="dark")
                    page.evaluate("document.documentElement.style.fontSize = '200%'")
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"),
                        page.evaluate("document.documentElement.clientWidth") + 1,
                    )
                    page.emulate_media(forced_colors="active")
                    expect(composer).to_be_visible()
                    page.emulate_media(
                        color_scheme="light",
                        reduced_motion="no-preference",
                        forced_colors="none",
                    )
                    page.evaluate("document.documentElement.style.fontSize = ''")
                    page.set_viewport_size({"width": 1440, "height": 900})
                    cite.press("Enter")
                    expect(
                        page.get_by_role(
                            "heading", name="Witness statement", exact=True
                        )
                    ).to_be_visible()
                    page.go_back()
                    expect(
                        page.get_by_role("heading", name="Suggested next check")
                    ).to_be_visible()
                    reply = page.locator(".chat-turn").last
                    reply.get_by_role(
                        "button", name="Mark important", exact=True
                    ).press("Enter")
                    expect(
                        page.get_by_role("heading", name="Evidence chat reply")
                    ).to_be_visible()
                    page.goto(self.url("core:important:list", case_id=case.pk))
                    page.get_by_role(
                        "link", name="The harbour pickup.", exact=True
                    ).press("Enter")
                    expect(
                        page.get_by_role("link", name="Back to Important", exact=True)
                    ).to_be_visible()
                    for width, height in [
                        (1440, 1024),
                        (768, 1024),
                        (390, 844),
                        (320, 800),
                    ]:
                        page.set_viewport_size({"width": width, "height": height})
                        self.assertLessEqual(
                            page.evaluate("document.documentElement.scrollWidth"),
                            page.evaluate("document.documentElement.clientWidth") + 1,
                        )
                        if width == 390:
                            self.capture(page, browser, "mobile-reply")

                    page.set_viewport_size({"width": 1440, "height": 900})
                    page.goto(conversation_url)
                    page.get_by_role("link", name="New chat", exact=True).press("Enter")
                    saved = page.locator("#saved-chats")
                    original = saved.get_by_role("link", name="When was the pickup?")
                    expect(original).to_be_visible()
                    self.capture(page, browser, "desktop-new-chat")
                    composer.fill("When was the inland pickup?")
                    page.get_by_role("button", name="Ask question").press("Enter")
                    expect(
                        saved.get_by_role("link", name="When was the inland pickup?")
                    ).to_have_attribute("aria-current", "page")
                    original.press("Enter")
                    expect(original).to_have_attribute("aria-current", "page")
                    expect(
                        page.get_by_role(
                            "heading", level=1, name="When was the pickup?"
                        )
                    ).to_be_visible()
                    self.db(
                        Case.objects.filter(pk=case.pk).update,
                        name=("Harbour pickup and dispatch records " * 7).strip(),
                    )
                    page.set_viewport_size({"width": 1152, "height": 768})
                    page.goto(conversation_url)
                    expect(composer).to_be_in_viewport(ratio=1)
                    expect(
                        page.get_by_role("button", name="Ask follow-up")
                    ).to_be_in_viewport(ratio=1)

                with self.browser_page(browser, java_script_enabled=False) as (_, page):
                    page.set_viewport_size({"width": 390, "height": 844})
                    self.sign_in(page, viewer)
                    page.goto(conversation_url)
                    expect(page.get_by_role("link", name="New chat")).to_have_count(0)
                    expect(page.get_by_role("textbox", name="Question")).to_have_count(
                        0
                    )
                    expect(
                        page.get_by_role("link", name="Reference 1:", exact=False)
                    ).to_be_visible()
                    page.get_by_role("link", name="All saved chats").press("Enter")
                    expect(
                        page.get_by_role("link", name="When was the inland pickup?")
                    ).to_be_visible()
