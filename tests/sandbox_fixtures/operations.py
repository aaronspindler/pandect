"""Test-only hostile parser substitutes, mounted over the production dispatcher.

The production image has no test operation or environment-variable bypass. This
module is loaded only by scripts/sandbox-smoke.sh in a disposable constrained
container. All activity is restricted to synthetic scratch and bounded tests.
"""

import ctypes
import errno
import os
import resource
import signal
import socket
import time
import traceback
from pathlib import Path


def _denied(call):
    try:
        call()
    except OSError:
        return True
    return False


def _boundary(request):
    uid = os.getuid()
    assert 20_000 <= uid < 60_000 and os.geteuid() == uid
    assert os.getgroups() == []
    assert os.getgid() == uid
    fields = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    assert int(fields["CapEff"].strip(), 16) == 0
    assert fields["NoNewPrivs"].strip() == "1"
    assert fields["Seccomp"].strip() == "2"
    assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == (256, 256)
    assert resource.getrlimit(resource.RLIMIT_FSIZE) == (1024**3, 1024**3)
    # Some kernels expose their default, DOWN tunnel devices in every new
    # network namespace. None may be UP; the child cannot acquire NET_ADMIN.
    for _index, name in socket.if_nameindex():
        flags = int(Path("/sys/class/net", name, "flags").read_text().strip(), 16)
        assert name == "lo" or flags & 1 == 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.2)
        # TEST-NET-1 is documentation-only. The namespace must have no route;
        # this neither relies on nor contacts an external service.
        assert _denied(lambda: connection.connect(("192.0.2.1", 9)))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        assert _denied(lambda: connection.connect("/run/pandect-media/runner.sock"))
    assert "PANDECT_CLOUDFLARE_API_TOKEN" not in os.environ
    assert "PANDECT_SECRET_KEY" not in os.environ
    assert _denied(lambda: Path("/proc/1/environ").read_bytes())
    for path in (
        "/data/application.sqlite3",
        "/data/huey.sqlite3",
        "/data/secret.key",
        "/data/media/synthetic-original.bin",
        "/tmp/client-only-secret",
    ):
        assert _denied(lambda path=path: Path(path).read_bytes())
    model = Path("/models/public-model.txt")
    assert model.read_text().startswith("Synthetic public model fixture.")
    assert os.statvfs(model).f_flag & os.ST_RDONLY
    assert os.statvfs("/app").f_flag & os.ST_RDONLY
    assert _denied(lambda: model.write_bytes(b"must never replace the public model"))
    assert _denied(lambda: Path("/app/forbidden-write").write_bytes(b"denied"))
    assert _denied(lambda: os.kill(1, signal.SIGKILL))
    assert _denied(lambda: os.listdir("/scratch"))
    for directory in ("/dev/shm", "/dev/mqueue", "/tmp"):
        assert _denied(lambda directory=directory: Path(directory, "state").touch())
    libc = ctypes.CDLL(None, use_errno=True)
    for name, arguments in (
        ("shmget", (0, 4096, 0o1600)),
        ("semget", (0, 1, 0o1600)),
        ("msgget", (0, 0o1600)),
    ):
        ctypes.set_errno(0)
        assert getattr(libc, name)(*arguments) == -1
        assert ctypes.get_errno() == errno.EPERM
    for path in request["inputs"].values():
        assert Path(path).read_bytes() == b"synthetic input only"
        assert _denied(lambda path=path: Path(path).write_bytes(b"forbidden mutation"))
    inherited = set()
    for descriptor in os.listdir("/proc/self/fd"):
        try:
            os.readlink(f"/proc/self/fd/{descriptor}")
        except FileNotFoundError:
            continue
        inherited.add(int(descriptor))
    assert inherited <= {0, 1, 2}
    # A previous malicious job must not retain processes or reusable files.
    pids = {int(name) for name in os.listdir("/proc") if name.isdecimal()}
    assert pids == {1, os.getpid()}, f"Unexpected namespace processes: {pids}"
    for pid in request["parameters"].get("absent_pids", []):
        assert not Path(f"/proc/{pid}").exists()
    previous_job = request["parameters"].get("previous_job")
    if previous_job:
        assert not Path(previous_job).exists()
    return {"uid": uid, "job": str(Path(request["output_dir"]).parent)}


def _orphan():
    read_fd, write_fd = os.pipe()
    first = os.fork()
    if first == 0:
        os.close(read_fd)
        os.setsid()
        second = os.fork()
        if second:
            os._exit(0)
        os.write(write_fd, str(os.getpid()).encode("ascii"))
        os.close(write_fd)
        while True:
            signal.pause()
    os.close(write_fd)
    try:
        grandchild = int(os.read(read_fd, 32))
    finally:
        os.close(read_fd)
    os.waitpid(first, 0)
    return grandchild


def _run_fixture(request):
    """Use one existing wire operation; never extend production operations."""
    assert request["operation"] == "tool_version"
    scenario = request["parameters"]["scenario"]
    output = Path(request["output_dir"])
    if scenario == "minimal":
        return {"result": {}, "outputs": {}}
    if scenario == "good":
        try:
            result = _boundary(request)
        except Exception as error:
            # Only synthetic fixture diagnostics cross this test-only interface.
            result = {
                "boundary_failure": type(error).__name__,
                "line": traceback.extract_tb(error.__traceback__)[-1].lineno,
                "detail": str(error),
            }
        (output / "artifact.bin").write_bytes(b"verified isolated output")
        return {"result": result, "outputs": {"artifact": "artifact.bin"}}
    if scenario == "orphan":
        return {"result": {"orphan_pid": _orphan()}, "outputs": {}}
    if scenario == "pid_limit":
        children = []
        for _ in range(64):
            try:
                pid = os.fork()
            except OSError as error:
                assert error.errno == errno.EAGAIN
                return {"result": {"children": children}, "outputs": {}}
            if pid == 0:
                while True:
                    signal.pause()
            children.append(pid)
        raise AssertionError("The configured PID limit was not enforced")
    if scenario == "timeout" or scenario == "disconnect":
        _orphan()
        time.sleep(30)
        raise AssertionError("The supervisor did not cancel this job")
    if scenario == "crash":
        os._exit(77)
    if scenario == "mode_zero":
        inner = output / "locked"
        inner.mkdir()
        (inner / "synthetic.bin").write_bytes(b"private temporary state")
        inner.chmod(0)
        output.chmod(0)
        return {"result": {}, "outputs": {}}
    if scenario == "symlink":
        (output / "bad.bin").symlink_to("/models/public-model.txt")
    elif scenario == "hardlink":
        (output / "other.bin").write_bytes(b"synthetic bytes")
        os.link(output / "other.bin", output / "bad.bin")
        return {
            "result": {},
            "outputs": {"other": "other.bin", "bad": "bad.bin"},
        }
    elif scenario == "fifo":
        os.mkfifo(output / "bad.bin", 0o600)
    elif scenario == "traversal":
        return {"result": {}, "outputs": {"bad": "../request.json"}}
    elif scenario == "output_count":
        outputs = {}
        for number in range(1025):
            name = f"extra-{number}"
            (output / name).touch()
            outputs[name] = name
        return {"result": {}, "outputs": outputs}
    elif scenario == "output_bytes":
        for name in ("first.bin", "second.bin"):
            with (output / name).open("wb") as stream:
                stream.truncate(600 * 1024**2)
        return {
            "result": {},
            "outputs": {"first": "first.bin", "second": "second.bin"},
        }
    elif scenario == "file_size":
        with (output / "bad.bin").open("wb") as stream:
            stream.truncate(1024**3 + 1)
        (output / "bad.bin").unlink()
        return {"result": {"unexpected": "file-size limit not enforced"}, "outputs": {}}
    elif scenario == "scratch_bytes":
        block = b"x" * 1024**2
        with (output / "full.bin").open("wb", buffering=0) as stream:
            for _ in range(80):
                stream.write(block)
        (output / "full.bin").unlink()
        return {
            "result": {"unexpected": "scratch byte limit not enforced"},
            "outputs": {},
        }
    elif scenario == "scratch_inodes":
        for number in range(4096):
            (output / f"inode-{number}").touch()
        for number in range(4096):
            (output / f"inode-{number}").unlink()
        return {
            "result": {"unexpected": "scratch inode limit not enforced"},
            "outputs": {},
        }
    elif scenario == "memory":
        blocks = []
        for _ in range(96):
            blocks.append(bytearray(8 * 1024**2))
        return {"result": {"unexpected": "memory limit not enforced"}, "outputs": {}}
    elif scenario == "invalid_json":
        Path(request["result_path"]).write_bytes(
            b'{"result":{"invalid":NaN},"outputs":{}}'
        )
        os._exit(0)
    else:
        raise AssertionError("Unknown test scenario")
    return {"result": {}, "outputs": {"bad": "bad.bin"}}


def run_child_request(request):
    envelope = _run_fixture(request)
    envelope["result"]["version"] = "synthetic sandbox fixture"
    return envelope
