"""Hermetic tests of the executable-free media-runner wire boundary."""

import socket
import struct

import pytest

from pandect.media_runner import protocol

pytestmark = pytest.mark.media_runner


def request(**changes):
    value = {
        "version": 1,
        "operation": "probe_media",
        "parameters": {},
        "inputs": [{"name": "source", "size": 20}],
        "timeout_seconds": 60,
    }
    value.update(changes)
    return value


def test_json_roundtrip_over_unix_socket():
    sender, receiver = socket.socketpair()
    with sender, receiver:
        protocol.send_json(sender, request())
        assert protocol.receive_json(receiver) == request()


def test_error_codes_cannot_smuggle_sensitive_text():
    assert protocol.safe_error_code("resource_limit") == "resource_limit"
    assert protocol.safe_error_code("private-evidence") == "job_failed"
    assert protocol.safe_error_code(["tool_failed"]) == "job_failed"


@pytest.mark.parametrize(
    "value",
    [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b"[]", b"\xff"],
)
def test_json_rejects_ambiguous_or_nonfinite_values(value):
    with pytest.raises(protocol.RunnerProtocolError):
        protocol.decode_json(value)


def test_complex_json_is_bounded():
    nested = "x"
    for _ in range(34):
        nested = [nested]
    with pytest.raises(protocol.RunnerProtocolError):
        protocol.encode_json({"nested": nested})


@pytest.mark.parametrize("size", [0, protocol.MAX_JSON_BYTES + 1])
def test_json_length_is_rejected_before_body_read(size):
    sender, receiver = socket.socketpair()
    with sender, receiver:
        sender.sendall(struct.pack("!I", size))
        with pytest.raises(protocol.RunnerProtocolError):
            protocol.receive_json(receiver)


def test_short_message_is_rejected():
    sender, receiver = socket.socketpair()
    with sender, receiver:
        sender.sendall(struct.pack("!I", 10) + b"{}")
        sender.shutdown(socket.SHUT_WR)
        with pytest.raises(protocol.RunnerProtocolError):
            protocol.receive_json(receiver)


@pytest.mark.parametrize("name", ["../source", "/source", ".", "a/b", "a\\b", "", "💥"])
def test_only_simple_ascii_names_are_accepted(name):
    with pytest.raises(protocol.RunnerProtocolError):
        protocol.safe_name(name)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"operation": "shell"},
        {"operation": []},
        {"parameters": []},
        {"timeout_seconds": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": 7201},
        {"timeout_seconds": float("nan")},
        {"inputs": [{"name": "../source", "size": 1}]},
        {"inputs": [{"name": "source", "size": True}]},
        {"inputs": [{"name": "source", "size": -1}]},
        {"inputs": [{"name": "source", "size": protocol.MAX_INPUT_BYTES + 1}]},
        {"inputs": [{"name": "source", "size": 1}] * 2},
        {"inputs": [{"name": "source", "size": 1, "path": "/data/secret"}]},
        {"argv": ["sh"]},
    ],
)
def test_request_rejects_executable_unbounded_and_ambiguous_fields(changes):
    with pytest.raises(protocol.RunnerProtocolError):
        protocol.validate_request(request(**changes))


def test_file_boundaries_accept_exact_limit_and_reject_aggregate_overflow():
    assert protocol.file_manifest(
        [{"name": "source", "size": protocol.MAX_INPUT_BYTES}]
    )
    with pytest.raises(protocol.RunnerProtocolError):
        protocol.file_manifest(
            [
                {"name": "one", "size": protocol.MAX_INPUT_BYTES},
                {"name": "two", "size": 1},
            ]
        )


@pytest.mark.parametrize(
    "entries",
    [
        [{"name": "result.json", "key": "artifact", "size": 1}],
        [
            {"name": "one", "key": "artifact", "size": 1},
            {"name": "two", "key": "artifact", "size": 1},
        ],
        [
            {"name": "one", "key": "first", "size": 1},
            {"name": "one", "key": "second", "size": 1},
        ],
        [{"name": "one", "key": "artifact", "size": protocol.MAX_OUTPUT_BYTES + 1}],
    ],
)
def test_output_manifest_rejects_duplicates_reserved_names_and_overflow(entries):
    with pytest.raises(protocol.RunnerProtocolError):
        protocol.file_manifest(entries, output=True)
