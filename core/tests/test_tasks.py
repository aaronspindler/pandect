import logging
from io import StringIO
from unittest.mock import patch
from uuid import UUID

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection, transaction
from django.tasks import TaskResultStatus, task
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django_tasks_db.management.commands.db_worker import Command as WorkerCommand
from django_tasks_db.models import DBTaskResult

from cases.models import Case
from core.tasks import worker_probe

COMMAND = "core.management.commands.check_task_worker"
WORKER = "django_tasks_db.management.commands.db_worker"


@task
def failing_probe(nonce):
    raise ValueError("Synthetic task failure")


def drain_queue(*args):
    # Keep the real worker loop, without replacing the test runner's signals/logging.
    with (
        patch(f"{WORKER}.Worker.configure_signals"),
        patch(f"{WORKER}.Command.configure_logging"),
    ):
        call_command(
            "db_worker",
            batch=True,
            reload=False,
            startup_delay=False,
            interval=0,
            stdout=StringIO(),
        )


class QueueTests(TransactionTestCase):
    @override_settings(DEBUG=True)
    def test_debug_enqueues_durably_and_worker_executes(self):
        result = worker_probe.enqueue("synthetic-nonce")
        self.assertEqual(result.status, TaskResultStatus.READY)
        stored = DBTaskResult.objects.get(pk=result.id)
        self.assertEqual(stored.task_path, "core.tasks.worker_probe")
        self.assertIsInstance(stored.pk, UUID)
        self.assertEqual(stored.pk.version, 4)
        self.assertIsNone(stored.started_at)
        self.assertEqual(
            stored.args_kwargs, {"args": ["synthetic-nonce"], "kwargs": {}}
        )

        drain_queue()

        result.refresh()
        self.assertEqual(result.status, TaskResultStatus.SUCCESSFUL)
        self.assertEqual(result.return_value, "synthetic-nonce")
        self.assertEqual(result.attempts, 1)

    def test_task_and_application_write_commit_together(self):
        owner = get_user_model().objects.create_user(username="queue-owner")
        other = connection.copy()
        try:
            with transaction.atomic():
                case = Case.objects.create(owner=owner, name="Synthetic queued work")
                result = worker_probe.enqueue(str(case.pk))
                with other.cursor() as cursor:
                    cursor.execute(
                        f"SELECT count(*) FROM {DBTaskResult._meta.db_table} "
                        "WHERE id = %s",
                        [result.id],
                    )
                    self.assertEqual(cursor.fetchone()[0], 0)
                    cursor.execute(
                        "SELECT count(*) FROM cases_case WHERE id = %s", [case.pk]
                    )
                    self.assertEqual(cursor.fetchone()[0], 0)
            with other.cursor() as cursor:
                cursor.execute(
                    f"SELECT count(*) FROM {DBTaskResult._meta.db_table} WHERE id = %s",
                    [result.id],
                )
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute(
                    "SELECT count(*) FROM cases_case WHERE id = %s", [case.pk]
                )
                self.assertEqual(cursor.fetchone()[0], 1)
        finally:
            other.close()

    def test_rollback_removes_task_and_application_write(self):
        owner = get_user_model().objects.create_user(username="rollback-owner")
        with self.assertRaisesMessage(ValueError, "rollback"), transaction.atomic():
            case = Case.objects.create(owner=owner, name="Rolled back")
            result = worker_probe.enqueue(str(case.pk))
            raise ValueError("rollback")
        self.assertFalse(Case.objects.filter(pk=case.pk).exists())
        self.assertFalse(DBTaskResult.objects.filter(pk=result.id).exists())

    def test_failure_is_recorded_without_automatic_retry(self):
        result = failing_probe.enqueue("synthetic-nonce")
        output = StringIO()
        handler = logging.getLogger("django").handlers[0]
        with patch.object(handler, "stream", output):
            drain_queue()
        self.assertIn("FAILED", output.getvalue())
        self.assertNotIn("Synthetic task failure", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())
        drain_queue()
        result.refresh()
        self.assertEqual(result.status, TaskResultStatus.FAILED)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.errors[0].exception_class, ValueError)


class DiagnosticTests(TransactionTestCase):
    def test_probe_waits_for_worker_and_matches_result(self):
        output = StringIO()
        with patch(f"{COMMAND}.sleep", side_effect=drain_queue):
            call_command("check_task_worker", timeout=1, stdout=output)
        stored = DBTaskResult.objects.get()
        self.assertEqual(stored.status, TaskResultStatus.SUCCESSFUL)
        self.assertIn(str(stored.pk), output.getvalue())
        self.assertIn("OK", output.getvalue())
        self.assertNotIn(stored.return_value, output.getvalue())

    def test_timeout_does_not_execute_probe_or_remove_it(self):
        with (
            patch(f"{COMMAND}.monotonic", side_effect=[0, 1]),
            self.assertRaisesMessage(CommandError, "timed out (READY)"),
        ):
            call_command("check_task_worker", timeout=1)
        stored = DBTaskResult.objects.get()
        self.assertEqual(stored.status, TaskResultStatus.READY)
        self.assertIsNone(stored.started_at)

    def test_failed_probe_does_not_print_error_payload(self):
        with (
            patch(f"{COMMAND}.worker_probe", wraps=worker_probe) as probe,
            patch(f"{COMMAND}.sleep", side_effect=drain_queue),
        ):
            probe.enqueue.side_effect = failing_probe.enqueue
            with self.assertRaisesMessage(CommandError, "failed") as caught:
                call_command("check_task_worker", timeout=1)
        self.assertNotIn("Synthetic task failure", str(caught.exception))

    def test_mismatched_nonce_fails(self):
        with (
            patch(f"{COMMAND}.worker_probe", wraps=worker_probe) as probe,
            patch(f"{COMMAND}.sleep", side_effect=drain_queue),
        ):
            probe.enqueue.side_effect = lambda nonce: worker_probe.enqueue("wrong")
            with self.assertRaisesMessage(CommandError, "mismatch"):
                call_command("check_task_worker", timeout=1)

    def test_database_failure_has_a_safe_actionable_message(self):
        with patch(f"{COMMAND}.worker_probe", wraps=worker_probe) as probe:
            probe.enqueue.side_effect = OperationalError("synthetic-private-detail")
            with self.assertRaisesMessage(
                CommandError, "connectivity and migrations"
            ) as caught:
                call_command("check_task_worker")
        self.assertNotIn("synthetic-private-detail", str(caught.exception))

    def test_invalid_timeout_does_not_enqueue(self):
        for timeout in (0, -1):
            with (
                self.subTest(timeout=timeout),
                self.assertRaisesMessage(CommandError, "positive integer"),
            ):
                call_command("check_task_worker", timeout=timeout)
        self.assertFalse(DBTaskResult.objects.exists())

    @override_settings(
        TASKS={
            "default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}
        }
    )
    def test_diagnostic_rejects_inline_backend(self):
        with self.assertRaisesMessage(CommandError, "database Tasks backend"):
            call_command("check_task_worker")
        self.assertFalse(DBTaskResult.objects.exists())


class TaskAdminTests(TestCase):
    def test_global_task_admin_is_not_registered_or_accessible(self):
        result = worker_probe.enqueue("synthetic-private-nonce")
        self.assertFalse(admin.site.is_registered(DBTaskResult))
        with self.assertRaises(NoReverseMatch):
            reverse("admin:django_tasks_database_dbtaskresult_changelist")
        staff = get_user_model().objects.create_user(
            username="queue-staff", is_staff=True
        )
        staff.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="django_tasks_database")
        )
        superuser = get_user_model().objects.create_superuser(
            username="queue-superuser",
            email="operator@example.com",
            password="synthetic",
        )
        for user in (staff, superuser):
            self.client.force_login(user)
            for suffix in ("", f"{result.id}/change/", f"{result.id}/history/"):
                with self.subTest(user=user.username, suffix=suffix):
                    response = self.client.get(
                        "/admin/django_tasks_database/dbtaskresult/" + suffix
                    )
                    self.assertEqual(response.status_code, 404)
                    self.assertNotContains(
                        response, "synthetic-private-nonce", status_code=404
                    )


class WorkerLoggingTests(SimpleTestCase):
    def test_worker_uses_private_handler_without_adding_an_unfiltered_handler(self):
        logger = logging.getLogger("django_tasks_db")
        handlers = logger.handlers[:]
        WorkerCommand().configure_logging(1)
        self.assertEqual(logger.handlers, handlers)
        output = StringIO()
        with patch.object(handlers[0], "stream", output):
            logger.warning("Worker URL /invitations/private-token/?private=value")
        self.assertIn("/invitations/[redacted]", output.getvalue())
        self.assertNotIn("private-token", output.getvalue())
        self.assertNotIn("private=value", output.getvalue())
