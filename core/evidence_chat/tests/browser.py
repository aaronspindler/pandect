"""Read a verified saved answer after external AI is disabled."""

from uuid import uuid4

from django.utils import timezone
from playwright.sync_api import expect

from cases.models import Case
from config.tests.browser_base import BrowserTestCase
from core.evidence_chat.checkpoints import evidence_versions, source_row
from core.evidence_chat.corpus import discover
from core.models import ChatTurn, EvidenceChat
from text.models import TextEvidence


class ChatBrowserTests(BrowserTestCase):
    artifact_subdirectory = "evidence-chat"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("chat-owner")
        self.case = Case.objects.create(owner=self.owner, name="Harbour receipts")
        quote = "The driver field was blank."
        source = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Dispatch receipt",
            body=quote,
        )
        loc = next(r for r in discover(self.case, False) if r["pk"] == str(source.pk))
        chat = EvidenceChat.objects.create(
            case=self.case, created_by=self.owner, title="Driver at North Quay"
        )
        self.turn = ChatTurn.objects.create(
            case=self.case,
            chat=chat,
            ordinal=1,
            asked_by=self.owner,
            question="Who drove the van?",
            submission_id=uuid4(),
            started_at=timezone.now(),
            status="complete",
            mode="ordinary",
            plan={
                "agent_version": 1,
                "cutoff_versions": evidence_versions(self.case),
                "coverage": {"running": 1},
            },
            answer={
                "claims": [
                    {
                        "text": "The dispatch receipt leaves the driver unknown.",
                        "citations": [
                            {
                                "key": loc["key"],
                                "start": 0,
                                "end": len(quote),
                                "quote": quote,
                            }
                        ],
                    }
                ]
            },
        )
        source_row(self.turn, loc, "context").save()

    def test_offline_reader_opens_exact_source_with_javascript_disabled(self):
        for browser in self.engines:
            with (
                self.subTest(browser=browser),
                self.browser_page(browser, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(
                    self.url(
                        "core:chat:reply",
                        case_id=self.case.pk,
                        chat_id=self.turn.chat_id,
                        turn_id=self.turn.pk,
                    )
                )
                expect(page.get_by_text("Evidence cutoff", exact=False)).to_be_visible()
                expect(
                    page.get_by_text("Some evidence was still processing", exact=False)
                ).to_be_visible()
                expect(
                    page.get_by_role("button", name="Update with current evidence")
                ).to_have_count(0)
                expect(page.locator("blockquote")).to_have_text(
                    "The driver field was blank."
                )
                self.capture(page, browser, "saved-answer")
                page.get_by_role("link", name="Reference 1", exact=False).click()
                expect(
                    page.get_by_role("heading", name="Dispatch receipt", exact=True)
                ).to_be_visible()
