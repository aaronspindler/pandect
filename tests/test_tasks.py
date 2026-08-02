import argparse
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

from pandect.core.management.commands.task_smoke import positive_timeout
from pandect.core.tasks import echo

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_demonstration_task_runs_eagerly_without_a_worker():
    result = echo("local-only")

    assert result.get(blocking=True, timeout=0.5) == "local-only"


def test_task_smoke_succeeds_in_eager_test_mode():
    output = StringIO()

    call_command("task_smoke", timeout=0.5, stdout=output)

    assert "Huey task smoke check passed" in output.getvalue()


def test_task_smoke_rejects_non_finite_or_excessive_timeouts():
    for value in ("nan", "inf", "0", "301"):
        with pytest.raises(argparse.ArgumentTypeError):
            positive_timeout(value)


def test_task_smoke_fails_cleanly_when_no_worker_is_running(tmp_path):
    environment = os.environ.copy()
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": "pandect.config.settings",
            "PANDECT_DATA_DIR": str(tmp_path),
            "PANDECT_DEBUG": "false",
            "PANDECT_SECRET_KEY": "test-only-subprocess-secret",
        }
    )

    completed = subprocess.run(
        [
            sys.executable,
            "manage.py",
            "task_smoke",
            "--timeout",
            "0.05",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode != 0
    assert "No Huey worker completed the task" in completed.stderr
    assert "Confirm the worker is running" in completed.stderr
