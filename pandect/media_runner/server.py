"""A single-job PID-1 supervisor. It never imports a media parser or Django."""

import errno
import os
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .confinement import require_supervisor_boundary, restrict_shared_state
from .protocol import (
    CHUNK_BYTES,
    MAX_FILES,
    MAX_JSON_BYTES,
    VERSION,
    RunnerError,
    RunnerJobError,
    RunnerProtocolError,
    decode_json,
    encode_json,
    file_manifest,
    peer_uid,
    receive_exact,
    receive_json,
    safe_error_code,
    safe_name,
    send_json,
    validate_request,
)


class _ResponseStarted(Exception):
    """Close rather than append an error frame to an artifact byte stream."""


def _owned_directory(parent_fd, name):
    """Claim child-created directories only after the whole namespace is quiescent."""
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise RunnerProtocolError("The media runner returned an invalid directory.")
    os.chown(name, 0, 0, dir_fd=parent_fd, follow_symlinks=False)
    os.chmod(name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)


def _owned_file(directory_fd, name, *, size_limit):
    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > size_limit
    ):
        raise RunnerProtocolError("The media runner returned an unsafe artifact.")
    os.chown(name, 0, 0, dir_fd=directory_fd, follow_symlinks=False)
    os.chmod(name, 0o600, dir_fd=directory_fd, follow_symlinks=False)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    if os.fstat(fd).st_ino != info.st_ino:
        os.close(fd)
        raise RunnerProtocolError("The media-runner artifact changed unexpectedly.")
    return fd, info.st_size


def remove_tree_at(parent_fd, name, *, depth=0, budget=None):
    """Never follow parser-created symlinks, including during failure cleanup."""
    if budget is None:
        budget = [8192]
    budget[0] -= 1
    if depth > 64 or budget[0] < 0:
        raise RuntimeError("The media-runner scratch tree cannot be safely reclaimed.")
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    directory_fd = _owned_directory(parent_fd, name)
    try:
        for child in os.listdir(directory_fd):
            remove_tree_at(directory_fd, child, depth=depth + 1, budget=budget)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


def kill_and_reap():
    """PID 1 owns all orphan descendants; process groups alone are insufficient.

    The repeated guard is deliberate: this function must never broadcast signals
    from a host development process, even if called independently of ``serve``.
    """
    require_supervisor_boundary()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(-1, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            time.sleep(0.01)
    # Retaining one hostile process would cross the next job's security boundary.
    os._exit(70)


def collect_outputs(job, *, maximum_bytes):
    """Inspect only after descendants are dead; retain opened FDs for transfer."""
    job_fd = os.open(job, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    output_fd = None
    opened = []
    try:
        output_fd = _owned_directory(job_fd, "output")
        names = os.listdir(output_fd)
        if len(names) > MAX_FILES + 1:
            raise RunnerProtocolError("The media runner returned too many artifacts.")
        for name in names:
            safe_name(name)
            info = os.stat(name, dir_fd=output_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RunnerProtocolError(
                    "The media runner returned an unsafe artifact."
                )
        result_fd, _size = _owned_file(
            output_fd, "result.json", size_limit=MAX_JSON_BYTES
        )
        with os.fdopen(result_fd, "rb") as result_file:
            envelope = decode_json(result_file.read(MAX_JSON_BYTES + 1))
        if set(envelope) == {"error_code"}:
            raise RunnerJobError(
                "Media processing failed.", code=safe_error_code(envelope["error_code"])
            )
        if set(envelope) != {"result", "outputs"}:
            raise RunnerProtocolError("Invalid media-runner child result fields.")
        result, outputs = envelope["result"], envelope["outputs"]
        if (
            not isinstance(result, dict)
            or "outputs" in result
            or not isinstance(outputs, dict)
            or len(outputs) > MAX_FILES
        ):
            raise RunnerProtocolError("Invalid media-runner child result.")
        if set(names) != {"result.json", *outputs.values()}:
            raise RunnerProtocolError("The media runner left unexpected scratch files.")
        manifest = []
        for key, name in outputs.items():
            safe_name(key)
            safe_name(name)
            fd, size = _owned_file(output_fd, name, size_limit=maximum_bytes)
            opened.append(fd)
            manifest.append({"key": key, "name": name, "size": size})
        file_manifest(manifest, output=True)
        if sum(entry["size"] for entry in manifest) > maximum_bytes:
            raise RunnerProtocolError("Media-runner output exceeds its resource limit.")
        return result, manifest, opened
    except BaseException:
        for fd in opened:
            os.close(fd)
        raise
    finally:
        if output_fd is not None:
            os.close(output_fd)
        os.close(job_fd)


class Supervisor:
    def __init__(self, *, socket_path, scratch, app_uid, app_gid):
        self.socket_path = Path(socket_path)
        self.scratch = Path(scratch)
        self.app_uid = app_uid
        self.app_gid = app_gid
        self.job_lock = threading.Lock()
        self.connection_slots = threading.BoundedSemaphore(8)
        self.next_identity = 20_000

    def _identity(self):
        while self.next_identity in {self.app_uid, self.app_gid}:
            self.next_identity += 1
        identity = self.next_identity
        self.next_identity += 1
        if identity >= 60_000:
            raise RuntimeError("Restart the media runner to refresh job identities.")
        return identity

    def _create_job(self, request, identity, *, job=None):
        job = job if job is not None else self.scratch / uuid.uuid4().hex
        job.mkdir(mode=0o750)
        os.chown(job, 0, identity)
        job.chmod(0o750)
        inputs = job / "input"
        inputs.mkdir(mode=0o750)
        os.chown(inputs, 0, identity)
        inputs.chmod(0o750)
        output = job / "output"
        output.mkdir(mode=0o700)
        os.chown(output, identity, identity)
        control = {
            "operation": request["operation"],
            "inputs": {
                entry["name"]: str(inputs / entry["name"])
                for entry in request["inputs"]
            },
            "parameters": request["parameters"],
            "output_dir": str(output),
            "result_path": str(output / "result.json"),
            "timeout_seconds": request["timeout_seconds"],
        }
        control_path = job / "request.json"
        control_path.write_bytes(encode_json(control))
        os.chown(control_path, 0, identity)
        control_path.chmod(0o440)
        return job

    def _receive_inputs(self, connection, request, job, identity):
        deadline = time.monotonic() + 300
        for entry in request["inputs"]:
            path = job / "input" / entry["name"]
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as target:
                remaining = entry["size"]
                while remaining:
                    data = receive_exact(
                        connection, min(CHUNK_BYTES, remaining), deadline=deadline
                    )
                    target.write(data)
                    remaining -= len(data)
            os.chown(path, 0, identity)
            path.chmod(0o440)

    def _execute_child(self, connection, request, job, identity):
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pandect.media_runner.confinement",
                str(job / "request.json"),
                str(identity),
                str(identity),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env={
                "PATH": "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin",
                "PYTHONPATH": "/app",
                "PYTHONDONTWRITEBYTECODE": "1",
                "LANG": "C.UTF-8",
            },
        )
        deadline = time.monotonic() + request["timeout_seconds"]
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise RunnerJobError(
                        "Media processing timed out.", code="tool_timed_out"
                    )
                if select.select([connection], [], [], 0.1)[0]:
                    # No further client bytes are valid after the input streams.
                    raise RunnerJobError(
                        "The media client disconnected.", code="cancelled"
                    )
            if process.returncode:
                raise RunnerJobError(
                    "Media processing exited unsuccessfully.",
                    code="resource_limit" if process.returncode == -9 else "job_failed",
                )
        finally:
            kill_and_reap()
            # waitpid above may already have reaped the direct child.
            process.wait()

    def _job(self, connection, request):
        identity = self._identity()
        job = self.scratch / uuid.uuid4().hex
        opened = []
        response_started = False
        try:
            self._create_job(request, identity, job=job)
            send_json(connection, {"version": VERSION, "status": "ready"})
            self._receive_inputs(connection, request, job, identity)
            self._execute_child(connection, request, job, identity)
            result, manifest, opened = collect_outputs(job, maximum_bytes=1024**3)
            response_started = True
            send_json(
                connection,
                {
                    "version": VERSION,
                    "status": "ok",
                    "result": result,
                    "outputs": manifest,
                },
            )
            deadline = time.monotonic() + 300
            for entry, fd in zip(manifest, opened, strict=True):
                remaining = entry["size"]
                while remaining:
                    data = os.read(fd, min(CHUNK_BYTES, remaining))
                    if not data:
                        raise RunnerProtocolError(
                            "A media artifact ended unexpectedly."
                        )
                    time_left = deadline - time.monotonic()
                    if time_left <= 0:
                        raise TimeoutError(
                            "The media-runner transfer deadline expired."
                        )
                    connection.settimeout(time_left)
                    connection.sendall(data)
                    remaining -= len(data)
        except (OSError, RunnerError) as exc:
            if response_started:
                raise _ResponseStarted from exc
            raise
        finally:
            for fd in opened:
                os.close(fd)
            scratch_fd = os.open(
                self.scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                if job.exists():
                    remove_tree_at(scratch_fd, job.name)
            except Exception:
                # Fail closed: sensitive scratch must not survive into another job.
                os._exit(70)
            finally:
                os.close(scratch_fd)

    def handle(self, connection):
        with connection:
            try:
                if peer_uid(connection) != self.app_uid:
                    return
                connection.settimeout(5)
                request = receive_json(connection, deadline=time.monotonic() + 5)
                if (
                    request == {"version": VERSION, "operation": "status"}
                    and type(request["version"]) is int
                ):
                    send_json(
                        connection,
                        {
                            "version": VERSION,
                            "status": "ok",
                            "busy": self.job_lock.locked(),
                        },
                    )
                    return
                validate_request(request)
                if not self.job_lock.acquire(blocking=False):
                    send_json(connection, {"version": VERSION, "status": "busy"})
                    return
                try:
                    self._job(connection, request)
                finally:
                    self.job_lock.release()
            except _ResponseStarted:
                return
            except (RunnerError, OSError, ValueError, TypeError, KeyError) as exc:
                code = exc.code if isinstance(exc, RunnerJobError) else "job_failed"
                try:
                    send_json(
                        connection,
                        {"version": VERSION, "status": "failed", "error_code": code},
                    )
                except OSError:
                    pass
            finally:
                self.connection_slots.release()

    def serve(self):
        require_supervisor_boundary()
        # PID 1 otherwise ignores the default termination action. Exiting the
        # namespace terminates all children and discards this container's tmpfs;
        # clients fail closed and never publish an incomplete stream.
        signal.signal(signal.SIGTERM, lambda _signal, _frame: sys.exit(0))
        signal.signal(signal.SIGINT, lambda _signal, _frame: sys.exit(0))
        restrict_shared_state()
        if self.app_uid <= 0 or self.app_gid <= 0:
            raise RuntimeError(
                "The media runner needs a non-root application identity."
            )
        os.umask(0o077)
        self.scratch.mkdir(mode=0o711, exist_ok=True)
        os.chmod(self.scratch, 0o711)
        scratch_fd = os.open(self.scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in os.listdir(scratch_fd):
                remove_tree_at(scratch_fd, name)
        finally:
            os.close(scratch_fd)
        parent = self.socket_path.parent
        parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        os.chown(parent, 0, self.app_gid)
        os.chmod(parent, 0o750)
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
                raise RuntimeError("The media runner socket location is unsafe.")
            self.socket_path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.socket_path))
            os.chown(self.socket_path, 0, self.app_gid)
            os.chmod(self.socket_path, 0o660)
            listener.listen(8)
            while True:
                try:
                    connection, _address = listener.accept()
                except OSError as exc:
                    if exc.errno == errno.EINTR:
                        continue
                    raise
                if not self.connection_slots.acquire(blocking=False):
                    connection.close()
                    continue
                threading.Thread(
                    target=self.handle, args=(connection,), daemon=True
                ).start()


def main():
    Supervisor(
        socket_path=os.environ.get(
            "PANDECT_RUNNER_SOCKET", "/run/pandect-media/runner.sock"
        ),
        scratch=Path("/scratch"),
        app_uid=int(os.environ["PANDECT_RUNNER_APP_UID"]),
        app_gid=int(os.environ["PANDECT_RUNNER_APP_GID"]),
    ).serve()
