"""Return success while the Huey management-command process is running."""

from pathlib import Path


def huey_worker_is_running(proc_root: Path = Path("/proc")) -> bool:
    """Find the worker by exact argv tokens, tolerating short-lived processes."""
    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit():
            continue
        try:
            arguments = set((process_dir / "cmdline").read_bytes().split(b"\0"))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if {b"manage.py", b"run_huey"} <= arguments:
            return True
    return False


if __name__ == "__main__":
    raise SystemExit(0 if huey_worker_is_running() else 1)
