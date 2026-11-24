from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from core.testing import EvidenceViewTestMixin
from socialmedia import monitor_jobs
from socialmedia.instagram_connector import (
    PILOT_TARGET_ID,
    ConnectorError,
)
from socialmedia.instagram_rosters import LIVE_VERSION
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
        self.assertRedirects(response, self.case_url("list"))
        return url

    def create(self):
        monitor_jobs.set_target_approval(
            self.users["owner"], self.case.pk, approved=True
        )
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

    def test_live_pilot_cannot_pair_or_schedule_and_retained_history_is_visible(self):
        synthetic = self.create()
        InstagramCheck.objects.create(
            monitor=synthetic, status="complete", finished_at=timezone.now()
        )
        connection = InstagramConnection.objects.create(
            user=self.users["owner"],
            observer="test.observer",
            observer_id_string="987",
            session="private-test-session",
            source="instagram",
        )
        legacy = InstagramMonitor.objects.create(
            case=self.case,
            connection=connection,
            created_by=self.users["owner"],
            source="instagram",
            target="test.target",
            status="paused",
        )
        live_config = {
            "url": "http://fixture.invalid",
            "token": "test-token",
            "user_id": str(self.users["owner"].pk),
            "case_id": str(self.case.pk),
            "observer": connection.observer,
            "targets": (legacy.target, "unrelated.target"),
            "min_hours": 6,
        }
        self.login("owner")
        with self.settings(
            PANDECT_INSTAGRAM_LEGACY_REVOCATION=live_config,
            PANDECT_INSTAGRAM_PILOT_URL="",
        ):
            account_url = reverse("account_instagram")
            response = self.client.get(account_url)
            self.assertContains(response, "Previous Instagram observer")
            self.assertContains(response, "Instagram collection is disabled")
            self.assertNotContains(response, connection.session)
            self.assertNotContains(response, "Connect Instagram account")
            self.assertNotContains(response, "unrelated.target")
            self.assertNotContains(response, 'type="password"')

            with patch("socialmedia.instagram_connector.start_pairing") as pairing:
                response = self.client.post(
                    account_url, {"action": "start"}, follow=True
                )
                pairing.assert_not_called()
            self.assertNotContains(response, "Confirm Instagram connection")
            self.assertIsNone(self.client.session.get("instagram_pilot_pair"))
            response = self.client.post(
                self.case_url("create"),
                {"target": legacy.target, "interval_hours": "24"},
            )
            self.assertContains(
                response, "Instagram collection is disabled", status_code=400
            )
            self.assertEqual(InstagramMonitor.objects.filter(case=self.case).count(), 2)
            response = self.client.post(
                self.case_url("action", legacy.pk, "resume"), follow=True
            )
            self.assertNotContains(response, "Resume monitor")
            legacy.refresh_from_db()
            self.assertEqual(legacy.status, "paused")
            self.assertContains(self.client.get(self.case_url("list")), "@test.target")
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

    def test_case_owner_can_approve_and_revoke_from_settings(self):
        settings_url = reverse("cases:case_settings", args=[self.case.pk])
        approval_url = reverse("cases:case_instagram_monitoring", args=[self.case.pk])
        self.login("editor")
        self.connect()
        response = self.client.get(self.case_url("list"))
        self.assertContains(response, "The case owner must approve")
        self.assertNotContains(response, "Create monitor")
        self.assertNotContains(response, "Review monitoring approval")
        self.assertEqual(
            self.client.post(
                self.case_url("create"),
                {"target": "@demo.account", "interval_hours": "6"},
            ).status_code,
            400,
        )
        self.assertFalse(InstagramMonitor.objects.filter(case=self.case).exists())
        self.assertEqual(
            self.client.post(approval_url, {"action": "approve"}).status_code, 403
        )
        self.login("viewer")
        self.assertEqual(
            self.client.post(approval_url, {"action": "approve"}).status_code, 403
        )
        self.login("outsider")
        self.assertEqual(
            self.client.post(approval_url, {"action": "approve"}).status_code, 404
        )

        self.login("owner")
        self.assertContains(
            self.client.get(self.case_url("list")),
            settings_url + "#instagram-monitoring",
        )
        response = self.client.get(settings_url)
        self.assertContains(response, "Approve synthetic Instagram monitoring")
        self.assertContains(
            response, "Case members can read the saved lists and comparisons"
        )
        self.assertEqual(self.client.get(approval_url).status_code, 405)
        response = self.client.post(approval_url, {"action": "approve"}, follow=True)
        self.assertContains(response, "Revoke Instagram monitoring approval")
        self.assertTrue(monitor_jobs.target_approved(self.case.pk))
        self.assertFalse(monitor_jobs.target_approved(self.other_case.pk))

        monitor = self.create()
        self.login("owner")
        response = self.client.post(approval_url, {"action": "revoke"}, follow=True)
        self.assertContains(response, "Approve synthetic Instagram monitoring")
        self.assertFalse(monitor_jobs.target_approved(self.case.pk))
        monitor.refresh_from_db()
        self.assertEqual(monitor.status, "paused")
        self.login("editor")
        response = self.client.get(self.case_url("detail", monitor.pk))
        self.assertContains(response, "The case owner must approve")
        self.assertNotContains(response, "Resume monitor")

    def test_expired_connection_offers_reconnect_without_exposing_binding(self):
        monitor = self.create()
        conn = monitor.connection
        with patch(
            "socialmedia.instagram_connector.connection_expires_at", return_value=None
        ):
            response = self.client.get(self.case_url("detail", monitor.pk))
            self.assertContains(
                response, "This monitor needs an active synthetic connection"
            )
            self.assertContains(response, "Manage your connection")
            self.assertNotContains(response, "Change check interval")
            response = self.client.get(reverse("account_instagram"))
            self.assertContains(response, "is inactive")
            self.assertContains(response, "Reconnect synthetic account")
            self.assertNotContains(response, conn.session)

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
        self.assertEqual(monitor.status, "active")
        self.assertRedirects(
            self.client.post(
                self.case_url("action", monitor.pk, "stop"), {"confirm_stop": "1"}
            ),
            self.case_url("detail", monitor.pk),
        )
        monitor.refresh_from_db()
        self.assertEqual(monitor.status, "stopped")

    def test_schedule_changes_keep_delivery_and_running_checks_hide_recovery(self):
        monitor = self.create()
        delivery = monitor.generation, monitor.next_check_at
        interval = self.case_url("action", monitor.pk, "interval")
        detail = self.case_url("detail", monitor.pk)
        self.assertRedirects(
            self.client.post(interval, {"interval_hours": "12"}), detail
        )
        monitor.refresh_from_db()
        self.assertEqual(monitor.interval_hours, 12)
        self.assertEqual((monitor.generation, monitor.next_check_at), delivery)
        monitor.next_check_at = None
        monitor.save(update_fields=["next_check_at"])
        check = InstagramCheck.objects.create(
            monitor=monitor, generation=monitor.generation
        )
        self.assertContains(self.client.get(detail), "A check is running.")
        self.assertNotContains(self.client.get(detail), "Recover an interrupted check")
        recover = self.case_url("action", monitor.pk, "recover")
        self.client.post(recover, {"worker_stopped": "1"})
        check.refresh_from_db()
        self.assertEqual(check.status, "running")
        check.started_at = (
            timezone.now() - monitor_jobs.RECOVERY_GRACE - timedelta(seconds=1)
        )
        check.save(update_fields=["started_at"])
        self.assertContains(self.client.get(detail), "Recover an interrupted check")

    def test_connection_navigation_requires_configuration_or_retained_connection(self):
        self.login("editor")
        with self.settings(PANDECT_INSTAGRAM_PILOT_URL=""):
            self.assertNotContains(
                self.client.get(reverse("account_email")), "Instagram connection"
            )
        self.connect()
        with self.settings(PANDECT_INSTAGRAM_PILOT_URL=""):
            self.assertContains(
                self.client.get(reverse("account_email")), "Instagram connection"
            )

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
                "target_id": PILOT_TARGET_ID,
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

    def test_latest_comparison_reports_zero_and_distinguishes_newer_check_failures(
        self,
    ):
        monitor = self.create()
        connection = monitor.connection
        detail_url = self.case_url("detail", monitor.pk)
        self.login("viewer")

        for handles in ([], [{"handle": "new.account"}], [{"handle": "new.account"}]):
            check = InstagramCheck.objects.create(
                monitor=monitor, status="complete", finished_at=timezone.now()
            )
            publish_captures(
                check,
                {
                    "observer": connection.observer,
                    "observer_id": connection.observer_id_string,
                    "target": monitor.target,
                    "target_id": PILOT_TARGET_ID,
                    "collector_version": "synthetic-browser-v1",
                    "followers": {"members": handles, "complete": True, "reason": ""},
                    "following": {
                        "members": [],
                        "complete": False,
                        "reason": "stalled",
                    },
                },
                originals=[],
            )
            if not handles:
                self.assertContains(
                    self.client.get(detail_url),
                    "No comparable baseline for the latest capture.",
                )

        latest = check.captures.get(direction="followers")
        detail = self.client.get(detail_url)
        self.assertContains(detail, "No additions observed", count=1)
        self.assertContains(detail, "Latest capture was partial.")
        self.assertContains(
            detail, self.case_url("capture", monitor.pk, latest.pk) + "?show=changes"
        )
        result, history = detail.content.decode().split(
            "<summary>Check history</summary>"
        )
        self.assertNotIn("@new.account", result)
        self.assertIn("@new.account", history)

        InstagramCheck.objects.create(
            monitor=monitor,
            status="failed",
            error="cooldown",
            finished_at=timezone.now(),
        )
        detail = self.client.get(detail_url)
        self.assertContains(detail, "Latest check failed.")
        self.assertContains(detail, "No additions observed", count=1)
        self.assertContains(detail, "The collection source requested a longer wait")

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
