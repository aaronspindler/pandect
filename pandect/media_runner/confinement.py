"""Fixed Linux bootstrap, executed before any decoder or inference imports.

This is not a general command launcher. The only executable target is Pandect's
own child dispatcher. The supervisor supplies all paths and the fresh identity.
"""

import ctypes
import errno
import math
import os
import resource
import socket
import sys
from pathlib import Path

from .protocol import MAX_OUTPUT_BYTES, decode_json

_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38
_CAP_VERSION = 0x20080522
_SECCOMP_ALLOW = 0x7FFF0000
_SECCOMP_ERRNO = 0x00050000 | errno.EPERM
SUPERVISOR_CAPABILITIES = (1 << 0) | (1 << 5) | (1 << 6) | (1 << 7)


def require_supervisor_boundary():
    """Fail closed before any operation capable of signalling namespace peers."""
    if sys.platform != "linux" or os.geteuid() != 0 or os.getpid() != 1:
        raise RuntimeError(
            "The media runner must be root PID 1 in its private container."
        )
    # Docker Desktop may expose dormant kernel tunnel devices even in a ``none``
    # namespace. They cannot carry traffic while down, and this process has no
    # NET_ADMIN capability with which to activate them. Do not mistake these or
    # the bonding_masters sysfs control file for a live network connection.
    for _index, name in socket.if_nameindex():
        flags = int(Path(f"/sys/class/net/{name}/flags").read_text().strip(), 16)
        if name != "lo" and flags & 1:  # IFF_UP
            raise RuntimeError(
                "The media runner must have no active network interface."
            )
    fields = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    if int(fields["CapEff"].strip(), 16) != SUPERVISOR_CAPABILITIES:
        raise RuntimeError("The media runner has unexpected capabilities.")
    if fields.get("NoNewPrivs", "").strip() != "1":
        raise RuntimeError("The media runner requires no-new-privileges.")
    if fields.get("Seccomp", "").strip() != "2":
        raise RuntimeError("The media runner requires the container seccomp filter.")


def restrict_shared_state():
    """No child-writable state outside the lifetime of its private job."""
    for directory in ("/dev/shm", "/dev/mqueue", "/tmp"):
        info = os.stat(directory, follow_symlinks=False)
        if info.st_uid != 0 or not Path(directory).is_dir():
            raise RuntimeError("The media runner has an unsafe shared directory.")
        os.chmod(directory, 0o700, follow_symlinks=False)


def deny_sysv_ipc():
    """SysV objects otherwise outlive the originating process in the IPC namespace."""
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(_SECCOMP_ALLOW)
    if not context:
        raise RuntimeError("Cannot initialize media-runner seccomp confinement.")
    try:
        for name in (
            "shmget",
            "shmat",
            "shmdt",
            "shmctl",
            "semget",
            "semctl",
            "semop",
            "semtimedop",
            "msgget",
            "msgsnd",
            "msgrcv",
            "msgctl",
            "ipc",
        ):
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:
                # Some obsolete spellings do not exist on every supported architecture.
                if name in {"semop", "semtimedop", "ipc"}:
                    continue
                raise RuntimeError("Cannot resolve a required IPC confinement rule.")
            if library.seccomp_rule_add(context, _SECCOMP_ERRNO, number, 0) != 0:
                raise RuntimeError("Cannot install media-runner IPC confinement.")
        if library.seccomp_load(context) != 0:
            raise RuntimeError("Cannot activate media-runner IPC confinement.")
    finally:
        library.seccomp_release(context)


def drop_identity(uid, gid):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise RuntimeError("Cannot prevent privilege acquisition.")
    os.setgroups([])
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)

    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class Capabilities(ctypes.Structure):
        _fields_ = [
            ("effective", ctypes.c_uint32),
            ("permitted", ctypes.c_uint32),
            ("inheritable", ctypes.c_uint32),
        ]

    header = Header(_CAP_VERSION, 0)
    data = (Capabilities * 2)()
    if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        raise RuntimeError("Cannot clear child capabilities.")
    if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise RuntimeError("Cannot disable child process inspection.")
    if os.geteuid() != uid or os.getgroups():
        raise RuntimeError("The media-runner child identity is not confined.")


def main():
    # Only the root supervisor can enter this bootstrap. No developer/test bypass.
    if sys.platform != "linux" or os.geteuid() != 0 or len(sys.argv) != 4:
        raise RuntimeError("The media child requires the isolated supervisor.")
    control_path = Path(sys.argv[1])
    uid, gid = int(sys.argv[2]), int(sys.argv[3])
    if not (20_000 <= uid < 60_000 and 20_000 <= gid < 60_000):
        raise RuntimeError("Invalid media-runner child identity.")
    control = decode_json(control_path.read_bytes())
    working_dir = Path(control["output_dir"])
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    timeout = control["timeout_seconds"]
    cpu_limit = math.ceil(timeout * 2) + 10
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
    deny_sysv_ipc()
    drop_identity(uid, gid)
    # The supervisor intentionally lacks DAC_OVERRIDE: it cannot traverse the
    # child-owned 0700 output directory until it has assumed that identity.
    os.chdir(working_dir)
    environment = {
        "PATH": "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": str(working_dir),
        "TMPDIR": str(working_dir),
        "PYTHONPATH": "/app",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OMP_NUM_THREADS": "1",
        "OMP_THREAD_LIMIT": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "PANDECT_MODELS_DIR": "/models",
    }
    os.execve(
        sys.executable,
        [sys.executable, "-m", "pandect.media_runner.child", str(control_path)],
        environment,
    )


if __name__ == "__main__":
    main()
