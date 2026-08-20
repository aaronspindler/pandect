"""Bounded end-to-end coverage for the real SQLite-backed Huey worker."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANAGE_PY = PROJECT_ROOT / "manage.py"
COMMAND_TIMEOUT_SECONDS = 20
WORKFLOW_TIMEOUT_SECONDS = 30
WORKER_STOP_TIMEOUT_SECONDS = 10
POLL_INTERVAL_SECONDS = 0.1
MAX_DIAGNOSTIC_CHARACTERS = 4_000

FIRST_BODY = (
    "Queue payload sentinel one. This deliberately ordinary local writing sample "
    "contains enough distinct words for deterministic text and style processing "
    "while requiring no network service, credential, or provisioned model file."
)
SECOND_BODY = (
    "Queue payload sentinel two. A restarted worker should process this separate "
    "message after reconciliation restores its deliberately abandoned delivery, "
    "again using only deterministic local text analysis and durable SQLite state."
)

_CREATE_RUN_SCRIPT = r"""
import json
import sys
from datetime import timedelta

import django

django.setup()

from django.utils import timezone

from pandect.catalog.models import CatalogItem
from pandect.messages.models import Message, TextMessage
from pandect.processing.models import ProcessingStage
from pandect.processing.services import create_local_run

body = sys.argv[1]
mode = sys.argv[2]
item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.TEXT_MESSAGE)
message = Message.objects.create(catalog_item=item, body=body)
TextMessage.objects.create(message=message)
run = create_local_run(item, enqueue=mode == "enqueue")
first_stage = run.stages.order_by("priority", "created_at", "pk").first()
if mode == "stale":
    ProcessingStage.objects.filter(pk=first_stage.pk).update(
        status=ProcessingStage.Status.QUEUED,
        queued_at=timezone.now() - timedelta(seconds=5),
        task_id="lost-delivery",
    )
print(json.dumps({
    "item_id": str(item.pk),
    "run_id": str(run.pk),
    "stage_id": str(first_stage.pk),
}))
"""

_INSPECT_QUEUE_SCRIPT = r"""
import json

import django

django.setup()

from huey.contrib.djhuey import HUEY

tasks = []
for value in HUEY.storage.enqueued_items():
    task = HUEY.deserialize_task(value)
    tasks.append({
        "name": task.name,
        "args": list(task.args),
        "kwargs": task.kwargs,
    })
print(json.dumps(tasks))
"""


def _subprocess_environment(data_dir: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": "pandect.config.settings",
            "PANDECT_CLOUDFLARE_AI_ENABLED": "false",
            "PANDECT_DATA_DIR": str(data_dir),
            "PANDECT_DEBUG": "false",
            "PANDECT_MODELS_DIR": str(data_dir / "models"),
            "PANDECT_SECRET_KEY": "worker-integration-test-secret",
            "PANDECT_TIME_ZONE": "UTC",
            "PANDECT_WORK_DIR": str(data_dir / "work"),
        }
    )
    environment.pop("PANDECT_CLOUDFLARE_ACCOUNT_ID", None)
    environment.pop("PANDECT_CLOUDFLARE_API_TOKEN", None)
    return environment


def _sanitized_output(value, data_dir: Path) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = value or ""
    text = text.replace(str(data_dir), "<data-dir>")
    for body in (FIRST_BODY, SECOND_BODY):
        text = text.replace(body, "<redacted-test-evidence>")
    printable = "".join(
        character
        for character in text
        if character.isprintable() or character in "\n\t"
    )
    return printable[-MAX_DIAGNOSTIC_CHARACTERS:]


def _run_checked(
    argv: list[str],
    *,
    environment: dict[str, str],
    data_dir: Path,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> str:
    try:
        completed = subprocess.run(
            argv,
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        output = _sanitized_output(error.stdout, data_dir)
        raise AssertionError(
            f"Subprocess exceeded its {timeout}-second deadline.\n{output}"
        ) from None
    if completed.returncode:
        output = _sanitized_output(f"{completed.stdout}\n{completed.stderr}", data_dir)
        raise AssertionError(
            f"Subprocess exited with status {completed.returncode}.\n{output}"
        )
    return completed.stdout


def _json_from_script(
    script: str,
    *arguments: str,
    environment: dict[str, str],
    data_dir: Path,
):
    output = _run_checked(
        [sys.executable, "-c", script, *arguments],
        environment=environment,
        data_dir=data_dir,
    )
    for line in reversed(output.splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise AssertionError(
        f"Subprocess returned no JSON result.\n{_sanitized_output(output, data_dir)}"
    )


def _run_snapshot(database_path: Path, run_id: str) -> dict[str, object]:
    stored_run_id = run_id.replace("-", "")
    with sqlite3.connect(database_path, timeout=2) as connection:
        connection.execute("PRAGMA busy_timeout = 2000")
        run_row = connection.execute(
            "SELECT status, started_at, finished_at "
            "FROM processing_processingrun WHERE id = ?",
            (stored_run_id,),
        ).fetchone()
        if run_row is None:
            raise AssertionError("The processing run was not persisted.")
        stage_rows = connection.execute(
            "SELECT kind, status, attempts, task_id, failure_code "
            "FROM processing_processingstage WHERE run_id = ? "
            "ORDER BY priority, created_at, id",
            (stored_run_id,),
        ).fetchall()
        result_rows = connection.execute(
            "SELECT result_kind FROM processing_processingresult "
            "WHERE stage_id IN ("
            "SELECT id FROM processing_processingstage WHERE run_id = ?"
            ") ORDER BY result_kind",
            (stored_run_id,),
        ).fetchall()
    return {
        "run_status": run_row[0],
        "run_started": run_row[1] is not None,
        "run_finished": run_row[2] is not None,
        "stages": [
            {
                "kind": row[0],
                "status": row[1],
                "attempts": row[2],
                "task_id": row[3],
                "failure_code": row[4],
            }
            for row in stage_rows
        ],
        "result_kinds": [row[0] for row in result_rows],
    }


def _worker_log(log_path: Path, data_dir: Path) -> str:
    try:
        value = log_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        value = ""
    return _sanitized_output(value, data_dir)


def _start_worker(
    *,
    environment: dict[str, str],
    log_path: Path,
) -> subprocess.Popen:
    with log_path.open("w", encoding="utf-8") as log_file:
        return subprocess.Popen(
            [
                sys.executable,
                str(MANAGE_PY),
                "run_huey",
                "-w",
                "1",
                "-k",
                "thread",
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )


def _stop_worker(
    worker: subprocess.Popen,
    *,
    log_path: Path,
    data_dir: Path,
) -> None:
    if worker.poll() is not None:
        raise AssertionError(
            f"Huey worker exited before SIGINT with status {worker.returncode}.\n"
            f"{_worker_log(log_path, data_dir)}"
        )
    worker.send_signal(signal.SIGINT)
    try:
        return_code = worker.wait(timeout=WORKER_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        worker.terminate()
        try:
            worker.wait(timeout=2)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=2)
        raise AssertionError(
            "Huey worker did not stop within the bounded SIGINT grace period.\n"
            f"{_worker_log(log_path, data_dir)}"
        ) from None
    if return_code != 0:
        raise AssertionError(
            f"Huey worker handled SIGINT with status {return_code}.\n"
            f"{_worker_log(log_path, data_dir)}"
        )


def _wait_for_terminal_run(
    database_path: Path,
    run_id: str,
    worker: subprocess.Popen,
    *,
    log_path: Path,
    data_dir: Path,
) -> dict[str, object]:
    deadline = time.monotonic() + WORKFLOW_TIMEOUT_SECONDS
    last_snapshot = None
    while time.monotonic() < deadline:
        if worker.poll() is not None:
            raise AssertionError(
                f"Huey worker exited with status {worker.returncode}.\n"
                f"{_worker_log(log_path, data_dir)}"
            )
        try:
            last_snapshot = _run_snapshot(database_path, run_id)
        except sqlite3.OperationalError:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        if last_snapshot["run_status"] in {
            "succeeded",
            "partial",
            "failed",
            "superseded",
        }:
            return last_snapshot
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"Processing run did not finish within {WORKFLOW_TIMEOUT_SECONDS} seconds; "
        f"last durable state was {last_snapshot!r}.\n"
        f"{_worker_log(log_path, data_dir)}"
    )


def _assert_succeeded(snapshot: dict[str, object]) -> None:
    assert snapshot["run_status"] == "succeeded", snapshot
    assert snapshot["run_started"] is True
    assert snapshot["run_finished"] is True
    assert [stage["status"] for stage in snapshot["stages"]] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    assert [stage["attempts"] for stage in snapshot["stages"]] == [1, 1, 1]
    assert snapshot["result_kinds"] == [
        "authorship_candidates",
        "text_extraction",
        "writing_style",
    ]


def _assert_uuid_only_queue_payload(
    expected_stage_id: str,
    *,
    environment: dict[str, str],
    data_dir: Path,
) -> None:
    tasks = _json_from_script(
        _INSPECT_QUEUE_SCRIPT,
        environment=environment,
        data_dir=data_dir,
    )
    assert len(tasks) == 1, tasks
    assert tasks[0]["kwargs"] == {}
    assert tasks[0]["args"] == [expected_stage_id]
    assert uuid.UUID(tasks[0]["args"][0]) == uuid.UUID(expected_stage_id)


@pytest.mark.skipif(os.name != "posix", reason="graceful SIGINT requires POSIX")
def test_real_sqlite_worker_processes_uuid_tasks_across_restart_and_reconciliation(
    tmp_path,
):
    data_dir = tmp_path / "worker-data"
    environment = _subprocess_environment(data_dir)
    application_database = data_dir / "application.sqlite3"
    huey_database = data_dir / "huey.sqlite3"

    _run_checked(
        [sys.executable, str(MANAGE_PY), "migrate", "--noinput", "--verbosity", "0"],
        environment=environment,
        data_dir=data_dir,
    )

    first = _json_from_script(
        _CREATE_RUN_SCRIPT,
        FIRST_BODY,
        "enqueue",
        environment=environment,
        data_dir=data_dir,
    )
    first_queued = _run_snapshot(application_database, first["run_id"])
    assert [stage["status"] for stage in first_queued["stages"]] == [
        "queued",
        "pending",
        "pending",
    ]
    _assert_uuid_only_queue_payload(
        first["stage_id"], environment=environment, data_dir=data_dir
    )
    assert FIRST_BODY.encode() not in huey_database.read_bytes()

    first_log = tmp_path / "first-worker.log"
    first_worker = _start_worker(environment=environment, log_path=first_log)
    try:
        first_complete = _wait_for_terminal_run(
            application_database,
            first["run_id"],
            first_worker,
            log_path=first_log,
            data_dir=data_dir,
        )
        _assert_succeeded(first_complete)
    finally:
        _stop_worker(first_worker, log_path=first_log, data_dir=data_dir)

    second = _json_from_script(
        _CREATE_RUN_SCRIPT,
        SECOND_BODY,
        "stale",
        environment=environment,
        data_dir=data_dir,
    )
    stale = _run_snapshot(application_database, second["run_id"])
    assert [stage["status"] for stage in stale["stages"]] == [
        "queued",
        "pending",
        "pending",
    ]
    assert stale["stages"][0]["task_id"] == "lost-delivery"

    reconcile_output = _run_checked(
        [
            sys.executable,
            str(MANAGE_PY),
            "reconcile_processing",
            "--limit",
            "10",
            "--queued-timeout",
            "1",
        ],
        environment=environment,
        data_dir=data_dir,
    )
    assert "queued recovered=1" in reconcile_output
    reconciled = _run_snapshot(application_database, second["run_id"])
    assert reconciled["stages"][0]["status"] == "queued"
    assert reconciled["stages"][0]["task_id"] != "lost-delivery"
    _assert_uuid_only_queue_payload(
        second["stage_id"], environment=environment, data_dir=data_dir
    )
    assert SECOND_BODY.encode() not in huey_database.read_bytes()

    second_log = tmp_path / "second-worker.log"
    second_worker = _start_worker(environment=environment, log_path=second_log)
    try:
        second_complete = _wait_for_terminal_run(
            application_database,
            second["run_id"],
            second_worker,
            log_path=second_log,
            data_dir=data_dir,
        )
        _assert_succeeded(second_complete)
    finally:
        _stop_worker(second_worker, log_path=second_log, data_dir=data_dir)

    assert _run_snapshot(application_database, first["run_id"]) == first_complete
