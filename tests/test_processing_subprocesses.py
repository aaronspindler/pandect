import hashlib
import io
import os
import sys
import time

import pytest

from pandect.processing.extractors.errors import (
    ToolFailedError,
    ToolOutputLimitError,
    ToolTimedOutError,
    ToolUnavailableError,
)
from pandect.processing.extractors.hashing import (
    fingerprint_file,
    fingerprint_stream,
)
from pandect.processing.extractors.subprocesses import run_command


def test_fingerprint_stream_hashes_all_bytes_and_restores_position():
    stream = io.BytesIO(b"canonical evidence")
    stream.seek(5)

    fingerprint = fingerprint_stream(stream, chunk_size=3)

    assert fingerprint.sha256 == hashlib.sha256(b"canonical evidence").hexdigest()
    assert fingerprint.size_bytes == len(b"canonical evidence")
    assert stream.tell() == 5


def test_fingerprint_file_streams_the_whole_file(tmp_path):
    path = tmp_path / "evidence.bin"
    path.write_bytes(b"abc" * 100)

    fingerprint = fingerprint_file(path, chunk_size=7)

    assert fingerprint.sha256 == hashlib.sha256(b"abc" * 100).hexdigest()
    assert fingerprint.size_bytes == 300


def test_run_command_uses_argv_and_captures_bounded_binary_output():
    result = run_command(
        (sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'facts')"),
        timeout_seconds=2,
        output_limit_bytes=16,
    )

    assert result.stdout == b"facts"
    assert result.stderr == b""
    assert result.returncode == 0


def test_run_command_failure_does_not_expose_arguments_or_tool_output():
    secret = "sensitive-evidence-value"

    with pytest.raises(ToolFailedError) as captured:
        run_command(
            (
                sys.executable,
                "-c",
                f"import sys;sys.stderr.write('{secret}');sys.exit(7)",
            ),
            timeout_seconds=2,
        )

    assert captured.value.returncode == 7
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)


def test_run_command_stops_when_combined_output_exceeds_limit():
    with pytest.raises(ToolOutputLimitError):
        run_command(
            (sys.executable, "-c", "print('x' * 4096)"),
            timeout_seconds=2,
            output_limit_bytes=64,
        )


def test_run_command_stops_at_deadline():
    with pytest.raises(ToolTimedOutError):
        run_command(
            (sys.executable, "-c", "import time;time.sleep(5)"),
            timeout_seconds=0.05,
        )


def test_run_command_reports_progress_while_a_quiet_tool_is_running():
    progress = []

    run_command(
        (sys.executable, "-c", "import time;time.sleep(0.08)"),
        timeout_seconds=2,
        progress_callback=lambda: progress.append(True),
        progress_interval_seconds=0.01,
    )

    assert progress


def test_run_command_kills_the_tool_when_progress_callback_loses_its_lease():
    def lease_lost():
        raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        run_command(
            (sys.executable, "-c", "import time;time.sleep(5)"),
            timeout_seconds=10,
            progress_callback=lease_lost,
            progress_interval_seconds=0.01,
        )


@pytest.mark.skipif(os.name != "posix", reason="process groups require POSIX")
def test_timeout_kills_child_holding_pipes_after_group_leader_exits(tmp_path):
    survived = tmp_path / "child-survived"
    child_program = (
        "import pathlib,sys,time;"
        "time.sleep(0.5);"
        "pathlib.Path(sys.argv[1]).write_text('survived');"
        "time.sleep(0.5)"
    )
    parent_program = (
        "import subprocess,sys;"
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]])"
    )

    with pytest.raises(ToolTimedOutError):
        run_command(
            (
                sys.executable,
                "-c",
                parent_program,
                child_program,
                str(survived),
            ),
            timeout_seconds=0.1,
        )

    # Without process-group cleanup, the grandchild outlives its exited parent
    # and writes this marker. Allow enough time for that observable side effect
    # while keeping the regression bounded.
    time.sleep(0.6)
    assert not survived.exists()


def test_run_command_reports_missing_binary_without_its_path():
    binary = "/definitely/not/a/real/local-tool"

    with pytest.raises(ToolUnavailableError) as captured:
        run_command((binary,), timeout_seconds=1)

    assert binary not in str(captured.value)


@pytest.mark.parametrize("argv", ["not-an-argv", (), ("",), ("tool\0name",)])
def test_run_command_rejects_invalid_argv(argv):
    with pytest.raises(ValueError):
        run_command(argv, timeout_seconds=1)
