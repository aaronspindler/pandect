from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from core.testing import EvidenceViewTestMixin
from socialmedia.instagram_connector import LIVE_VERSION, ConnectorError
from socialmedia.models import InstagramCheck, InstagramConnection, InstagramMonitor
from socialmedia.monitor_evidence import publish_captures


@override_settings(
    PANDECT_INSTAGRAM_PILOT_URL="http://pilot.invalid",
    PANDECT_INSTAGRAM_PILOT_KEY="synthetic-connection-test-key-0000",
)
class InstagramMonitorViewTests(EvidenceViewTestMixin, TestCase):
    def case_url(self, name, *args, case=None):
        return reverse(f"instagram:{name}", args=((case or self.case).pk, *args))

    def connect(self):
        url = reverse("account_instagram") + f"?case={self.case.pk}"
        self.client.post(url, {"action": "start"})
        response = self.client.post(url, {"action": "finish"}, follow=True)
        self.assertContains(response, "Manage connection")
        return url

    def create(self):
        self.login("editor")
        self.connect()
        response = self.client.post(
            self.case_url("create"), {"target": "@demo.account", "interval_hours": "6"}
        )
        monitor = InstagramMonitor.objects.get(case=self.case)
        self.assertRedirects(response, self.case_url("detail", monitor.pk))
        return monitor

    def test_connection_belongs_to_user_and_disconnect_survives_case_revocation(self):
        monitor = self.create()
        conn = InstagramConnection.objects.get(user=self.users["editor"])
        self.assertNotContains(
            self.client.get(reverse("account_instagram")), conn.session
        )
        pending = self.client.session.get("instagram_pilot_pair")
        self.assertIsNone(pending)
        self.login("owner")
        response = self.client.post(
            reverse("account_instagram"),
            {"action": "finish", "pair_id": "client-value"},
            follow=True,
        )
        self.assertContains(response, "Start a new connection")
        self.assertNotContains(response, conn.observer)
        self.assertEqual(
            self.client.post(self.case_url("action", monitor.pk, "pause")).status_code,
            403,
        )
        self.login("editor")
        CaseMembership.objects.filter(
            user=self.users["editor"], case=self.case
        ).delete()
        self.client.post(
            reverse("account_instagram") + f"?case={self.case.pk}",
            {"action": "disconnect"},
        )
        conn.refresh_from_db()
        monitor.refresh_from_db()
        self.assertFalse(conn.session)
        self.assertNotEqual(monitor.status, "active")

    def test_live_setup_scope_and_synthetic_attribution(self):
        synthetic = self.create()
        InstagramCheck.objects.create(
            monitor=synthetic, status="complete", finished_at=timezone.now()
        )
        settings = {
            "url": "http://fixture.invalid",
            "token": "test-token",
            "user_id": str(self.users["editor"].pk),
            "case_id": str(self.case.pk),
            "observer": "test.observer",
            "targets": ("test.target", "unrelated.target"),
            "min_hours": 6,
        }
        with self.settings(
            PANDECT_INSTAGRAM_LIVE=settings, PANDECT_INSTAGRAM_PILOT_URL=""
        ):
            account_url = reverse("account_instagram")
            response = self.client.get(account_url)
            self.assertContains(response, "Synthetic observer")
            self.assertContains(response, "private Chromium window on the pilot Mac")
            self.assertNotContains(response, "unrelated.target")
            self.assertNotContains(response, 'type="password"')
            response = self.client.get(self.case_url("list"))
            self.assertNotContains(response, "Create monitor")
            self.assertContains(
                self.client.get(self.case_url("detail", synthetic.pk)),
                "Synthetic Instagram monitor",
            )
            self.assertContains(
                self.client.get(
                    reverse("core:analysis:processing", args=(self.case.pk,))
                ),
                "Synthetic check",
            )
            pair = {"source": "instagram", "token": "private-test-pair"}
            with patch(
                "socialmedia.monitor_jobs.start_pairing",
                return_value={"id": pair, "url": ""},
            ):
                response = self.client.post(
                    account_url, {"action": "start"}, follow=True
                )
            self.assertContains(response, "Confirm Instagram connection")
            self.assertNotContains(response, "private-test-pair")
            self.assertContains(response, "confirm within 10 minutes")
            with patch("socialmedia.monitor_jobs.cancel_pairing") as cancel:
                self.client.post(
                    account_url, {"action": "cancel", "pair_id": "untrusted"}
                )
                cancel.assert_called_once_with(self.users["editor"], pair)
            with patch(
                "socialmedia.monitor_jobs.start_pairing",
                return_value={"id": pair, "url": ""},
            ):
                self.client.post(account_url, {"action": "start"})
            with patch("socialmedia.monitor_jobs.finish_pairing") as finish:
                self.client.post(
                    account_url, {"action": "finish", "pair_id": "untrusted"}
                )
                finish.assert_called_once_with(self.users["editor"], pair)
            InstagramConnection.objects.filter(user=self.users["editor"]).update(
                source="instagram",
                observer="test.observer",
                session="private-test-session",
            )
            form = self.client.get(self.case_url("create"))
            self.assertContains(form, "@test.observer")
            self.assertNotContains(form, "demo.account")
            self.assertNotContains(form, "unrelated.target")
            self.assertNotContains(form, "private-test-session")
            self.assertNotIn(
                1, dict(form.context["form"].fields["interval_hours"].choices)
            )
            self.assertEqual(
                self.client.post(
                    self.case_url("create"),
                    {"target": "test.target", "interval_hours": "1"},
                ).status_code,
                400,
            )
            self.assertEqual(
                self.client.post(
                    self.case_url("create"),
                    {"target": "test.target", "interval_hours": "6"},
                ).status_code,
                302,
            )
            live_monitor = InstagramMonitor.objects.get(
                case=self.case, source="instagram"
            )
            self.assertNotContains(
                self.client.get(self.case_url("detail", live_monitor.pk)), "Synthetic"
            )
            self.login("owner")
            unauthorized = self.client.get(account_url)
            self.assertNotContains(unauthorized, "Connect Instagram account")
            self.assertNotContains(unauthorized, "test.observer")
            self.assertNotContains(
                self.client.get(self.case_url("create")), "test.target"
            )

    def test_pending_live_connection_cannot_finish_after_switch_to_synthetic(self):
        self.login("editor")
        session = self.client.session
        session["instagram_pilot_pair"] = {
            "source": "instagram",
            "token": "older-live-pair",
        }
        session.save()
        with patch("socialmedia.monitor_jobs.finish_pairing") as finish:
            response = self.client.post(
                reverse("account_instagram"), {"action": "finish"}, follow=True
            )
        finish.assert_not_called()
        self.assertContains(response, "Start a new connection")
        self.assertNotContains(response, "older-live-pair")

    def test_pending_sign_in_can_be_retried_and_expiry_offers_a_fresh_connection(self):
        self.login("editor")
        url = reverse("account_instagram")
        self.client.post(url, {"action": "start"})
        with patch(
            "socialmedia.instagram_connector.finish_pairing",
            side_effect=[
                ConnectorError("pairing_pending"),
                ConnectorError("pairing_expired"),
            ],
        ):
            early = self.client.post(url, {"action": "finish"}, follow=True)
            self.assertContains(early, "Confirm synthetic connection")
            expired = self.client.post(url, {"action": "finish"}, follow=True)
        self.assertContains(expired, "Connect synthetic account")
        self.assertNotContains(expired, "Confirm synthetic connection")

    def test_roles_and_unavailable_pilot(self):
        monitor = self.create()
        self.login("viewer")
        response = self.client.get(self.case_url("list"))
        self.assertContains(response, "@demo.account")
        self.assertNotContains(response, "Manage connection")
        self.assertEqual(self.client.get(self.case_url("create")).status_code, 403)
        self.assertEqual(
            self.client.post(self.case_url("action", monitor.pk, "stop")).status_code,
            403,
        )
        self.login("outsider")
        self.assertEqual(
            self.client.get(self.case_url("detail", monitor.pk)).status_code, 404
        )
        self.assertEqual(
            self.client.get(
                self.case_url("detail", monitor.pk, case=self.other_case)
            ).status_code,
            404,
        )
        self.login("owner")
        with self.settings(PANDECT_INSTAGRAM_PILOT_URL=""):
            tools = self.client.get(
                reverse("core:analysis:index", args=(self.case.pk,))
            )
            self.assertContains(tools, self.case_url("list"))
            self.assertContains(self.client.get(self.case_url("list")), "disabled")
            self.assertNotContains(
                self.client.get(self.case_url("list")), "Create monitor"
            )
        self.assertRedirects(
            self.client.post(self.case_url("action", monitor.pk, "stop")),
            self.case_url("detail", monitor.pk),
        )
        monitor.refresh_from_db()
        self.assertEqual(monitor.status, "stopped")

    def test_change_feed_readable_sources_and_paginated_partial_handles(self):
        monitor = self.create()
        connection = monitor.connection
        for number in range(2):
            check = InstagramCheck.objects.create(
                monitor=monitor, status="complete", finished_at=timezone.now()
            )
            captured = {
                "observer": connection.observer,
                "observer_id": connection.observer_id_string,
                "target": monitor.target,
                "target_id": "fixture-target",
                "collector_version": "synthetic-browser-v1",
                "followers": {
                    "members": [
                        {"handle": f"follower{n:02}"} for n in range(27 + number)
                    ],
                    "complete": True,
                    "reason": "",
                },
                "following": {
                    "members": [{"handle": "one_visible"}],
                    "complete": False,
                    "reason": "stalled",
                },
            }
            publish_captures(check, captured, originals=[])
        self.login("viewer")
        detail = self.client.get(self.case_url("detail", monitor.pk))
        self.assertContains(detail, "@follower27")
        self.assertContains(detail, "New accounts followed")
        self.assertContains(detail, "The list stopped loading")
        self.assertNotContains(detail, "Stop monitor permanently")
        followers = check.captures.get(direction="followers")
        url = self.case_url("capture", monitor.pk, followers.pk)
        changes = self.client.get(url, {"show": "changes"})
        self.assertContains(changes, "@follower27")
        self.assertNotContains(changes, "@follower24")
        self.assertContains(changes, "View previous capture")
        self.assertContains(changes, self.url("detail", followers.evidence, "document"))
        self.assertContains(self.client.get(url, {"page": 2}), "@follower25")
        partial = self.client.get(
            self.case_url(
                "capture", monitor.pk, check.captures.get(direction="following").pk
            )
        )
        self.assertContains(partial, "@one_visible")
        self.assertNotContains(partial, "View previous capture")
        self.assertContains(
            self.client.get(reverse("core:analysis:processing", args=[self.case.pk])),
            "@demo.account",
        )
        self.login("outsider")
        self.assertEqual(
            self.client.get(
                self.case_url("capture", monitor.pk, followers.pk, case=self.other_case)
            ).status_code,
            404,
        )

    def test_new_following_shows_warning_from_prior_count_mismatch(self):
        owner = self.users["owner"]
        connection = InstagramConnection.objects.create(
            user=owner, source="instagram", observer="fixture.observer"
        )
        monitor = InstagramMonitor.objects.create(
            case=self.case,
            connection=connection,
            created_by=owner,
            source="instagram",
            target="fixture.target",
        )
        checks = []
        members = [{"handle": "known.account", "id": "101"}]
        for complete in (False, True):
            check = InstagramCheck.objects.create(
                monitor=monitor, status="complete", finished_at=timezone.now()
            )
            publish_captures(
                check,
                {
                    "source": "instagram",
                    "observer": connection.observer,
                    "target": monitor.target,
                    "target_id": "200",
                    "collector_version": LIVE_VERSION,
                    "followers": {
                        "members": [],
                        "complete": True,
                        "reason": "",
                        "page_count": 1,
                        "expected_count": 0,
                    },
                    "following": {
                        "members": members,
                        "complete": complete,
                        "reason": "" if complete else "count_mismatch",
                        "page_count": 1,
                        "expected_count": 2,
                    },
                },
                originals=[],
            )
            checks.append(check)
            members = [*members, {"handle": "new.account", "id": "102"}]

        self.login("viewer")
        detail = self.client.get(self.case_url("detail", monitor.pk))
        self.assertContains(detail, "Profile count mismatch", count=2)
        self.assertContains(detail, "@new.account")
        baseline, current = [
            check.captures.get(direction="following") for check in checks
        ]
        baseline_page = self.client.get(
            self.case_url("capture", monitor.pk, baseline.pk)
        )
        self.assertContains(baseline_page, "The returned list can serve as a baseline.")
        self.assertNotContains(baseline_page, "cannot establish a new baseline")
        changes = self.client.get(
            self.case_url("capture", monitor.pk, current.pk), {"show": "changes"}
        )
        self.assertContains(changes, "@new.account")
        self.assertContains(
            changes, "Gaps in either list can affect which additions appear."
        )
        self.assertContains(changes, self.url("detail", baseline.evidence, "document"))
