"""Exercise pagination and coverage using an actual offline Chromium browser."""

from django.test import SimpleTestCase
from playwright.sync_api import sync_playwright

from instagram_pilot.collector import read_list, synthetic_page


class PilotTraversalTests(SimpleTestCase):
    def test_traverse_and_bound_each_direction(self):
        pages = [[("one", "1"), ("two", "2")], [("two", "2"), ("three", "3")]]
        with sync_playwright() as driver:
            browser = driver.chromium.launch()
            try:
                page = browser.new_page(offline=True)
                for scroll in (True, False):
                    with self.subTest(scroll=scroll):
                        page.set_content(synthetic_page(pages, scroll=scroll))
                        result = read_list(page, 3)
                        self.assertEqual(
                            [entry["handle"] for entry in result["members"]],
                            ["one", "two", "three"],
                        )
                        self.assertTrue(result["complete"])
                page.set_content(synthetic_page(pages, scroll=False))
                page.get_by_role("button", name="Load more").evaluate(
                    "button => {button.disabled = true}"
                )
                stalled = read_list(page, 3, wait_ms=150)
                self.assertEqual(stalled["reason"], "stalled")
                self.assertFalse(stalled["complete"])
                self.assertEqual(len(stalled["members"]), 2)
                page.set_content(synthetic_page([[]], scroll=False))
                self.assertTrue(read_list(page, 0)["complete"])
                page.set_content(synthetic_page(pages, scroll=False, finish=False))
                self.assertEqual(read_list(page, 3, wait_ms=300)["reason"], "stalled")
                page.set_content(synthetic_page(pages, scroll=False))
                self.assertEqual(read_list(page, 7)["reason"], "count_mismatch")
                page.set_content(synthetic_page(pages, scroll=False))
                self.assertEqual(
                    read_list(page, 3, max_members=2)["reason"], "row_limit"
                )
            finally:
                browser.close()
