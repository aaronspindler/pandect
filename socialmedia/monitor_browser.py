import re
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from playwright.sync_api import expect

from cases.models import Case
from config.tests.browser_base import BrowserTestCase
from socialmedia.models import InstagramMonitor
from socialmedia.monitor_jobs import run_check


@override_settings(
    PANDECT_INSTAGRAM_PILOT_URL="http://fixture.invalid",
    PANDECT_INSTAGRAM_PILOT_KEY="synthetic-connection-test-key-0000",
)
class InstagramMonitorBrowserTests(BrowserTestCase):
    artifact_subdirectory = "instagram-monitors"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("monitor-browser-owner")
        self.case = Case.objects.create(owner=self.owner, name="Synthetic monitor case")

    def run_two_checks(self):
        monitor = InstagramMonitor.objects.get(case=self.case, status="active")
        source = {
            "observer": monitor.connection.observer,
            "observer_id": monitor.connection.observer_id_string,
            "target": monitor.target,
            "target_id": "fixture-target",
            "collector_version": "synthetic-browser-v1",
            "followers": {"members": [], "complete": True, "reason": ""},
            "following": {"members": [], "complete": True, "reason": ""},
        }
        with patch("socialmedia.instagram_connector.collect", return_value=source):
            self.assertEqual(run_check(monitor.pk, monitor.generation), "complete")
            source["followers"]["members"] = [{"handle": "lake.survey"}]
            source["following"]["members"] = [{"handle": "harbour.team"}]
            monitor.refresh_from_db()
            monitor.next_check_at = timezone.now()
            monitor.save(update_fields=["next_check_at"])
            self.assertEqual(run_check(monitor.pk, monitor.generation), "complete")

    def test_case_tools_connect_schedule_pause_and_read_on_mobile(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("core:analysis:index", case_id=self.case.pk))
                page.get_by_role("link", name="Instagram monitors").click()
                page.get_by_role(
                    "link", name=re.compile(r"(Instagram|Manage) connection")
                ).click()
                # Reuse the investigator's personal connection across browser engines.
                if page.get_by_role(
                    "button", name="Reconnect synthetic account"
                ).count():
                    page.get_by_role(
                        "button", name="Reconnect synthetic account"
                    ).click()
                else:
                    page.get_by_role("button", name="Connect synthetic account").click()
                page.get_by_role("button", name="Confirm synthetic connection").press(
                    "Enter"
                )
                page.get_by_role("link", name="Create monitor").click()
                page.get_by_label("Check interval").select_option("6")
                page.get_by_role("button", name="Create monitor").click()
                expect(
                    page.get_by_role("heading", name="@demo.account")
                ).to_be_visible()
                expect(
                    page.get_by_role("heading", name="New accounts followed")
                ).to_be_visible()
                self.db(self.run_two_checks)
                page.reload()
                page.get_by_role("link", name=re.compile("@lake.survey")).click()
                expect(
                    page.get_by_text("Handle match only", exact=False)
                ).to_be_visible()
                expect(
                    page.get_by_role("link", name="View previous capture", exact=False)
                ).to_be_visible()
                page.get_by_role("link", name="Monitor for @demo.account").click()
                page.get_by_role("button", name="Pause monitor").click()
                expect(
                    page.get_by_role("button", name="Resume monitor")
                ).to_be_visible()
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 640),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertTrue(
                        page.evaluate(
                            "document.documentElement.scrollWidth <= window.innerWidth"
                        )
                    )
                    self.capture(page, engine, f"monitor-{width}-{height}")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                page.set_viewport_size({"width": 390, "height": 844})
                self.capture(page, engine, "monitor-mobile-dark")
                page.set_viewport_size({"width": 1440, "height": 900})
                for style in ("font-size: 200%", "zoom: 4"):
                    page.locator("html").evaluate(
                        "(el, styles) => el.style.cssText = styles", style
                    )
                    self.assertTrue(
                        page.evaluate(
                            "document.documentElement.scrollWidth <= window.innerWidth"
                        )
                    )
                page.locator("html").evaluate("el => el.style.cssText = ''")
                page.emulate_media(forced_colors="active")
                expect(
                    page.get_by_role("button", name="Resume monitor")
                ).to_be_visible()
                page.get_by_role("button", name="Stop monitor permanently").click()
                expect(page.get_by_role("button", name="Resume monitor")).to_have_count(
                    0
                )
