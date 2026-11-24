"""Exercise standalone browser timing and destination restrictions."""

from unittest.mock import Mock

from django.test import SimpleTestCase

from instagram_pilot import live_host


class LiveHostActionTests(SimpleTestCase):
    def test_actions_respect_spacing_and_deadline_across_calls(self):
        now = 100.0

        def wait(seconds):
            nonlocal now
            now += seconds

        clock = live_host.ActionClock(now=lambda: now, wait=wait)
        self.assertTrue(clock.before(110))
        self.assertEqual(now, 100)
        self.assertFalse(clock.before(105))
        self.assertTrue(clock.before(106))
        self.assertEqual(now, 105)
        now = 115
        self.assertTrue(clock.before(116))
        self.assertEqual(now, 115)

    def test_browser_allows_only_official_navigation_and_assets(self):
        for url, navigation, allowed in (
            ("https://www.instagram.com/accounts/login/", True, True),
            ("https://images.cdninstagram.com/img", False, True),
            ("https://images.cdninstagram.com/page", True, False),
            ("https://instagram.com.example.test/a", False, False),
            ("http://www.instagram.com/a", True, False),
            ("https://name@www.instagram.com/a", True, False),
            ("https://www.instagram.com:8000/a", True, False),
        ):
            with self.subTest(url=url, navigation=navigation):
                route = Mock()
                route.request.url = url
                route.request.is_navigation_request.return_value = navigation
                live_host.restrict_destinations(route)
                self.assertEqual(route.continue_.call_count, int(allowed))
                self.assertEqual(route.abort.call_count, int(not allowed))
