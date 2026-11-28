from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.http import Http404
from django.test import TestCase, override_settings
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from cases.models import Case, CaseMembership
from core.tests.testing import EvidenceViewTestMixin
from socialmedia import instagram_connector as connector
from socialmedia import monitor_jobs as jobs
from socialmedia.models import InstagramCapture, InstagramCheck


@override_settings(
    PANDECT_INSTAGRAM_PILOT_URL="http://synthetic.invalid",
    PANDECT_INSTAGRAM_PILOT_KEY="synthetic-test-key-only-12345678901",
)
class InstagramMonitorJobTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.editor = self.users["editor"]
        self.conn = jobs.finish_pairing(
            self.editor, jobs.start_pairing(self.editor)["id"]
        )
        self.approve(self.case, self.users["owner"])
        self.monitor = jobs.create_monitor(
            self.editor, self.case.pk, "@demo.account", 6
        )
        self.collect = self.enterContext(patch.object(connector, "collect"))
        self.collect.return_value = {
            "observer": self.conn.observer,
            "observer_id": self.conn.observer_id_string,
            "collector_version": connector.VERSION,
            "target": connector.PILOT_TARGET,
            "target_id": connector.PILOT_TARGET_ID,
            "followers": {"members": [], "complete": True, "reason": ""},
            "following": {"members": [], "complete": True, "reason": ""},
        }

    def approve(self, case, owner):
        jobs.set_target_approval(owner, case.pk, approved=True)

    def run_current(self):
        return jobs.run_check(str(self.monitor.pk), str(self.monitor.generation))

    def test_initial_check_is_idempotent_and_successor_is_deferred(self):
        original = self.monitor.generation
        row = DBTaskResult.objects.get()
        self.assertEqual(row.queue_name, "instagram")
        self.assertEqual(
            row.args_kwargs,
            {"args": [str(self.monitor.pk), str(original)], "kwargs": {}},
        )
        self.assertEqual(
            jobs.create_monitor(self.editor, self.case.pk, "demo.account", 6),
            self.monitor,
        )
        self.assertEqual(self.run_current(), "complete")
        self.assertEqual(self.run_current(), "cancelled")
        self.collect.assert_called_once_with(self.conn.session, connector.PILOT_TARGET)
        self.monitor.refresh_from_db()
        successor = DBTaskResult.objects.exclude(pk=row.pk).get()
        self.assertEqual(successor.run_after, self.monitor.next_check_at)
        self.assertGreater(successor.run_after, timezone.now() + timedelta(hours=5))
        self.assertEqual(self.monitor.checks.get().captures.count(), 2)
        self.assertEqual(self.run_current(), "unavailable")
        self.assertEqual(DBTaskResult.objects.count(), 2)

    def test_interval_change_during_collection_preserves_capture_and_successor(self):
        generation = self.monitor.generation
        original = DBTaskResult.objects.get()

        def change_interval(*args):
            updated = jobs.change_interval(
                self.editor, self.case.pk, self.monitor.pk, 12
            )
            self.assertEqual(updated.generation, generation)
            return self.collect.return_value

        self.collect.side_effect = change_interval
        before = timezone.now()
        self.assertEqual(self.run_current(), "complete")
        self.monitor.refresh_from_db()
        successor = DBTaskResult.objects.exclude(pk=original.pk).get()
        self.assertEqual(self.monitor.checks.get().captures.count(), 2)
        self.assertEqual(successor.run_after, self.monitor.next_check_at)
        self.assertGreaterEqual(successor.run_after, before + timedelta(hours=12))
        self.assertLessEqual(successor.run_after, timezone.now() + timedelta(hours=12))

    def test_approval_only_allows_owner_for_their_case_and_stable_target(self):
        other = Case.objects.create(owner=self.editor)
        self.assertFalse(jobs.target_approved(other.pk))
        with self.assertRaisesMessage(
            ValidationError, jobs.check_message("target_not_approved")
        ):
            jobs.create_monitor(self.editor, other.pk, "demo.account", 24)
        for role in ("editor", "viewer"):
            with self.subTest(role=role), self.assertRaises(PermissionDenied):
                jobs.set_target_approval(self.users[role], self.case.pk, approved=False)
        with self.assertRaises(Http404):
            jobs.set_target_approval(self.users["owner"], other.pk, approved=True)
        self.approve(other, self.editor)
        monitor = jobs.create_monitor(self.editor, other.pk, " @Demo.Account ", 24)
        self.assertEqual(
            (monitor.target, monitor.target_id),
            ("demo.account", connector.PILOT_TARGET_ID),
        )
        self.assertTrue(jobs.target_approved(self.case.pk))
        with self.assertRaisesMessage(
            ValidationError, jobs.check_message("target_unavailable")
        ):
            jobs.create_monitor(self.editor, other.pk, "different.account", 24)

    def test_wrong_stable_target_cannot_publish(self):
        self.collect.return_value["target_id"] = "recycled-handle"
        self.assertEqual(self.run_current(), "failed")
        self.assertEqual(self.monitor.checks.get().error, "invalid_response")
        self.assertFalse(InstagramCapture.objects.exists())
        self.assertEqual(DBTaskResult.objects.count(), 1)

    def test_legacy_live_monitor_cannot_schedule_or_collect(self):
        live_config = {
            "user_id": str(self.editor.pk),
            "case_id": str(self.case.pk),
            "targets": (self.monitor.target,),
            "observer": self.conn.observer,
            "min_hours": 1,
            "url": "http://127.0.0.1:1",
            "token": "test-only",
        }
        self.monitor.source = self.conn.source = "instagram"
        self.monitor.save(update_fields=["source"])
        self.conn.save(update_fields=["source"])
        with self.settings(PANDECT_INSTAGRAM_LEGACY_REVOCATION=live_config):
            self.assertEqual(
                jobs.start_pairing(self.editor)["id"]["source"], "synthetic"
            )
            self.assertEqual(self.run_current(), "unavailable")
            self.assertEqual(self.monitor.checks.get().error, "source_changed")
            with self.assertRaisesMessage(
                ValidationError, jobs.check_message("source_changed")
            ):
                jobs.resume_monitor(self.editor, self.case.pk, self.monitor.pk)
        self.collect.assert_not_called()

    def test_replacement_and_disconnect_revoke_only_the_replaced_live_binding(self):
        self.conn.source = "instagram"
        self.conn.save(update_fields=["source"])
        pending = jobs.start_pairing(self.editor)["id"]
        with (
            patch.object(
                connector,
                "revoke_live",
                side_effect=RuntimeError("sensitive upstream detail"),
            ) as revoke,
            self.assertLogs("socialmedia.monitor_jobs", level="WARNING") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            updated = jobs.finish_pairing(self.editor, pending)
            self.assertEqual(updated.source, "synthetic")
            jobs.disconnect(self.editor)
            jobs.disconnect(self.editor)
            revoke.assert_not_called()
        revoke.assert_called_once_with(self.conn.session)
        self.assertNotIn("sensitive upstream detail", str(logs.output))
        self.assertNotIn(self.conn.session, str(logs.output))
        self.conn.refresh_from_db()
        self.assertFalse(self.conn.session)

    def test_live_disconnect_retires_work_and_revokes_the_binding_once_after_commit(
        self,
    ):
        self.conn.source = "instagram"
        self.conn.save(update_fields=["source"])
        with (
            patch.object(connector, "revoke_live") as revoke,
            self.captureOnCommitCallbacks(execute=True),
        ):
            jobs.disconnect(self.editor)
            jobs.disconnect(self.editor)
            revoke.assert_not_called()
            self.monitor.refresh_from_db()
            self.assertEqual(self.monitor.status, "paused")
        revoke.assert_called_once_with(self.conn.session)
        self.collect.assert_not_called()

    def test_changed_target_policy_is_checked_before_contact_and_before_resuming(self):
        with patch.object(connector, "allowed_target", return_value=False):
            self.assertEqual(self.run_current(), "unavailable")
            self.assertEqual(self.monitor.checks.get().error, "target_unavailable")
            with self.assertRaisesMessage(
                ValidationError, jobs.check_message("target_unavailable")
            ):
                jobs.resume_monitor(self.editor, self.case.pk, self.monitor.pk)
        self.collect.assert_not_called()

    def test_revoke_during_collection_preserves_history_and_cancels_old_delivery(self):
        self.assertEqual(self.run_current(), "complete")
        self.monitor.refresh_from_db()
        self.monitor.next_check_at = timezone.now()
        self.monitor.save(update_fields=["next_check_at"])
        owner = self.users["owner"]

        def revoke(*args):
            jobs.set_target_approval(owner, self.case.pk, approved=False)
            self.assertFalse(jobs.target_approved(self.case.pk))
            self.approve(self.case, owner)
            return self.collect.return_value

        self.collect.side_effect = revoke
        self.assertEqual(self.run_current(), "cancelled")
        self.assertEqual(self.run_current(), "cancelled")
        self.assertEqual(self.monitor.checks.filter(status="complete").count(), 1)
        cancelled = self.monitor.checks.get(status="cancelled")
        self.assertEqual(cancelled.error, "target_not_approved")
        self.assertEqual(InstagramCapture.objects.count(), 2)
        self.assertEqual(DBTaskResult.objects.count(), 2)
        self.monitor.refresh_from_db()
        self.assertEqual(self.monitor.status, "paused")
        self.assertIsNone(self.monitor.next_check_at)

    def test_revoked_approval_prevents_pending_delivery_and_resume(self):
        jobs.set_target_approval(self.users["owner"], self.case.pk, approved=False)
        self.assertEqual(self.run_current(), "cancelled")
        with self.assertRaisesMessage(
            ValidationError, jobs.check_message("target_not_approved")
        ):
            jobs.resume_monitor(self.editor, self.case.pk, self.monitor.pk)
        self.collect.assert_not_called()

    def test_pending_pairing_cannot_cross_sources_or_replace_connection_after_drift(
        self,
    ):
        pending = jobs.start_pairing(self.editor)["id"]
        with (
            self.settings(PANDECT_INSTAGRAM_PILOT_URL=""),
            patch.object(connector, "finish_pairing") as finish,
        ):
            for old in (pending, pending["token"]):
                with self.assertRaisesMessage(
                    ValidationError, jobs.check_message("not_configured")
                ):
                    jobs.finish_pairing(self.editor, old)
            finish.assert_not_called()
        with patch.object(
            connector, "finish_pairing", return_value={"source": "instagram"}
        ):
            with self.assertRaisesMessage(
                ValidationError, jobs.check_message("source_changed")
            ):
                jobs.finish_pairing(self.editor, pending)
        original_session = self.conn.session
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.session, original_session)

    def test_pause_resume_and_owner_stop_retire_existing_deliveries(self):
        owner = self.users["owner"]
        with self.assertRaises(PermissionDenied):
            jobs.pause_monitor(owner, self.case.pk, self.monitor.pk)
        jobs.pause_monitor(self.editor, self.case.pk, self.monitor.pk)
        self.assertEqual(self.run_current(), "cancelled")
        resumed = jobs.resume_monitor(self.editor, self.case.pk, self.monitor.pk)
        self.assertNotEqual(resumed.generation, self.monitor.generation)
        stopped = jobs.stop_monitor(owner, self.case.pk, self.monitor.pk)
        self.assertEqual(stopped.status, "stopped")
        with self.assertRaises(ValidationError):
            jobs.resume_monitor(self.editor, self.case.pk, self.monitor.pk)
        self.collect.assert_not_called()

    def test_recheck_after_membership_revocation_during_collection(self):
        def revoke(*args):
            CaseMembership.objects.filter(case=self.case, user=self.editor).delete()
            return self.collect.return_value

        self.collect.side_effect = revoke
        self.assertEqual(self.run_current(), "failed")
        self.assertEqual(self.monitor.checks.get().error, "access_removed")
        self.assertFalse(InstagramCapture.objects.exists())
        self.assertEqual(DBTaskResult.objects.count(), 1)

    def test_unverified_user_cannot_start_background_collection(self):
        EmailAddress.objects.filter(user=self.editor).update(verified=False)
        self.run_current()
        self.collect.assert_not_called()
        self.monitor.refresh_from_db()
        self.assertEqual(self.monitor.status, "paused")

    def test_expiry_requires_reconnection_before_another_check(self):
        with patch.object(connector, "connection_expires_at", return_value=None):
            self.assertEqual(self.run_current(), "unavailable")
            self.assertEqual(self.monitor.checks.get().error, "disconnected")
            with self.assertRaisesMessage(
                ValidationError, jobs.check_message("disconnected")
            ):
                jobs.resume_monitor(self.editor, self.case.pk, self.monitor.pk)
        self.collect.assert_not_called()
        jobs.finish_pairing(self.editor, jobs.start_pairing(self.editor)["id"])
        resumed = jobs.resume_monitor(self.editor, self.case.pk, self.monitor.pk)
        self.assertEqual(jobs.run_check(resumed.pk, resumed.generation), "complete")

    def test_disconnect_while_collecting_invalidates_publication(self):
        def disconnect(*args):
            jobs.disconnect(self.editor)
            return self.collect.return_value

        self.collect.side_effect = disconnect
        self.assertEqual(self.run_current(), "cancelled")
        self.assertEqual(self.monitor.checks.get().status, "cancelled")
        self.assertFalse(InstagramCapture.objects.exists())
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.session, "")
        with self.assertRaises(ValidationError):
            jobs.resume_monitor(self.editor, self.case.pk, self.monitor.pk)

    def test_competing_worker_defers_without_collecting_or_stealing_its_slot(self):
        ready, release = Event(), Event()
        connection_id = self.conn.pk

        def run_other_worker():
            try:
                with jobs._collection_slot(connection_id) as acquired:
                    self.assertTrue(acquired)
                    ready.set()
                    release.wait(10)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as workers:
            another_worker = workers.submit(run_other_worker)
            try:
                self.assertTrue(ready.wait(5))
                with self.assertRaises(ValidationError):
                    jobs.recover_monitor(self.editor, self.case.pk, self.monitor.pk)
                self.assertEqual(self.run_current(), "unavailable")
            finally:
                release.set()
            another_worker.result()
        self.collect.assert_not_called()
        self.monitor.refresh_from_db()
        self.assertGreater(self.monitor.next_check_at, timezone.now())
        self.assertFalse(self.monitor.checks.exists())

    def test_platform_cooldown_applies_to_entire_connection_across_cases(self):
        other_case = Case.objects.create(owner=self.editor)
        self.approve(other_case, self.editor)
        other = jobs.create_monitor(self.editor, other_case.pk, "demo.account", 1)
        self.collect.side_effect = connector.ConnectorError(
            "cooldown", retry_after=9 * 3600
        )
        self.assertEqual(self.run_current(), "cooldown")
        self.assertEqual(jobs.run_check(other.pk, other.generation), "unavailable")
        self.collect.assert_called_once()
        self.conn.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(other.next_check_at, self.conn.cooldown_until)
        self.assertGreater(other.next_check_at, timezone.now() + timedelta(hours=8))

    def test_challenge_pauses_every_monitor_using_expired_session(self):
        other_case = Case.objects.create(owner=self.editor)
        self.approve(other_case, self.editor)
        other = jobs.create_monitor(self.editor, other_case.pk, "demo.account", 1)
        self.collect.side_effect = connector.ConnectorError("challenge")
        self.assertEqual(self.run_current(), "failed")
        other.refresh_from_db()
        self.assertEqual(other.status, "paused")
        self.assertEqual(self.monitor.checks.get().error, "challenge")
        self.conn.refresh_from_db()
        self.assertFalse(self.conn.session)

    def test_partial_publication_failure_keeps_no_file_or_case_evidence(self):
        self.collect.return_value["following"]["members"] = [
            {"handle": "invalid handle"}
        ]
        self.assertEqual(self.run_current(), "failed")
        self.assertEqual(self.monitor.checks.get().error, "invalid_response")
        self.assertFalse(InstagramCapture.objects.exists())
        self.assertFalse(any(path.is_file() for path in self.media_root.rglob("*")))

    def test_resume_after_worker_stopped_retires_interrupted_check(self):
        pending = InstagramCheck.objects.create(
            monitor=self.monitor,
            generation=self.monitor.generation,
            started_at=timezone.now() - jobs.RECOVERY_GRACE,
        )
        self.monitor.next_check_at = None
        self.monitor.save(update_fields=["next_check_at"])
        resumed = jobs.recover_monitor(self.editor, self.case.pk, self.monitor.pk)
        self.assertNotEqual(resumed.generation, self.monitor.generation)
        pending.refresh_from_db()
        self.assertEqual((pending.status, pending.error), ("failed", "interrupted"))
        self.assertEqual(self.run_current(), "cancelled")
