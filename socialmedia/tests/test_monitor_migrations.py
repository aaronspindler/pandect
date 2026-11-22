from hashlib import sha256
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from cases.models import Case
from socialmedia import instagram_connector


class InstagramMonitorMigrationTests(TransactionTestCase):
    def test_upgrade_invalidates_pilot_deliveries_and_preserves_captures(self):
        before = [("socialmedia", "0004_instagram_source")]
        after = [("socialmedia", "0005_instagram_target_approval")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(before)
            old = executor.loader.project_state(before).apps
            user = get_user_model().objects.create_user(username="synthetic-migration")
            case = Case.objects.create(owner=user, name="Synthetic migration case")
            binding = old.get_model(
                "socialmedia", "InstagramConnection"
            ).objects.create(user_id=user.pk, source="synthetic")
            Monitor = old.get_model("socialmedia", "InstagramMonitor")
            Check = old.get_model("socialmedia", "InstagramCheck")
            Document = old.get_model("document", "DocumentEvidence")
            Capture = old.get_model("socialmedia", "InstagramCapture")
            fixture_hash = sha256(b"synthetic capture").hexdigest()
            records = []
            for source, target, version in (
                ("synthetic", "demo.account", "synthetic-browser-v1"),
                ("instagram", "example.fixture", "instagram-endpoints-v1"),
            ):
                monitor = Monitor.objects.create(
                    case_id=case.pk,
                    connection_id=binding.pk,
                    created_by_id=user.pk,
                    source=source,
                    target=target,
                    next_check_at=timezone.now(),
                )
                completed = Check.objects.create(
                    monitor_id=monitor.pk, status="complete", generation=uuid4()
                )
                pending = Check.objects.create(
                    monitor_id=monitor.pk,
                    status="running",
                    generation=monitor.generation,
                )
                document = Document.objects.create(
                    case_id=case.pk,
                    uploaded_by_id=user.pk,
                    title="Synthetic roster capture",
                    file=f"{source}.json",
                    original_filename=f"{source}.json",
                    byte_size=len(b"synthetic capture"),
                    sha256=fixture_hash,
                )
                capture = Capture.objects.create(
                    monitor_check_id=completed.pk,
                    evidence_id=document.pk,
                    direction="followers",
                    observer_epoch=binding.observer_epoch,
                    target_identity=f"handle:{target}",
                    collector_version=version,
                    complete=True,
                    entry_count=0,
                )
                records.append((monitor, pending, completed, capture))

            executor = MigrationExecutor(connection)
            executor.migrate(after)
            migrated = executor.loader.project_state(after).apps
            new_monitors = migrated.get_model("socialmedia", "InstagramMonitor")
            new_checks = migrated.get_model("socialmedia", "InstagramCheck")
            new_captures = migrated.get_model("socialmedia", "InstagramCapture")
            approvals = migrated.get_model("socialmedia", "InstagramTargetApproval")
            self.assertFalse(approvals.objects.exists())
            for old_monitor, pending, completed, old_capture in records:
                with self.subTest(source=old_monitor.source):
                    monitor = new_monitors.objects.get(pk=old_monitor.pk)
                    self.assertEqual(monitor.status, "paused")
                    self.assertIsNone(monitor.next_check_at)
                    self.assertNotEqual(monitor.generation, old_monitor.generation)
                    self.assertEqual(
                        monitor.target_id,
                        instagram_connector.PILOT_TARGET_ID
                        if old_monitor.source == "synthetic"
                        else "",
                    )
                    self.assertEqual(
                        new_checks.objects.get(pk=pending.pk).status, "cancelled"
                    )
                    self.assertEqual(
                        new_checks.objects.get(pk=completed.pk).status, "complete"
                    )
                    capture = new_captures.objects.get(pk=old_capture.pk)
                    self.assertEqual(
                        capture.target_identity, old_capture.target_identity
                    )
                    self.assertEqual(
                        capture.collector_version, old_capture.collector_version
                    )
                    self.assertEqual(capture.evidence.sha256, fixture_hash)
                    self.assertEqual(
                        capture.evidence.file.name, f"{old_monitor.source}.json"
                    )
        finally:
            MigrationExecutor(connection).migrate(latest)
