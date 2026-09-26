"""Supervisor policy unit tests; process confinement runs in sandbox-smoke."""

import os
import socket
from pathlib import Path

import pytest

from pandect.media_runner import confinement, protocol, server

pytestmark = pytest.mark.media_runner


@pytest.fixture
def local_ownership(monkeypatch):
    # Unit fixtures belong to the unprivileged test user; container tests exercise
    # the real root-only CHOWN transitions with no DAC_OVERRIDE capability.
    monkeypatch.setattr(os, "chown", lambda *_args, **_kwargs: None)


def test_kill_broadcast_cannot_run_outside_root_pid_one(monkeypatch):
    monkeypatch.setattr(os, "getpid", lambda: 42)
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("Unsafe kill broadcast"))
    with pytest.raises(RuntimeError, match="PID 1"):
        server.kill_and_reap()


def test_shared_tree_cleanup_does_not_follow_links(tmp_path, local_ownership):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evidence").write_bytes(b"canonical")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    job = scratch / "job"
    job.mkdir()
    (job / "link").symlink_to(outside, target_is_directory=True)
    (job / "file").write_bytes(b"temporary")
    descriptor = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY)
    try:
        server.remove_tree_at(descriptor, "job")
    finally:
        os.close(descriptor)
    assert list(scratch.iterdir()) == []
    assert (outside / "evidence").read_bytes() == b"canonical"


def _output_job(tmp_path, outputs=None):
    job = tmp_path / "job"
    output = job / "output"
    output.mkdir(parents=True)
    (output / "result.json").write_bytes(
        protocol.encode_json(
            {
                "result": {"value": "ok"},
                "outputs": outputs or {},
            }
        )
    )
    return job, output


def test_collect_outputs_validates_and_keeps_open_file_descriptors(
    tmp_path, local_ownership
):
    job, output = _output_job(tmp_path, {"artifact": "frame.png"})
    (output / "frame.png").write_bytes(b"derived")
    result, manifest, descriptors = server.collect_outputs(job, maximum_bytes=1024)
    try:
        assert result == {"value": "ok"}
        assert manifest == [{"key": "artifact", "name": "frame.png", "size": 7}]
        assert os.read(descriptors[0], 7) == b"derived"
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "hardlink", "extra"])
def test_collect_outputs_rejects_untrusted_filesystem_entries(
    tmp_path, local_ownership, kind
):
    job, output = _output_job(tmp_path, {"artifact": "artifact"})
    artifact = output / "artifact"
    if kind == "symlink":
        artifact.symlink_to(output / "result.json")
    elif kind == "fifo":
        os.mkfifo(artifact)
    elif kind == "directory":
        artifact.mkdir()
    elif kind == "hardlink":
        os.link(output / "result.json", artifact)
    else:
        artifact.write_bytes(b"valid")
        (output / "unlisted").write_bytes(b"unexpected")
    with pytest.raises(protocol.RunnerProtocolError):
        server.collect_outputs(job, maximum_bytes=1024)


def test_collect_outputs_rejects_total_size_overflow(tmp_path, local_ownership):
    job, output = _output_job(tmp_path, {"one": "one", "two": "two"})
    (output / "one").write_bytes(b"123456")
    (output / "two").write_bytes(b"123456")
    with pytest.raises(protocol.RunnerProtocolError):
        server.collect_outputs(job, maximum_bytes=10)


def test_job_directories_remain_traversable_with_private_umask(
    tmp_path, local_ownership
):
    supervisor = server.Supervisor(
        socket_path=tmp_path / "socket",
        scratch=tmp_path,
        app_uid=501,
        app_gid=20,
    )
    old_mask = os.umask(0o077)
    try:
        job = supervisor._create_job(
            {
                "operation": "tool_version",
                "inputs": [],
                "parameters": {},
                "timeout_seconds": 1,
            },
            20_000,
        )
    finally:
        os.umask(old_mask)
    assert job.stat().st_mode & 0o777 == 0o750
    assert (job / "input").stat().st_mode & 0o777 == 0o750
    assert (job / "request.json").stat().st_mode & 0o777 == 0o440


def test_fresh_job_identity_never_reuses_application_identity(tmp_path):
    supervisor = server.Supervisor(
        socket_path=tmp_path / "socket",
        scratch=tmp_path,
        app_uid=20_000,
        app_gid=20_001,
    )
    assert supervisor._identity() == 20_002
    assert supervisor._identity() == 20_003


def test_unauthorized_peer_is_rejected_before_json_is_read(tmp_path, monkeypatch):
    supervisor = server.Supervisor(
        socket_path=tmp_path / "socket",
        scratch=tmp_path,
        app_uid=501,
        app_gid=20,
    )
    monkeypatch.setattr(server, "peer_uid", lambda _connection: 20_000)
    monkeypatch.setattr(
        server,
        "receive_json",
        lambda *_args, **_kwargs: pytest.fail("Read unauthorized peer"),
    )
    application, runner = socket.socketpair()
    with application:
        supervisor.connection_slots.acquire()
        supervisor.handle(runner)
        assert application.recv(1) == b""


def test_confinement_bootstrap_contains_no_configurable_executable():
    source = Path(confinement.__file__).read_text()
    assert '"-m", "pandect.media_runner.child"' in source
    assert "preexec_fn" not in source


def test_bootstrap_drops_identity_before_entering_child_private_directory(
    tmp_path, monkeypatch
):
    events = []
    control = tmp_path / "request.json"
    output = tmp_path / "output"
    control.write_bytes(
        protocol.encode_json(
            {
                "output_dir": str(output),
                "timeout_seconds": 10,
            }
        )
    )
    monkeypatch.setattr(confinement.sys, "platform", "linux")
    monkeypatch.setattr(
        confinement.sys, "argv", ["bootstrap", str(control), "20000", "20000"]
    )
    monkeypatch.setattr(confinement.os, "geteuid", lambda: 0)
    monkeypatch.setattr(confinement.os, "umask", lambda _mask: 0)
    monkeypatch.setattr(confinement.resource, "setrlimit", lambda *_args: None)
    monkeypatch.setattr(confinement, "deny_sysv_ipc", lambda: events.append("seccomp"))
    monkeypatch.setattr(
        confinement, "drop_identity", lambda *_args: events.append("drop")
    )
    monkeypatch.setattr(confinement.os, "chdir", lambda _path: events.append("chdir"))

    def execute(_executable, argv, environment):
        events.append("exec")
        assert argv[1:] == ["-m", "pandect.media_runner.child", str(control)]
        assert "PANDECT_CLOUDFLARE_API_TOKEN" not in environment
        assert "PANDECT_DATA_DIR" not in environment
        assert environment["TMPDIR"] == str(output)

    monkeypatch.setattr(confinement.os, "execve", execute)
    confinement.main()
    assert events == ["seccomp", "drop", "chdir", "exec"]
