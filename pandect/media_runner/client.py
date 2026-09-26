"""Trusted clients never decode media and never fall back to local execution."""

import os
import select
import socket
import stat
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

from .protocol import (
    CHUNK_BYTES,
    MAX_TIMEOUT_SECONDS,
    VERSION,
    RunnerBusy,
    RunnerError,
    RunnerJobError,
    RunnerProtocolError,
    RunnerUnavailable,
    file_manifest,
    peer_uid,
    receive_exact,
    receive_json,
    safe_error_code,
    safe_name,
    send_json,
    validate_request,
)

__all__ = [
    "RunnerBusy",
    "RunnerError",
    "RunnerJobError",
    "RunnerProtocolError",
    "RunnerUnavailable",
    "run_operation",
    "status",
]


def socket_path():
    return os.environ.get("PANDECT_RUNNER_SOCKET", "/run/pandect-media/runner.sock")


def _connect():
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5)
    try:
        connection.connect(socket_path())
        if peer_uid(connection) != 0:
            raise RunnerUnavailable("The media runner supervisor is not trusted.")
    except BaseException:
        connection.close()
        raise
    return connection


def _check_response(response, expected):
    if type(response.get("version")) is not int or response["version"] != VERSION:
        raise RunnerProtocolError("Unsupported media-runner response version.")
    state = response.get("status")
    if state == "busy":
        if set(response) != {"version", "status"}:
            raise RunnerProtocolError("Invalid media-runner busy response.")
        raise RunnerBusy(
            "Media processing is busy. Retry the import after the current job finishes."
        )
    if state == "failed":
        if set(response) != {"version", "status", "error_code"}:
            raise RunnerProtocolError("Invalid media-runner failure response.")
        code = safe_error_code(response.get("error_code"))
        raise RunnerJobError(
            "Media processing failed safely. Check the processing stage and retry.",
            code=code,
        )
    if state != expected:
        raise RunnerProtocolError("Unexpected media-runner response.")
    if state == "ready" and set(response) != {"version", "status"}:
        raise RunnerProtocolError("Invalid media-runner readiness response.")


def status():
    """Readiness includes a busy runner; callers can separately display capacity."""
    try:
        with _connect() as connection:
            send_json(connection, {"version": VERSION, "operation": "status"})
            response = receive_json(connection, deadline=time.monotonic() + 5)
            _check_response(response, "ok")
            if (
                set(response) != {"version", "status", "busy"}
                or type(response.get("busy")) is not bool
            ):
                raise RunnerProtocolError("Invalid media-runner status.")
            return response
    except OSError as exc:
        raise RunnerUnavailable(
            "Media processing is unavailable. "
            "Start or inspect the media-runner service."
        ) from exc


def _stream_input(connection, source, size, deadline, progress_callback):
    remaining = size
    while remaining:
        time_left = deadline - time.monotonic()
        if time_left <= 0:
            raise TimeoutError("The media-runner transfer deadline expired.")
        connection.settimeout(time_left)
        data = source.read(min(CHUNK_BYTES, remaining))
        if not data:
            raise RunnerProtocolError("The media input changed during transfer.")
        connection.sendall(data)
        remaining -= len(data)
        progress_callback()
    if source.read(1):
        raise RunnerProtocolError("The media input changed during transfer.")


def _check_output_space(directory_fd, required_bytes, reserve_bytes):
    usage = os.fstatvfs(directory_fd)
    if usage.f_bavail * usage.f_frsize < required_bytes + reserve_bytes:
        raise RunnerUnavailable(
            "The data volume is too full to safely store this result. "
            "Free space or move Pandect to a larger volume, then retry."
        )


def _validate_result(operation, result):
    from pandect.processing.extractors.errors import InvalidExtractorOutputError

    from .validation import validate_operation_result

    direct_result = operation in {"decode_qr", "tool_version", "model_smoke"}
    try:
        if not direct_result and set(result) != {"value"}:
            raise InvalidExtractorOutputError
        validate_operation_result(
            operation, result if direct_result else result["value"]
        )
    except InvalidExtractorOutputError as exc:
        raise RunnerProtocolError(
            "The media runner returned invalid operation data."
        ) from exc


def _receive_outputs(
    connection, manifest, output_dir, deadline, *, reserve_bytes, progress_callback
):
    if manifest and output_dir is None:
        raise RunnerProtocolError("The media operation returned unexpected artifacts.")
    if not manifest:
        return {}
    directory_fd = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    pending = []
    published = []
    try:
        _check_output_space(
            directory_fd, sum(entry["size"] for entry in manifest), reserve_bytes
        )
        for entry in manifest:
            temporary = f".runner-{uuid.uuid4().hex}"
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            pending.append(temporary)
            with os.fdopen(fd, "wb") as target:
                remaining = entry["size"]
                while remaining:
                    connection.settimeout(max(0.001, deadline - time.monotonic()))
                    data = receive_exact(
                        connection, min(CHUNK_BYTES, remaining), deadline=deadline
                    )
                    _check_output_space(directory_fd, len(data), reserve_bytes)
                    target.write(data)
                    remaining -= len(data)
                    progress_callback()
                target.flush()
                os.fsync(target.fileno())
        for temporary, entry in zip(pending, manifest, strict=True):
            os.link(
                temporary,
                entry["name"],
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            published.append(entry["name"])
        os.fsync(directory_fd)
        return {entry["key"]: entry["name"] for entry in manifest}
    except BaseException:
        for name in published:
            os.unlink(name, dir_fd=directory_fd)
        raise
    finally:
        for name in pending:
            os.unlink(name, dir_fd=directory_fd)
        os.close(directory_fd)


def run_operation(
    operation,
    *,
    inputs,
    parameters=None,
    output_dir=None,
    timeout_seconds=MAX_TIMEOUT_SECONDS,
    expected_outputs=None,
    progress_callback=None,
    reserve_bytes=2 * 1024**3,
):
    """Run one fixed operation; publish artifacts only to caller-owned scratch.

    ``result['outputs']`` maps logical output keys to safe basenames. The caller
    still validates its operation schema and hashes files before evidence publication.
    No parser-provided path is used as a destination.
    """
    if type(reserve_bytes) is not int or reserve_bytes < 0:
        raise RunnerProtocolError("Invalid media-runner storage reserve.")
    next_progress = time.monotonic() + 5

    def report_progress():
        nonlocal next_progress
        if progress_callback is not None and time.monotonic() >= next_progress:
            progress_callback()
            next_progress = time.monotonic() + 5

    try:
        with ExitStack() as stack:
            sources = {}
            manifest = []
            for name, source_path in inputs.items():
                safe_name(name)
                fd = os.open(
                    Path(source_path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                )
                source = stack.enter_context(os.fdopen(fd, "rb"))
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise RunnerProtocolError("A media input is not a regular file.")
                sources[name] = source
                manifest.append({"name": name, "size": info.st_size})
            request = validate_request(
                {
                    "version": VERSION,
                    "operation": operation,
                    "parameters": parameters if parameters is not None else {},
                    "inputs": manifest,
                    "timeout_seconds": timeout_seconds,
                }
            )
            connection = stack.enter_context(_connect())
            send_json(connection, request)
            _check_response(
                receive_json(connection, deadline=time.monotonic() + 5), "ready"
            )
            # Transfer is separately bounded so processing retains its own deadline.
            transfer_deadline = time.monotonic() + 300
            for entry in manifest:
                _stream_input(
                    connection,
                    sources[entry["name"]],
                    entry["size"],
                    transfer_deadline,
                    report_progress,
                )
            deadline = time.monotonic() + timeout_seconds + 10
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("The media-runner job deadline expired.")
                if select.select([connection], [], [], min(5, remaining))[0]:
                    break
                report_progress()
            response = receive_json(connection, deadline=deadline)
            _check_response(response, "ok")
            if set(response) != {"version", "status", "result", "outputs"}:
                raise RunnerProtocolError("Invalid media-runner result fields.")
            result = response["result"]
            if not isinstance(result, dict) or "outputs" in result:
                raise RunnerProtocolError("Invalid media-runner result.")
            manifest = file_manifest(response["outputs"], output=True)
            actual_outputs = {entry["key"]: entry["name"] for entry in manifest}
            if actual_outputs != (expected_outputs or {}):
                raise RunnerProtocolError("Unexpected media-runner output artifacts.")
            _validate_result(operation, result)
            result["outputs"] = _receive_outputs(
                connection,
                manifest,
                output_dir,
                time.monotonic() + 300,
                reserve_bytes=reserve_bytes,
                progress_callback=report_progress,
            )
            return result
    except OSError as exc:
        raise RunnerUnavailable(
            "Media processing or artifact transfer is unavailable. "
            "Check the runner and available disk space, then retry."
        ) from exc
