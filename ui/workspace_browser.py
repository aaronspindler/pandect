"""Exercise private case navigation through real links and browser history."""

from playwright.sync_api import expect

from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase
from core.models import AnalysisRun, CaseAnalysisBatch
from people.models import Person
from text.models import TextEvidence


class WorkspaceBrowserTests(BrowserTestCase):
    artifact_subdirectory = "workspace-navigation"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("workspace-owner")
        self.case = Case.objects.create(owner=self.owner, name="Synthetic navigation")
        for index in range(27):
            TextEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Record {index:02}",
                body=f"Synthetic text {index}.",
            )

    def test_tools_reader_paging_and_history(self):
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(self.url("core:analysis:index", case_id=self.case.pk))
                page.evaluate(
                    "window.savedRail = document.querySelector('.app-navigation')"
                )
                page.get_by_role("link", name="Evidence", exact=True).click()
                records = page.locator("#evidence-records")
                expect(records.locator(".evidence-row")).to_have_count(25)
                records.locator(".evidence-row").first.click()
                expect(page.locator(".evidence-reader")).to_be_visible()
                self.assertTrue(
                    page.evaluate(
                        "window.savedRail === document.querySelector('.app-navigation')"
                    )
                )
                reader = page.url
                page.locator(".evidence-reader").evaluate(
                    "node => window.readerNode = node"
                )
                page.get_by_role("link", name="Next page", exact=True).click()
                expect(records.locator(".evidence-row")).to_have_count(2)
                self.assertTrue(
                    page.locator(".evidence-reader").evaluate(
                        "node => node === window.readerNode"
                    )
                )
                page.go_back()
                expect(page).to_have_url(reader)
                expect(records.locator(".evidence-row")).to_have_count(25)
                page.get_by_role("link", name="Processing", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Processing", exact=True)
                ).to_be_visible()
                self.assertTrue(
                    page.evaluate(
                        "window.savedRail === document.querySelector('.app-navigation')"
                    )
                )
                page.go_back()
                expect(page.locator(".evidence-reader")).to_be_visible()
                self.assertFalse(
                    page.evaluate(
                        "Object.keys(localStorage).some(k => k.includes('htmx'))"
                    )
                )
                self.capture(page, engine, "reader-after-back")

    def test_account_switch_clears_owner_content_for_a_viewer_of_the_same_case(self):
        viewer = self.make_user("workspace-viewer")
        CaseMembership.objects.create(case=self.case, user=viewer, role="viewer")
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine) as (context, page),
            ):
                self.sign_in(page)
                page.goto(self.url("cases:case_settings", case_id=self.case.pk))
                invitation = page.get_by_role("link", name="Invite collaborator")
                expect(invitation).to_be_visible()
                replacement = context.new_page()
                replacement.goto(self.url("account_logout"))
                replacement.get_by_role("button", name="Sign out", exact=True).click()
                self.sign_in(replacement, viewer)
                page.bring_to_front()
                page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")

                heading = page.get_by_role(
                    "heading", name="Your session or access changed"
                )
                expect(heading).to_be_focused()
                expect(invitation).to_have_count(0)
                expect(page.get_by_text(self.case.name)).to_have_count(0)
                self.capture(page, engine, "private-access-recovery-desktop")
                page.set_viewport_size({"width": 390, "height": 844})
                self.capture(page, engine, "private-access-recovery-mobile")
                page.get_by_role("link", name="All cases", exact=True).click()
                expect(page.get_by_role("link", name=self.case.name)).to_be_visible()

    def test_role_change_discards_the_view_under_the_previous_role(self):
        editor = self.make_user("workspace-editor")
        for engine in self.engines:
            membership = CaseMembership.objects.create(
                case=self.case, user=editor, role="editor"
            )
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, editor)
                case_people = self.url("people:list", case_id=self.case.pk)
                activity = self.url("cases:activity", case_id=self.case.pk)
                # Hold the first activity request across a change of role. A
                # returning tab must also check changes made after that read.
                pending = []

                def hold_activity(route, _request, pending=pending):
                    pending.append(route)

                page.route(activity, hold_activity, times=1)
                with page.expect_request(activity):
                    page.goto(case_people)
                expect(page.get_by_role("link", name="Add person")).to_be_visible()
                initial = pending.pop()
                editor_snapshot = initial.fetch()
                self.assertEqual(editor_snapshot.json()["role"], "editor")
                self.db(
                    CaseMembership.objects.filter(pk=membership.pk).update,
                    role="viewer",
                )
                page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                initial.fulfill(response=editor_snapshot)
                expect(
                    page.get_by_role("heading", name="Your session or access changed")
                ).to_be_focused()
                expect(page.get_by_role("link", name="Add person")).to_have_count(0)
                expect(page.get_by_text(self.case.name)).to_have_count(0)
                page.goto(case_people)
                expect(page.get_by_role("link", name="Add person")).to_have_count(0)
                expect(
                    page.get_by_role("heading", name="People", exact=True)
                ).to_be_visible()
            self.db(membership.delete)

    def test_back_after_logout_reads_the_private_route_again(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine) as (context, page),
            ):
                self.sign_in(page)
                evidence = self.url("core:evidence:list", case_id=self.case.pk)
                page.goto(evidence)
                private_record = page.get_by_role("link", name="Record 26", exact=True)
                expect(private_record).to_be_visible()
                # Playwright disables the back-forward cache in some engines. Exercise
                # the page-transition listener as well as a real history entry.
                page.evaluate(
                    "window.dispatchEvent(new PageTransitionEvent("
                    "'pagehide', {persisted: true}))"
                )
                expect(private_record).to_be_hidden()
                page.goto(self.url("marketing:home"))
                replacement = context.new_page()
                replacement.goto(self.url("account_logout"))
                replacement.get_by_role("button", name="Sign out", exact=True).click()
                expect(
                    replacement.get_by_role("button", name="Sign in", exact=True)
                ).to_be_visible()
                page.go_back()
                expect(
                    page.get_by_role("button", name="Sign in", exact=True)
                ).to_be_visible()
                expect(private_record).to_have_count(0)

    def test_admin_scope_recheck_preserves_a_draft_until_the_role_changes(self):
        editor = self.make_user("workspace-admin-editor")
        editor.is_staff = editor.is_superuser = True
        editor.save(update_fields=("is_staff", "is_superuser"))
        person = Person.objects.create(case=self.case, first_name="Synthetic witness")
        person_url = self.url("admin:people_person_change", object_id=person.pk)
        for engine in self.engines:
            membership = CaseMembership.objects.create(
                case=self.case, user=editor, role="editor"
            )
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, editor)
                page.goto(person_url)
                name = page.get_by_label("First name")
                name.fill("Unsaved draft")
                visibility = "document.dispatchEvent(new Event('visibilitychange'))"
                with page.expect_response(person_url) as checked:
                    page.evaluate(visibility)
                self.assertEqual(checked.value.status, 200)
                expect(name).to_have_value("Unsaved draft")
                self.db(
                    CaseMembership.objects.filter(pk=membership.pk).update,
                    role="viewer",
                )
                page.evaluate(visibility)
                heading = page.get_by_role(
                    "heading", name="Your session or access changed"
                )
                expect(heading).to_be_focused()
                expect(name).to_have_count(0)
                expect(page.get_by_text(person.first_name)).to_have_count(0)
            self.db(membership.delete)

    def test_filter_page_edit_and_return_on_desktop_and_mobile(self):
        for engine in self.engines:
            for width, height in [(1440, 900), (390, 844)]:
                with (
                    self.subTest(engine=engine, width=width),
                    self.browser_page(engine) as (_, page),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self.sign_in(page)
                    page.goto(self.url("core:evidence:list", case_id=self.case.pk))
                    search = page.get_by_role("searchbox", name="Filter evidence")
                    search.fill("Record")
                    page.wait_for_url("**/evidence/?q=Record")
                    page.get_by_role("link", name="Next page", exact=True).click()
                    rows = page.locator("#evidence-records .evidence-row")
                    expect(rows).to_have_count(2)
                    origin = page.url
                    target = rows.last.get_attribute("id")
                    page.locator("#case-search").evaluate(
                        "el => window.previousSearch = el"
                    )
                    rows.last.click()
                    edit = page.get_by_role("link", name="Edit details", exact=True)
                    edit.click()
                    dialog = page.get_by_role("dialog")
                    title = f"Record updated {engine} {width}"
                    dialog.get_by_label("Title", exact=True).fill(title)
                    dialog.get_by_role(
                        "button", name="Save changes", exact=True
                    ).click()
                    expect(dialog).not_to_be_visible()
                    expect(page.locator(".evidence-reader h1")).to_have_text(title)
                    expect(edit).to_be_focused()
                    page.go_back()
                    expect(page).to_have_url(origin)
                    expect(rows).to_have_count(2)
                    expect(search).to_have_value("Record")
                    expect(page.locator(f"#{target}")).to_be_focused()
                    self.assertTrue(
                        page.locator("#case-search").evaluate(
                            "el => el === window.previousSearch"
                        )
                    )
                    self.capture(page, engine, f"filtered-page-after-edit-{width}")

    def test_processing_pagers_keep_other_history_sections_in_place(self):
        CaseAnalysisBatch.objects.bulk_create(
            [
                CaseAnalysisBatch(
                    case=self.case, requested_by=self.owner, status="complete"
                )
                for _ in range(11)
            ]
        )
        AnalysisRun.objects.bulk_create(
            [
                AnalysisRun(
                    case=self.case,
                    requested_by=self.owner,
                    operation="entities",
                    method_version="1",
                    status="complete",
                )
                for _ in range(26)
            ]
        )
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(
                    self.url("core:analysis:processing", case_id=self.case.pk)
                    + "?history=1&batches_page=1"
                )
                batch = page.locator("#batch-history")
                history = page.locator("#processing-history")
                batch.evaluate("el => window.originalBatch = el")
                history.get_by_role("link", name="Next", exact=True).click()
                expect(history.locator(".pagination")).to_contain_text("Page 2 of 2")
                self.assertTrue(batch.evaluate("el => el === window.originalBatch"))
                history.evaluate("el => window.originalHistory = el")
                batch.get_by_role("link", name="Next", exact=True).click()
                expect(batch.locator(".pagination")).to_contain_text("Page 2 of 2")
                expect(history.locator(".pagination")).to_contain_text("Page 2 of 2")
                self.assertIn("page=2", page.url)
                self.assertIn("batches_page=2", page.url)
                self.assertTrue(history.evaluate("el => el === window.originalHistory"))

    def test_latest_shell_or_collection_navigation_wins(self):
        def hold_requests(page, url):
            held = []
            page.route(url, lambda route: held.append(route))
            return held

        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                evidence = self.url("core:evidence:list", case_id=self.case.pk)
                processing = self.url("core:analysis:processing", case_id=self.case.pk)
                page.goto(evidence)
                rail_requests = hold_requests(page, processing)
                rail = page.get_by_role("link", name="Processing", exact=True)
                next_link = page.get_by_role("link", name="Next page", exact=True)
                rows = page.locator("#evidence-records .evidence-row")
                driver = page.locator("[data-workspace-status]")
                with page.expect_request(processing):
                    rail.click()
                next_link.click()
                expect(rows).to_have_count(2)
                page_two = page.url
                rail_requests.pop().continue_()
                expect(driver).not_to_have_attribute("aria-busy", "true")
                expect(page).to_have_url(page_two)
                expect(rows).to_have_count(2)
                page.go_back()
                expect(rows).to_have_count(25)
                page_requests = hold_requests(page, page_two)
                with page.expect_request(page_two):
                    next_link.click()
                with page.expect_request(processing):
                    rail.click()
                page_requests.pop().continue_()
                expect(page.locator("#evidence-records")).not_to_have_attribute(
                    "aria-busy", "true"
                )
                expect(rows).to_have_count(25)
                expect(page).to_have_url(evidence)
                rail_requests.pop().continue_()
                expect(page).to_have_url(processing)
                page.go_back()
                expect(rows).to_have_count(25)
                with page.expect_request(processing):
                    rail.click()
                page.get_by_role("searchbox", name="Filter evidence").fill("Record 04")
                expect(driver).not_to_have_attribute("aria-busy", "true")
                rail_requests.pop().continue_()
                expect(rows).to_have_count(1)
                expect(rows).to_contain_text("Record 04")

    def test_empty_people_refresh_keeps_shell_and_adds_search(self):
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(self.url("people:list", case_id=self.case.pk))
                expect(
                    page.get_by_role("heading", name="No people yet")
                ).to_be_visible()
                page.locator(".app-navigation").evaluate(
                    "el => window.originalRail = el"
                )
                self.db(Person.objects.create, case=self.case, first_name="Avery")
                page.evaluate("document.dispatchEvent(new Event('pandect:refresh'))")
                page.get_by_role("link", name="Refresh results").click()
                search = page.get_by_role("searchbox", name="Search people")
                expect(search).to_be_visible()
                self.assertTrue(
                    page.locator(".app-navigation").evaluate(
                        "el => el === window.originalRail"
                    )
                )
                search.fill("unmatched")
                expect(
                    page.get_by_role("heading", name="No matching people")
                ).to_be_visible()
                self.db(Person.objects.filter(case=self.case).delete)

    def test_workspace_pagination_without_javascript(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("core:evidence:list", case_id=self.case.pk))
                page.get_by_role("link", name="Next page", exact=True).click()
                expect(page.locator("#evidence-records .evidence-row")).to_have_count(2)
