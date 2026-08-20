"""Bounded, argv-only execution for local forensic tools."""

import os
import selectors
import signal
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from os import PathLike
from time import monotonic

from pandect.processing.extractors.errors import (
    ToolFailedError,
    ToolOutputLimitError,
    ToolTimedOutError,
    ToolUnavailableError,
)

DEFAULT_OUTPUT_LIMIT_BYTES = 16 * 1024 * 1024
_READ_SIZE = 64 * 1024


@dataclass(frozen=True)
class CommandResult:
    """Captured output from a successful bounded command."""

    stdout: bytes
    stderr: bytes
    returncode: int

    def stdout_text(self, encoding: str = "utf-8") -> str:
        return self.stdout.decode(encoding, errors="strict")


def _validated_argv(argv: Sequence[str | PathLike[str]]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not argv:
        raise ValueError("argv must be a non-empty sequence")
    result = tuple(os.fspath(part) for part in argv)
    if any(not part or "\x00" in part for part in result):
        raise ValueError("argv entries must be non-empty and contain no NUL bytes")
    return result


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    """Best-effort termination of the command and any child process group."""
    try:
        if os.name == "posix":
            # Do not call poll() before killpg(). Polling reaps an already-
            # exited group leader, which both makes us return too early while
            # a descendant still owns the capture pipes and permits its PID to
            # be reused as an unrelated process-group ID. Until Popen has
            # reaped the leader (returncode remains None), its PID cannot be
            # reused, while start_new_session guarantees that PID is the group
            # ID assigned at launch. It is therefore safe to kill that original
            # group even when the leader itself has already become a zombie.
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - Pandect's supported container is POSIX.
            if process.returncode is None:
                process.kill()
    except (OSError, ProcessLookupError):
        # On POSIX, do not fall back to signalling process.pid after the group
        # lookup failed: the leader may already have exited, and signalling an
        # independently reused PID would be unsafe. The subsequent wait only
        # reaps the Popen-owned leader.
        if os.name != "posix":  # pragma: no cover - supported runtime is POSIX.
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=1)
    except (subprocess.TimeoutExpired, OSError):
        pass


def run_command(
    argv: Sequence[str | PathLike[str]],
    *,
    timeout_seconds: float,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    allowed_returncodes: frozenset[int] = frozenset({0}),
    cwd: str | PathLike[str] | None = None,
    progress_callback: Callable[[], None] | None = None,
    progress_interval_seconds: float = 60.0,
) -> CommandResult:
    """Execute a local tool without a shell and capture bounded output.

    Output from stdout and stderr shares one limit. Exceptions deliberately omit
    arguments, paths, and captured bytes so callers can safely persist their code
    and public message.
    """
    command = _validated_argv(argv)
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if output_limit_bytes <= 0:
        raise ValueError("output_limit_bytes must be positive")
    if progress_interval_seconds <= 0:
        raise ValueError("progress_interval_seconds must be positive")

    try:
        process = subprocess.Popen(  # noqa: S603 - argv-only execution is intentional.
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            shell=False,
            start_new_session=os.name == "posix",
        )
    except (FileNotFoundError, PermissionError, OSError) as error:
        raise ToolUnavailableError from error

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    total_bytes = 0
    deadline = monotonic() + timeout_seconds
    next_progress = monotonic() + progress_interval_seconds

    try:
        while selector.get_map():
            current = monotonic()
            remaining = deadline - current
            if remaining <= 0:
                _kill_process(process)
                raise ToolTimedOutError

            if progress_callback is not None and current >= next_progress:
                progress_callback()
                next_progress = monotonic() + progress_interval_seconds

            poll_timeout = min(remaining, 0.1)
            if progress_callback is not None:
                poll_timeout = min(
                    poll_timeout,
                    max(0.0, next_progress - monotonic()),
                )
            events = selector.select(timeout=poll_timeout)
            if not events:
                continue

            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), _READ_SIZE)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                if len(chunk) > output_limit_bytes - total_bytes:
                    _kill_process(process)
                    raise ToolOutputLimitError
                captured[key.data].extend(chunk)
                total_bytes += len(chunk)

        returncode = process.wait(timeout=max(0.01, deadline - monotonic()))
    except subprocess.TimeoutExpired as error:
        _kill_process(process)
        raise ToolTimedOutError from error
    except BaseException:
        _kill_process(process)
        raise
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if not stream.closed:
                stream.close()

    if returncode not in allowed_returncodes:
        raise ToolFailedError(returncode=returncode)

    return CommandResult(
        stdout=bytes(captured["stdout"]),
        stderr=bytes(captured["stderr"]),
        returncode=returncode,
    )
