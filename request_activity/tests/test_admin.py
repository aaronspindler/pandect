from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from request_activity.store import StoreUnavailable
from request_activity.views import OPERATOR_GROUP

EVENT_ID = UUID("226a35ba-b612-42b4-ac81-f915fe6ba0ae")
OBSERVED_AT = datetime(2026, 11, 25, 18, 30, tzinfo=UTC)
FINGERPRINT = "v1:" + "a" * 64


@override_settings(PANDECT_REQUEST_ACTIVITY_ENABLED=True)
class RequestActivityAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.operator = user_model.objects.create_user("operator", is_staff=True)
        cls.staff = user_model.objects.create_user("staff", is_staff=True)
        cls.superuser = user_model.objects.create_superuser("superuser")
        cls.investigator = user_model.objects.create_user("investigator")
        cls.group = Group.objects.create(name=OPERATOR_GROUP)
        cls.operator.groups.add(cls.group)
        cls.investigator.groups.add(cls.group)

    def setUp(self):
        self.url = reverse("request_activity")
        reader_patch = patch("request_activity.views.Reader")
        self.open_reader = reader_patch.start()
        self.addCleanup(reader_patch.stop)
        health_patch = patch(
            "request_activity.views._capture_health", return_value=None
        )
        self.capture_health = health_patch.start()
        self.addCleanup(health_patch.stop)
        self.reader = self.open_reader.return_value.__enter__.return_value
        self.reader.lookup.return_value = SimpleNamespace(
            rows=(), first_seen=None, last_seen=None, next_cursor=None
        )
        self.reader.repeat_summary.return_value = SimpleNamespace(
            start_date=date(2026, 10, 27),
            end_date=date(2026, 11, 25),
            eligible_requests=0,
            unknown_fingerprints=0,
            versions=(),
            latest_record_at=None,
        )

    def test_only_active_staff_with_the_group_can_open_request_history(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        for user, denied_status in (
            (self.investigator, 302),
            (self.staff, 403),
            (self.superuser, 403),
        ):
            with self.subTest(user=user):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.url).status_code, denied_status)
        self.open_reader.assert_not_called()

        self.client.force_login(self.operator)
        self.assertContains(
            self.client.get(self.url), "No request records are available yet."
        )
        self.assertContains(
            self.client.get(reverse("admin:index")), f'href="{self.url}"'
        )
        self.operator.groups.remove(self.group)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertNotContains(
            self.client.get(reverse("admin:index")), f'href="{self.url}"'
        )
        self.open_reader.assert_called_once()

    @override_settings(PANDECT_REQUEST_ACTIVITY_ENABLED=False)
    def test_disabled_hides_the_link_and_history(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertNotContains(
            self.client.get(reverse("admin:index")), f'href="{self.url}"'
        )
        self.open_reader.assert_not_called()

    def test_search_and_older_page_are_exact_private_posts(self):
        self.client.force_login(self.operator)
        self.reader.lookup.return_value = SimpleNamespace(
            rows=(
                SimpleNamespace(
                    occurred_at=OBSERVED_AT,
                    method="GET",
                    status=200,
                    route="home",
                    ip="2001:db8::4",
                    fingerprint=FINGERPRINT,
                ),
            ),
            first_seen=OBSERVED_AT,
            last_seen=OBSERVED_AT,
            next_cursor=(OBSERVED_AT, EVENT_ID),
        )
        filters = {
            "ip": "2001:0db8::4",
            "fingerprint": FINGERPRINT,
            "start": "2026-11-24",
            "end": "2026-11-25",
        }
        response = self.client.post(self.url, filters)
        self.assertContains(response, "Server-reported status")
        self.assertContains(response, "First seen")
        self.assertContains(response, "2001:db8::4")
        self.assertContains(response, "Show older requests")
        self.assertNotContains(response, "?ip=")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.reader.lookup.assert_called_once_with(
            ip="2001:db8::4",
            fingerprint=FINGERPRINT,
            after=datetime(2026, 11, 24, tzinfo=UTC),
            before=datetime(2026, 11, 26, tzinfo=UTC),
            cursor=None,
            limit=50,
        )

        self.client.post(
            self.url, {**filters, "cursor": f"{OBSERVED_AT.isoformat()}|{EVENT_ID}"}
        )
        self.assertEqual(
            self.reader.lookup.call_args.kwargs["cursor"], (OBSERVED_AT, EVENT_ID)
        )

    def test_get_query_parameters_do_not_drive_private_search(self):
        self.client.force_login(self.operator)
        response = self.client.get(self.url, {"ip": "198.51.100.24"})
        self.assertNotContains(response, "198.51.100.24")
        self.assertIsNone(self.reader.lookup.call_args.kwargs["ip"])

    def test_invalid_filters_and_direct_mutation_never_query_history(self):
        self.client.force_login(self.operator)
        for data, message in (
            ({"ip": "wrong"}, "Enter a valid IPv4 or IPv6 address."),
            ({"fingerprint": "v1:wrong"}, "Enter a complete versioned fingerprint."),
            (
                {"start": "2026-11-25", "end": "2026-11-24"},
                "Enter a date on or after the start date.",
            ),
            ({"cursor": "invalid"}, "Start a new search to reset the page."),
        ):
            with self.subTest(data=data):
                self.assertContains(
                    self.client.post(self.url, data), message, status_code=400
                )
        self.assertEqual(self.client.delete(self.url).status_code, 405)
        self.open_reader.assert_not_called()

    def test_post_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.operator)
        response = client.post(self.url, {"ip": "198.51.100.24"})
        self.assertEqual(response.status_code, 403)
        self.open_reader.assert_not_called()

    def test_store_outage_is_distinct_from_empty_results(self):
        self.client.force_login(self.operator)
        self.assertContains(
            self.client.post(self.url, {"ip": "198.51.100.24"}),
            "No requests match this search.",
        )
        self.open_reader.side_effect = StoreUnavailable("hidden result details")
        response = self.client.get(self.url)
        self.assertContains(
            response, "Request history is temporarily unavailable.", status_code=503
        )
        self.assertNotContains(response, "hidden result details", status_code=503)
        self.assertNotContains(response, "No request records", status_code=503)

    def test_repeat_summary_and_capture_gap_are_visible(self):
        self.client.force_login(self.operator)
        self.reader.repeat_summary.return_value = SimpleNamespace(
            start_date=date(2026, 10, 27),
            end_date=date(2026, 11, 25),
            eligible_requests=10,
            unknown_fingerprints=2,
            versions=(
                SimpleNamespace(
                    version="v1", distinct_fingerprints=4, repeat_fingerprints=2
                ),
            ),
            latest_record_at=OBSERVED_AT,
        )
        self.capture_health.return_value = {
            "heartbeat_at": OBSERVED_AT,
            "last_gap_at": OBSERVED_AT,
            "queued_oldest_at": OBSERVED_AT,
            "queued_count": 6,
            "stale": True,
        }
        response = self.client.get(self.url)
        self.assertContains(response, "2026-10-27 through 2026-11-25 UTC")
        self.assertContains(response, "Seen on multiple dates")
        self.assertContains(response, "<strong>10</strong> eligible page requests")
        self.assertContains(response, "<strong>2</strong> without a usable fingerprint")
        self.assertContains(response, "Latest known capture gap")
        self.assertContains(response, "6 buffered records")
        self.assertContains(response, "Capture status is stale.")
