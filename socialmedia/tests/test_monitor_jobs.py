from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from cases.models import Case, CaseMembership
from core.testing import EvidenceViewTestMixin
from socialmedia import instagram_connector as connector
from socialmedia import monitor_jobs as jobs
from socialmedia.models import InstagramCapture, InstagramCheck, InstagramConnection


@override_settings(
    PANDECT_INSTAGRAM_PILOT_URL="http://synthetic.invalid",
    PANDECT_INSTAGRAM_PILOT_KEY="synthetic-test-key-only-12345678901",
)
class InstagramMonitorJobTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.editor = self.users["editor"]
        self.conn = InstagramConnection.objects.create(
            user=self.editor,
            observer="observer.test",
            session="synthetic-observer-ticket",
        )
        self.monitor = jobs.create_monitor(
            self.editor, self.case.pk, "@demo.account", 6
        )
        self.collect = self.enterContext(patch.object(connector, "collect"))
        self.collect.return_value = {
            "observer": self.conn.observer,
            "collector_version": connector.VERSION,
            "target": connector.PILOT_TARGET,
            "target_id": "target-007",
            "followers": {"members": [], "complete": True, "reason": ""},
            "following": {"members": [], "complete": True, "reason": ""},
        }

    def run_current(self):
        return jobs.run_check(str(self.monitor.pk), str(self.monitor.generation))

    def test_initial_check_is_idempotent_and_successor_is_deferred(self):
        original = self.monitor.generation
        row = DBTaskResult.objects.get()
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

    def test_higher_minimum_defers_previously_scheduled_live_delivery(self):
        self.conn.source = "instagram"
        self.conn.save(update_fields=["source"])
        live = {
            "user_id": str(self.editor.pk),
            "case_id": str(self.case.pk),
            "targets": ("demo.account",),
            "min_hours": 1,
        }
        self.collect.side_effect = connector.ConnectorError("cooldown", retry_after=60)
        with self.settings(PANDECT_INSTAGRAM_LIVE=live):
            monitor = jobs.create_monitor(self.editor, self.case.pk, "demo.account", 1)
            self.assertEqual(jobs.run_check(monitor.pk, monitor.generation), "cooldown")
        monitor.refresh_from_db()
        last_finish = monitor.checks.get().finished_at
        old_generation = monitor.generation
        old_due = monitor.next_check_at

        with self.settings(PANDECT_INSTAGRAM_LIVE=live | {"min_hours": 24}):
            updated = jobs.change_interval(self.editor, self.case.pk, monitor.pk, 24)
            self.assertEqual(jobs.run_check(monitor.pk, old_generation), "cancelled")
        self.assertLess(old_due, updated.next_check_at)
        self.assertEqual(updated.next_check_at, last_finish + timedelta(hours=24))
        self.assertEqual(updated.interval_hours, 24)
        self.collect.assert_called_once()

    def test_binding_retains_normalized_approved_live_target_and_source(self):
        self.conn.source = "instagram"
        self.conn.save(update_fields=["source"])
        with (
            patch.object(connector, "available", return_value=True),
            patch.object(connector, "current_source", return_value="instagram"),
            patch.object(
                connector,
                "permits",
                side_effect=lambda user, case=None: (
                    user == self.editor.pk and case == self.case.pk
                ),
            ),
            patch.object(
                connector,
                "allowed_target",
                side_effect=lambda source, target, interval: (
                    (source, target, interval)
                    == ("instagram", "controlled.account", 24)
                ),
            ),
        ):
            monitor = jobs.create_monitor(
                self.editor, self.case.pk, " @Controlled.Account ", 24
            )
            self.assertEqual(
                (monitor.target, monitor.source), ("controlled.account", "instagram")
            )
            for target, interval in (
                ("blocked", 24),
                ("controlled.account", 1),
                ("name/path", 24),
            ):
                with (
                    self.subTest(target=target, interval=interval),
                    self.assertRaises(ValidationError),
                ):
                    jobs.create_monitor(self.editor, self.case.pk, target, interval)
            with self.assertRaisesMessage(
                ValidationError, jobs.check_message("pilot_scope")
            ):
                jobs.create_monitor(
                    self.editor,
                    Case.objects.create(owner=self.editor).pk,
                    "controlled.account",
                    24,
                )
        self.collect.assert_not_called()

    def test_changed_source_cannot_collect_resume_or_reuse_connection(self):
        with (
            patch.object(connector, "available", return_value=True),
            patch.object(connector, "current_source", return_value="instagram"),
        ):
            with self.assertRaisesMessage(
                ValidationError, jobs.check_message("source_changed")
            ):
                jobs.create_monitor(self.editor, self.case.pk, "demo.account", 6)
            self.assertEqual(self.run_current(), "unavailable")
            self.assertEqual(self.monitor.checks.get().error, "source_changed")
            self.conn.source = "instagram"
            self.conn.save(update_fields=["source"])
            with self.assertRaisesMessage(
                ValidationError, jobs.check_message("source_changed")
            ):
                jobs.resume_monitor(self.editor, self.case.pk, self.monitor.pk)
            jobs.disconnect(self.editor)
            jobs.disconnect(self.editor)
        self.collect.assert_not_called()
        self.conn.refresh_from_db()
        self.assertFalse(self.conn.session)

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
        revoke.assert_called_once_with("synthetic-observer-ticket")
        self.assertNotIn("sensitive upstream detail", str(logs.output))
        self.assertNotIn("synthetic-observer-ticket", str(logs.output))
        self.conn.refresh_from_db()
        self.assertFalse(self.conn.session)

    def test_starting_a_live_replacement_retires_previous_work_even_when_cancelled(
        self,
    ):
        self.monitor.source = self.conn.source = "instagram"
        self.monitor.save(update_fields=["source"])
        self.conn.save(update_fields=["source"])
        live_config = {
            "user_id": str(self.editor.pk),
            "case_id": str(self.case.pk),
            "targets": (self.monitor.target,),
            "observer": self.conn.observer,
            "min_hours": 1,
            "url": "http://127.0.0.1:1",
            "token": "test-only",
        }

        def open_sign_in(user_id):
            # The host can close the browser as soon as this call starts.
            self.assertFalse(InstagramConnection.objects.get(pk=self.conn.pk).session)
            self.assertEqual(self.run_current(), "cancelled")
            return {"id": "new-pending-binding", "url": ""}

        self.login("editor")
        url = reverse("account_instagram")
        with (
            self.settings(PANDECT_INSTAGRAM_LIVE=live_config),
            patch.object(connector, "start_pairing", side_effect=open_sign_in),
            patch.object(connector, "cancel_pairing") as cancel,
            patch.object(connector, "revoke_live") as revoke,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.assertContains(self.client.get(url), "even if you cancel sign-in")
            self.client.post(url, {"action": "start"})
            self.client.post(url, {"action": "cancel"})
        cancel.assert_called_once_with(str(self.editor.pk), "new-pending-binding")
        revoke.assert_called_once_with(self.conn.session)
        self.monitor.refresh_from_db()
        self.assertEqual(self.monitor.status, "paused")
        self.assertIsNone(self.monitor.next_check_at)
        self.assertFalse(InstagramConnection.objects.get(pk=self.conn.pk).session)
        self.collect.assert_not_called()

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

    def test_revoked_pilot_scope_discards_collected_observation(self):
        with patch.object(connector, "permits", side_effect=[True, False]):
            self.assertEqual(self.run_current(), "failed")
        self.collect.assert_called_once()
        self.assertEqual(self.monitor.checks.get().error, "pilot_scope")
        self.assertFalse(InstagramCapture.objects.exists())

    def test_pending_pairing_cannot_cross_sources_or_replace_connection_after_drift(
        self,
    ):
        pending = jobs.start_pairing(self.editor)["id"]
        with (
            patch.object(connector, "available", return_value=True),
            patch.object(connector, "current_source", return_value="instagram"),
            patch.object(connector, "permits", return_value=True),
            patch.object(connector, "finish_pairing") as finish,
        ):
            for old in (pending, pending["token"]):
                with self.assertRaisesMessage(
                    ValidationError, jobs.check_message("pairing_expired")
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
        with (
            patch.object(connector, "permits", return_value=False),
            patch.object(connector, "start_pairing") as start,
            self.assertRaisesMessage(
                ValidationError, jobs.check_message("pilot_scope")
            ),
        ):
            jobs.start_pairing(self.editor)
        start.assert_not_called()
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.session, "synthetic-observer-ticket")

    def test_cancelling_pending_sign_in_contacts_only_its_configured_live_source(self):
        pending = {"source": "instagram", "token": "private-token"}
        with patch.object(connector, "cancel_pairing") as cancel:
            jobs.cancel_pairing(self.editor, pending)
            cancel.assert_not_called()
            with (
                patch.object(connector, "available", return_value=True),
                patch.object(connector, "current_source", return_value="instagram"),
                patch.object(connector, "permits", return_value=True),
            ):
                cancel.side_effect = connector.ConnectorError("pairing_expired")
                jobs.cancel_pairing(self.editor, pending)
                cancel.assert_called_once_with(str(self.editor.pk), "private-token")
                cancel.side_effect = connector.ConnectorError("unavailable")
                with self.assertRaisesMessage(
                    ValidationError, jobs.check_message("unavailable")
                ):
                    jobs.cancel_pairing(self.editor, pending)

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
        other = jobs.create_monitor(
            self.editor, Case.objects.create(owner=self.editor).pk, "demo.account", 1
        )
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
