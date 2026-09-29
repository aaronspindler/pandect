"""Suite-wide hermeticity guards."""

import socket

import pytest

_NETWORK_FAMILIES = {socket.AF_INET, socket.AF_INET6}
_BLOCKED_MESSAGE = "outbound network access is disabled in the hermetic test suite"


@pytest.fixture(autouse=True)
def explicit_extractor_unit_context(request, monkeypatch, tmp_path):
    """Exercise algorithms explicitly; production has no isolation-off flag.

    Routing/protocol tests opt out to test the actual fail-closed adapters.
    Real confinement is additionally tested by the Compose smoke suite.
    """
    if request.node.get_closest_marker("media_runner") is not None:
        yield
        return
    from pandect.media_runner.operations import dispatch
    from pandect.media_runner.routing import child_execution

    def in_process_operation(
        operation, *, inputs, parameters=None, output_dir=None, **_options
    ):
        result, outputs = dispatch(
            operation, inputs, parameters or {}, output_dir or tmp_path
        )
        result["outputs"] = {name: path.name for name, path in outputs.items()}
        return result

    monkeypatch.setattr(
        "pandect.media_runner.client.run_operation", in_process_operation
    )

    with child_execution():
        yield


@pytest.fixture(autouse=True)
def block_outbound_network(monkeypatch):
    """Reject real IP networking while leaving mock transports and IPC intact."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto

    def guarded_connect(sock, address):
        if sock.family in _NETWORK_FAMILIES:
            raise AssertionError(_BLOCKED_MESSAGE)
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if sock.family in _NETWORK_FAMILIES:
            raise AssertionError(_BLOCKED_MESSAGE)
        return original_connect_ex(sock, address)

    def guarded_sendto(sock, data, *args):
        if sock.family in _NETWORK_FAMILIES:
            raise AssertionError(_BLOCKED_MESSAGE)
        return original_sendto(sock, data, *args)

    def blocked_create_connection(*_args, **_kwargs):
        raise AssertionError(_BLOCKED_MESSAGE)

    def blocked_getaddrinfo(*_args, **_kwargs):
        raise AssertionError(_BLOCKED_MESSAGE)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)
    monkeypatch.setattr(socket, "create_connection", blocked_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", blocked_getaddrinfo)
