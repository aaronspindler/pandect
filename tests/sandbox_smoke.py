"""Drive the real, isolated runner from a separate unprivileged client container."""

import os
import socket
import sys
import time
from pathlib import Path

from pandect.media_runner.client import (
    RunnerBusy,
    RunnerError,
    RunnerJobError,
    run_operation,
    socket_path,
    status,
)
from pandect.media_runner.protocol import VERSION, receive_json, send_json


def wait_idle():
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not status()["busy"]:
            return
        time.sleep(0.05)
    raise AssertionError("The runner did not recover after the previous job")


def invoke(scenario, *, timeout=5, **parameters):
    return run_operation(
        "tool_version",
        inputs={"source": Path("/tmp/input.bin")} if scenario == "good" else {},
        parameters={"scenario": scenario, **parameters},
        output_dir=Path("/tmp/output"),
        timeout_seconds=timeout,
        reserve_bytes=0,  # Synthetic output tmpfs is deliberately only 16 MiB.
        expected_outputs={"artifact": "artifact.bin"} if scenario == "good" else {},
    )


def good(previous=None, *, absent_pids=()):
    wait_idle()
    try:
        result = invoke(
            "good",
            previous_job=(previous or {}).get("job"),
            absent_pids=list(absent_pids),
        )
    except RunnerJobError as error:
        raise AssertionError(
            f"Baseline fixture failed with code {error.code}"
        ) from error
    assert "boundary_failure" not in result, result
    assert Path("/tmp/output/artifact.bin").read_bytes() == b"verified isolated output"
    Path("/tmp/output/artifact.bin").unlink()
    if previous:
        assert result["uid"] > previous["uid"]
        assert result["job"] != previous["job"]
    return result


def check_disconnect_and_busy():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(socket_path())
        send_json(
            connection,
            {
                "version": VERSION,
                "operation": "tool_version",
                "parameters": {"scenario": "disconnect"},
                "inputs": [],
                "timeout_seconds": 5,
            },
        )
        assert receive_json(connection)["status"] == "ready"
        assert status()["busy"] is True
        started = time.monotonic()
        try:
            invoke("good")
        except RunnerBusy:
            pass
        else:
            raise AssertionError("A second concurrent job was incorrectly accepted")
        assert time.monotonic() - started < 2
        time.sleep(0.3)
    # Closing the socket must cancel the job before its five-second deadline.
    started = time.monotonic()
    wait_idle()
    assert time.monotonic() - started < 3


def main():
    if sys.argv[1:] == ["--unauthorized-peer"]:
        try:
            status()
        except RunnerError:
            print("PASS: wrong application UID rejected despite matching socket group")
            return
        raise AssertionError("An unauthorized application UID reached the supervisor")
    assert not sys.argv[1:]
    assert os.environ["PANDECT_CLOUDFLARE_API_TOKEN"] == "synthetic-client-only-token"
    Path("/tmp/client-only-secret").write_bytes(
        b"synthetic secret never mounted in runner"
    )
    Path("/tmp/input.bin").write_bytes(b"synthetic input only")
    Path("/tmp/output").mkdir()
    invoke("minimal")
    print("PASS: confined child bootstrap and result delivery")
    previous = good()
    print(
        "PASS: capabilities, identities, mounts, descriptors, IPC and network boundary"
    )
    orphan = invoke("orphan")["orphan_pid"]
    previous = good(previous, absent_pids=[orphan])
    print("PASS: double-forked setsid descendant killed and reaped")
    children = invoke("pid_limit")["children"]
    assert 0 < len(children) < 32
    previous = good(previous, absent_pids=children)
    print("PASS: PID exhaustion bounded; no retained processes")
    for scenario in (
        "mode_zero",
        "symlink",
        "hardlink",
        "fifo",
        "traversal",
        "output_count",
        "output_bytes",
        "file_size",
        "scratch_bytes",
        "scratch_inodes",
        "memory",
        "invalid_json",
        "crash",
        "timeout",
    ):
        wait_idle()
        try:
            invoke(scenario, timeout=0.5 if scenario == "timeout" else 5)
        except RunnerJobError as error:
            if scenario == "timeout":
                assert error.code == "tool_timed_out", f"Timeout returned {error.code}"
        else:
            raise AssertionError(f"Unsafe {scenario} output or job was accepted")
        previous = good(previous)
        print(f"PASS: {scenario} rejected; clean subsequent job")
    check_disconnect_and_busy()
    good(previous)
    print("PASS: busy response and disconnect cancellation; clean subsequent job")


if __name__ == "__main__":
    main()
