from datetime import UTC, datetime, timedelta
from email.message import Message
from email.utils import format_datetime
from unittest.mock import patch
from urllib.error import HTTPError

from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from core.case_search import cloudflare, rate_limit
from core.case_search.tests.fixtures import CONFIG
from core.models import HostedSearchIndex


class RetryDeadlineTests(SimpleTestCase):
    def test_retry_after_accepts_seconds_and_http_dates(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        future = now + timedelta(minutes=2)
        self.assertEqual(rate_limit.retry_deadline("120", now), future)
        self.assertEqual(
            rate_limit.retry_deadline(format_datetime(future, usegmt=True), now), future
        )
        self.assertGreater(rate_limit.retry_deadline("malformed", now), now)


@override_settings(PANDECT_CLOUDFLARE_SEARCH=CONFIG)
class SharedSearchRateLimitTests(TransactionTestCase):
    def test_idle_budget_allows_an_eight_case_search_page(self):
        with rate_limit.budget(CONFIG["account"]) as budget:
            for _ in range(8):
                self.assertIsNone(budget.reserve(3))

    def test_request_budget_is_shared_across_connections_by_account(self):
        now = timezone.now()
        other_account = "b" * 32
        with rate_limit.budget(CONFIG["account"]) as budget:
            self.assertIsNone(budget.reserve(rate_limit.BURST))
        with rate_limit.budget(CONFIG["account"]) as budget:
            self.assertGreater(budget.reserve(rate_limit.BURST), now)
        with rate_limit.budget(other_account) as budget:
            self.assertIsNone(budget.reserve(1))

    @patch("core.case_search.cloudflare.build_opener")
    def test_http_429_defers_another_namespace_without_a_network_request(self, opener):
        first = HostedSearchIndex(
            account=CONFIG["account"], namespace=CONFIG["namespace"]
        )
        other_namespace = HostedSearchIndex(
            account=CONFIG["account"], namespace="other-synthetic"
        )
        headers = Message()
        headers["Retry-After"] = "120"
        opener.return_value.open.side_effect = HTTPError(
            "https://api.cloudflare.com", 429, "Too Many Requests", headers, None
        )
        before = timezone.now()
        with self.assertRaisesMessage(
            cloudflare.HostedSearchError, "search_rate_limit"
        ) as first_error:
            cloudflare.request(first, "GET")
        self.assertGreaterEqual(
            first_error.exception.retry_at, before + timedelta(seconds=120)
        )
        self.assertTrue(first_error.exception.provider_throttled)

        with self.assertRaisesMessage(
            cloudflare.HostedSearchError, "search_rate_limit"
        ) as other_error:
            cloudflare.request(other_namespace, "GET")
        self.assertEqual(other_error.exception.retry_at, first_error.exception.retry_at)
        self.assertFalse(other_error.exception.provider_throttled)
        self.assertEqual(opener.return_value.open.call_count, 1)
