"""No subprocess or native decoder is used by trusted client tests."""

import socket
import threading
from types import SimpleNamespace

import pytest

from pandect.media_runner import client, protocol

pytestmark = pytest.mark.media_runner


@pytest.fixture
def fake_runner(monkeypatch):
    threads = []
    errors = []

    def install(handler):
        application, server = socket.socketpair()
        monkeypatch.setattr(client, "_connect", lambda: application)

        def serve():
            try:
                with server:
                    handler(server)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        threads.append(thread)

    yield install
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert not errors


def test_status_reports_busy_as_ready(fake_runner):
    def handler(connection):
        assert protocol.receive_json(connection) == {
            "version": 1,
            "operation": "status",
        }
        protocol.send_json(connection, {"version": 1, "status": "ok", "busy": True})

    fake_runner(handler)
    assert client.status()["busy"] is True


def test_busy_fails_before_input_bytes_are_sent(fake_runner, tmp_path):
    source = tmp_path / "original"
    source.write_bytes(b"private evidence")

    def handler(connection):
        protocol.receive_json(connection)
        protocol.send_json(connection, {"version": 1, "status": "busy"})
        assert connection.recv(1) == b""

    fake_runner(handler)
    with pytest.raises(client.RunnerBusy):
        client.run_operation("probe_media", inputs={"source": source})


def test_success_streams_input_and_materializes_only_expected_output(
    fake_runner, tmp_path
):
    source = tmp_path / "original"
    source.write_bytes(b"original")
    output_dir = tmp_path / "work"
    output_dir.mkdir()

    def handler(connection):
        request = protocol.receive_json(connection)
        assert request["inputs"] == [{"name": "source", "size": 8}]
        assert "original" not in str(request)
        protocol.send_json(connection, {"version": 1, "status": "ready"})
        assert protocol.receive_exact(connection, 8) == b"original"
        protocol.send_json(
            connection,
            {
                "version": 1,
                "status": "ok",
                "result": {
                    "value": {
                        "width": 1,
                        "height": 1,
                        "pixel_count": 1,
                        "frame_index": 0,
                    }
                },
                "outputs": [{"key": "artifact", "name": "thumbnail.png", "size": 4}],
            },
        )
        connection.sendall(b"fake")

    fake_runner(handler)
    result = client.run_operation(
        "render_image_frame",
        inputs={"source": source},
        output_dir=output_dir,
        expected_outputs={"artifact": "thumbnail.png"},
        reserve_bytes=0,
    )
    assert result == {
        "value": {"width": 1, "height": 1, "pixel_count": 1, "frame_index": 0},
        "outputs": {"artifact": "thumbnail.png"},
    }
    assert (output_dir / "thumbnail.png").read_bytes() == b"fake"
    assert (output_dir / "thumbnail.png").stat().st_mode & 0o777 == 0o600
    assert sorted(path.name for path in output_dir.iterdir()) == ["thumbnail.png"]
    assert source.read_bytes() == b"original"


@pytest.mark.parametrize("name", ["unexpected", "../secret"])
def test_hostile_output_names_are_rejected_before_any_copy(fake_runner, tmp_path, name):
    def handler(connection):
        protocol.receive_json(connection)
        protocol.send_json(connection, {"version": 1, "status": "ready"})
        protocol.send_json(
            connection,
            {
                "version": 1,
                "status": "ok",
                "result": {"version": "synthetic"},
                "outputs": [{"key": "artifact", "name": name, "size": 4}],
            },
        )

    fake_runner(handler)
    with pytest.raises(client.RunnerProtocolError):
        client.run_operation(
            "tool_version",
            inputs={},
            output_dir=tmp_path,
            expected_outputs={"artifact": "expected"},
        )
    assert list(tmp_path.iterdir()) == []


def test_incomplete_batch_leaves_no_partial_outputs(fake_runner, tmp_path):
    def handler(connection):
        protocol.receive_json(connection)
        protocol.send_json(connection, {"version": 1, "status": "ready"})
        protocol.send_json(
            connection,
            {
                "version": 1,
                "status": "ok",
                "result": {"version": "synthetic"},
                "outputs": [
                    {"key": "one", "name": "one", "size": 3},
                    {"key": "two", "name": "two", "size": 5},
                ],
            },
        )
        connection.sendall(b"onebad")

    fake_runner(handler)
    with pytest.raises(client.RunnerProtocolError):
        client.run_operation(
            "tool_version",
            inputs={},
            output_dir=tmp_path,
            expected_outputs={"one": "one", "two": "two"},
            reserve_bytes=0,
        )
    assert list(tmp_path.iterdir()) == []


def test_existing_output_is_never_overwritten(fake_runner, tmp_path):
    existing = tmp_path / "artifact"
    existing.write_bytes(b"existing evidence")

    def handler(connection):
        protocol.receive_json(connection)
        protocol.send_json(connection, {"version": 1, "status": "ready"})
        protocol.send_json(
            connection,
            {
                "version": 1,
                "status": "ok",
                "result": {"version": "synthetic"},
                "outputs": [{"key": "artifact", "name": "artifact", "size": 4}],
            },
        )
        connection.sendall(b"fake")

    fake_runner(handler)
    with pytest.raises(client.RunnerUnavailable):
        client.run_operation(
            "tool_version",
            inputs={},
            output_dir=tmp_path,
            expected_outputs={"artifact": "artifact"},
            reserve_bytes=0,
        )
    assert existing.read_bytes() == b"existing evidence"
    assert list(tmp_path.iterdir()) == [existing]


def test_known_failure_code_survives_without_untrusted_details(fake_runner):
    def handler(connection):
        protocol.receive_json(connection)
        protocol.send_json(connection, {"version": 1, "status": "ready"})
        protocol.send_json(
            connection,
            {
                "version": 1,
                "status": "failed",
                "error_code": "resource_limit",
            },
        )

    fake_runner(handler)
    with pytest.raises(client.RunnerJobError) as caught:
        client.run_operation("tool_version", inputs={})
    assert caught.value.code == "resource_limit"


def test_unavailable_runner_never_falls_back_to_execution(monkeypatch):
    def unavailable():
        raise FileNotFoundError("socket")

    monkeypatch.setattr(client, "_connect", unavailable)
    with pytest.raises(client.RunnerUnavailable):
        client.run_operation("tool_version", inputs={})


def test_storage_reserve_accepts_exact_boundary_and_rejects_overflow(monkeypatch):
    monkeypatch.setattr(
        client.os, "fstatvfs", lambda _fd: SimpleNamespace(f_bavail=100, f_frsize=1)
    )
    client._check_output_space(1, required_bytes=20, reserve_bytes=80)
    with pytest.raises(client.RunnerUnavailable):
        client._check_output_space(1, required_bytes=21, reserve_bytes=80)


def test_mid_stream_space_exhaustion_removes_only_provisional_outputs(
    fake_runner, tmp_path, monkeypatch
):
    calls = 0

    def space(_fd):
        nonlocal calls
        calls += 1
        return SimpleNamespace(f_bavail=1024 if calls == 1 else 0, f_frsize=1)

    monkeypatch.setattr(client.os, "fstatvfs", space)

    def handler(connection):
        protocol.receive_json(connection)
        protocol.send_json(connection, {"version": 1, "status": "ready"})
        protocol.send_json(
            connection,
            {
                "version": 1,
                "status": "ok",
                "result": {"version": "synthetic"},
                "outputs": [{"key": "artifact", "name": "artifact", "size": 4}],
            },
        )
        connection.sendall(b"fake")

    fake_runner(handler)
    with pytest.raises(client.RunnerUnavailable):
        client.run_operation(
            "tool_version",
            inputs={},
            output_dir=tmp_path,
            expected_outputs={"artifact": "artifact"},
            reserve_bytes=0,
        )
    assert calls == 2
    assert list(tmp_path.iterdir()) == []
