"""Explicit multi-account import mapping without JavaScript."""

import re
from uuid import UUID

from django.core.files.base import ContentFile
from playwright.sync_api import expect

from cases.models import Case
from config.tests.browser_base import BrowserTestCase
from core.analysis.services import process_run
from core.models import AnalysisRun
from document.models import DocumentEvidence
from people.models import Person
from phone.models import TextMessageEvidence


class ImportMappingBrowserTests(BrowserTestCase):
    artifact_subdirectory = "import-mapping-browser"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("mapping-browser-owner")
        self.case = Case.objects.create(owner=self.owner, name="Synthetic messages")
        self.people = [
            Person.objects.create(case=self.case, first_name=name)
            for name in ("Alex", "Blair", "Casey")
        ]
        html = """<div class="message"><span class="timestamp"></span>
<span class="sender">Me</span>
<span class="bubble">First message</span></div>
<div class="message"><span class="timestamp"></span><span class="sender">Them</span>
<span class="recipient">Third</span><span class="bubble">Second message</span></div>
<div class="message"><span class="timestamp"></span><span class="sender">Third</span>
<span class="bubble">Third message</span></div>"""
        self.document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic message export",
            file=ContentFile(html.encode(), name="messages.html"),
        )

    def test_mapping_add_remove_preserves_selection_and_imports_three_accounts(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(
                    self.url("core:analysis:create", case_id=self.case.pk)
                    + "?operation=conversation_import"
                )
                source = page.get_by_label(
                    "Document · Synthetic message export", exact=True
                )
                source.check()
                first = page.get_by_role("group", name="Account mapping 1", exact=True)
                first.get_by_label("Case person", exact=True).select_option(
                    str(self.people[0].pk)
                )
                first.get_by_label(
                    "Recipient when the source omits one", exact=True
                ).select_option(str(self.people[1].pk))
                second = page.get_by_role("group", name="Account mapping 2", exact=True)
                second.get_by_label("Case person", exact=True).select_option(
                    str(self.people[1].pk)
                )
                page.get_by_label("Export timezone", exact=True).fill("America/Toronto")
                page.get_by_role(
                    "button", name="Add account mapping", exact=True
                ).click()
                expect(source).to_be_checked()
                expect(page.get_by_label("Export timezone", exact=True)).to_have_value(
                    "America/Toronto"
                )
                expect(first.get_by_label("Case person", exact=True)).to_have_value(
                    str(self.people[0].pk)
                )
                page.get_by_role("button", name="Remove mapping 3", exact=True).click()
                expect(
                    page.get_by_role("group", name="Account mapping 3", exact=True)
                ).to_have_count(0)
                page.get_by_role(
                    "button", name="Add account mapping", exact=True
                ).click()
                third = page.get_by_role("group", name="Account mapping 3", exact=True)
                third.get_by_label("Source account label", exact=True).fill("Third")
                third.get_by_label("Case person", exact=True).select_option(
                    str(self.people[2].pk)
                )
                third.get_by_label(
                    "Recipient when the source omits one", exact=True
                ).select_option(str(self.people[0].pk))
                before = self.db(AnalysisRun.objects.count)
                for width, height in ((1440, 900), (390, 844), (320, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    self.capture(page, engine, f"three-account-mapping-{width}")
                page.get_by_role("button", name="Start analysis", exact=True).click()
                expect(
                    page.get_by_role("status").filter(has_text=re.compile(r"^Queued$"))
                ).to_be_visible()
                self.assertEqual(self.db(AnalysisRun.objects.count), before + 1)
                run_id = UUID(page.url.rstrip("/").split("/")[-1])
                self.db(process_run, run_id)
                run = self.db(AnalysisRun.objects.get, pk=run_id)
                self.assertEqual(run.status, "complete", run.error_code)
                pairs = self.db(
                    lambda: list(
                        TextMessageEvidence.objects.filter(case=self.case)
                        .order_by("body")
                        .values_list("sender_id", "recipient_id")
                    )
                )
                self.assertEqual(
                    pairs,
                    [
                        (self.people[0].pk, self.people[1].pk),
                        (self.people[1].pk, self.people[2].pk),
                        (self.people[2].pk, self.people[0].pk),
                    ],
                )
