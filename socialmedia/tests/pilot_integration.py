"""Opt-in PostgreSQL, worker-process and offline Chromium qualification."""

import hashlib
import os
import secrets
import selectors
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from cases.models import Case
from socialmedia import instagram_connector, monitor_jobs
from socialmedia.models import InstagramCapture, InstagramMonitor

ROOT = Path(__file__).resolve().parents[2]

# Run the bundled handler in its own process with a deterministic fixture clock.
# The first pair of collections straddles a change. The third repeats the
# second observation so a quiet check can be distinguished from missing data.
START_PILOT = """
from http.server import ThreadingHTTPServer
from itertools import chain, count
import collector
from server import PilotHandler
collector.time = chain((3600000, 3603600, 3603600), count(3607200, 3600)).__next__
with ThreadingHTTPServer(("127.0.0.1", 0), PilotHandler) as server:
    print(server.server_port, flush=True)
    server.serve_forever()
"""


class InstagramPilotIntegrationTests(TransactionTestCase):
    def test_scheduled_captures_survive_a_worker_restart(self):
        db = connection.settings_dict
        self.assertTrue(db["NAME"].startswith("test_"))
        self.assertIn(db["HOST"], {"localhost", "127.0.0.1", "::1", ""})
        media = self.enterContext(TemporaryDirectory())
        # Only pass the environment Chromium needs to locate its installed binary.
        browser_env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "TMPDIR", "PLAYWRIGHT_BROWSERS_PATH")
            if key in os.environ
        }
        pilot = self.enterContext(
            subprocess.Popen(
                [sys.executable, "-c", START_PILOT],
                cwd=ROOT / "instagram_pilot",
                env=browser_env,
                stdout=subprocess.PIPE,
                text=True,
            )
        )
        self.addCleanup(pilot.terminate)
        with selectors.DefaultSelector() as ready:
            ready.register(pilot.stdout, selectors.EVENT_READ)
            self.assertTrue(ready.select(timeout=15), "Synthetic server did not start")
        url = f"http://127.0.0.1:{int(pilot.stdout.readline())}"
        key = secrets.token_urlsafe(36)
        self.enterContext(
            self.settings(
                MEDIA_ROOT=media,
                PANDECT_INSTAGRAM_PILOT_URL=url,
                PANDECT_INSTAGRAM_PILOT_KEY=key,
            )
        )
        # The worker uses its real settings entry point and only the runner's test DB.
        worker_env = {
            "PATH": os.environ["PATH"],
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
            "POSTGRES_DB": db["NAME"],
            "POSTGRES_HOST": db["HOST"],
            "POSTGRES_PORT": str(db["PORT"]),
            "POSTGRES_USER": db["USER"],
            "POSTGRES_PASSWORD": db["PASSWORD"],
            "PANDECT_MEDIA_ROOT": media,
            "PANDECT_INSTAGRAM_PILOT_URL": url,
            "PANDECT_INSTAGRAM_PILOT_KEY": key,
        }

        def drain(queue_name="instagram"):
            subprocess.run(
                [
                    sys.executable,
                    "manage.py",
                    "db_worker",
                    "--batch",
                    "--no-reload",
                    "--no-startup-delay",
                    f"--queue-name={queue_name}",
                ],
                cwd=ROOT,
                env=worker_env,
                check=True,
                timeout=60,
                stdout=subprocess.DEVNULL,
            )

        def next_delivery(monitor):
            monitor.refresh_from_db()
            delivery = DBTaskResult.objects.get(queue_name="instagram", status="READY")
            self.assertEqual(delivery.run_after, monitor.next_check_at)
            self.assertGreater(delivery.run_after, timezone.now() + timedelta(hours=23))
            return delivery

        def make_due(monitor, delivery):
            # Advance only the two due timestamps in the disposable DB. A new
            # worker process consumes the persisted delivery with its original
            # generation and arguments.
            now = timezone.now()
            with transaction.atomic():
                DBTaskResult.objects.filter(pk=delivery.pk).update(run_after=now)
                InstagramMonitor.objects.filter(pk=monitor.pk).update(next_check_at=now)

        user = get_user_model().objects.create_user(
            username="pilot", email="pilot@example.test"
        )
        EmailAddress.objects.create(user=user, email=user.email, verified=True)
        case = Case.objects.create(owner=user)
        monitor_jobs.set_target_approval(user, case.pk, approved=True)
        pair = monitor_jobs.start_pairing(user)
        conn = monitor_jobs.finish_pairing(user, pair["id"])
        monitor = monitor_jobs.create_monitor(
            user, case.pk, instagram_connector.PILOT_TARGET, 24
        )
        self.assertEqual(monitor.target_id, instagram_connector.PILOT_TARGET_ID)
        drain("default")
        self.assertFalse(monitor.checks.exists())
        drain()
        first = monitor.checks.get()
        self.assertEqual(first.status, "complete", first.error)
        self.assertEqual(first.captures.filter(complete=True, entry_count=3).count(), 2)
        make_due(monitor, next_delivery(monitor))
        drain()
        second = monitor.checks.exclude(pk=first.pk).get()
        self.assertEqual(second.status, "complete", second.error)
        for capture in second.captures.all():
            with self.subTest(direction=capture.direction):
                baseline = first.captures.get(direction=capture.direction)
                self.assertEqual(capture.comparison.previous, baseline)
                self.assertEqual(capture.entry_count, baseline.entry_count)
                self.assertEqual(
                    capture.comparison.additions,
                    [
                        {
                            "handle": f"{capture.direction}.1001",
                            "id": "1001",
                            "handle_only": False,
                        }
                    ],
                )
                with capture.evidence.file.open("rb") as source:
                    self.assertEqual(
                        hashlib.sha256(source.read()).hexdigest(),
                        capture.evidence.sha256,
                    )
        make_due(monitor, next_delivery(monitor))
        drain()
        quiet = monitor.checks.exclude(pk__in=(first.pk, second.pk)).get()
        self.assertEqual(quiet.status, "complete", quiet.error)
        self.assertEqual(quiet.captures.filter(comparison__addition_count=0).count(), 2)

        make_due(monitor, next_delivery(monitor))
        monitor_jobs.set_target_approval(user, case.pk, approved=False)
        drain()
        self.assertEqual(monitor.checks.count(), 3)
        self.assertFalse(
            DBTaskResult.objects.filter(queue_name="instagram", status="READY").exists()
        )
        monitor_jobs.disconnect(user)
        conn.refresh_from_db()
        monitor.refresh_from_db()
        self.assertEqual(conn.session, "")
        self.assertEqual(monitor.status, "paused")
        self.assertEqual(InstagramCapture.objects.count(), 6)
        case.delete()
        self.assertFalse(InstagramCapture.objects.exists())
        self.assertFalse(any(path.is_file() for path in Path(media).rglob("*")))
