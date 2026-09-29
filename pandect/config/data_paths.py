"""Data-directory guards shared by Django and the stdlib maintenance image."""

import os
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path

MARKER = ".pandect-data"
MARKER_CONTENT = "pandect-data-v1\n"


class UnsafeDataPath(ValueError):
    """The chosen directory is not a dedicated Pandect data location."""


def reject_symlinks(path: Path) -> None:
    """Reject symlink components, including dangling links, before resolving."""
    for component in (path, *path.parents):
        if component.is_symlink():
            raise UnsafeDataPath("Data paths must not contain symbolic links.")


def validate_data_path(path: Path, *, base_dir: Path, home_dir: Path) -> Path:
    """Allow the checkout's data directory or a dedicated external directory."""
    path = path.absolute()
    reject_symlinks(path)
    path = path.resolve()
    base_dir = base_dir.resolve()
    home_dir = home_dir.resolve()
    if path == Path(path.anchor) or path == home_dir or path == base_dir:
        raise UnsafeDataPath("Choose a dedicated Pandect data directory.")
    if path in base_dir.parents or path in home_dir.parents:
        raise UnsafeDataPath("A data directory must not contain the checkout or home.")
    if path.is_relative_to(base_dir) and path != base_dir / "data":
        raise UnsafeDataPath("Only ./data is supported inside the checkout.")
    if path.exists() and not path.is_dir():
        raise UnsafeDataPath("The data path must be a directory, not a file.")
    return path


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _legacy_pandect_schema(database: Path) -> bool:
    """Inspect a private WAL-aware copy; read-only SQLite can still need SHM."""
    with tempfile.TemporaryDirectory(prefix="pandect-schema-") as temporary:
        copied = Path(temporary) / database.name
        for suffix in ("", "-wal", "-shm", "-journal"):
            source = database.with_name(database.name + suffix)
            if source.exists() or source.is_symlink():
                if not _regular_file(source):
                    raise UnsafeDataPath("Database sidecars must be regular files.")
                shutil.copyfile(source, copied.with_name(copied.name + suffix))
        with closing(
            sqlite3.connect(f"{copied.as_uri()}?mode=ro", uri=True)
        ) as connection:
            return bool(
                connection.execute(
                    "SELECT 1 FROM django_migrations "
                    "WHERE app IN ('catalog', 'processing', 'identities') LIMIT 1"
                ).fetchone()
            )


def validate_data_contents(path: Path, *, secret_override: bool = False) -> None:
    """Recognize a new, marked, or legacy Pandect directory without changing it."""
    reject_symlinks(path)
    if not path.exists() or not any(path.iterdir()):
        return
    for name in (
        MARKER,
        "models",
        "work",
        "secret.key",
        "application.sqlite3",
        "huey.sqlite3",
    ):
        if (path / name).is_symlink():
            raise UnsafeDataPath("Reserved data paths must not be symbolic links.")
    marker = path / MARKER
    if _regular_file(marker):
        if marker.read_text(encoding="utf-8") == MARKER_CONTENT:
            return
        raise UnsafeDataPath("The data-directory marker is not recognized.")
    database = path / "application.sqlite3"
    secret = path / "secret.key"
    has_secret = secret_override or (_regular_file(secret) and secret.stat().st_size)
    if _regular_file(database) and has_secret:
        try:
            if _legacy_pandect_schema(database):
                return
        except sqlite3.Error:
            pass
    raise UnsafeDataPath(
        "Refusing an unrelated nonempty directory; use an empty directory or "
        "existing Pandect data with its database and generated secret."
    )


def initialize_data_directory(path: Path, *, secret_override: bool = False) -> None:
    """Initialize only validated data; do not change existing file permissions."""
    validate_data_contents(path, secret_override=secret_override)
    if path.exists():
        metadata = path.stat()
        private = not stat.S_IMODE(metadata.st_mode) & 0o077
        accessible = os.access(path, os.R_OK | os.W_OK | os.X_OK)
        # Docker Desktop can present a host-owned private bind-mount root as
        # uid 0 while honoring the operator's effective read/write access.
        # Host entrypoints verify the real owner before mounting. Do not apply
        # this exception to ordinary directories or inaccessible Linux mounts.
        mapped_mount = metadata.st_uid == 0 and path.is_mount() and accessible
        owned = metadata.st_uid == os.geteuid() or mapped_mount
        if not owned or not private or not accessible:
            raise UnsafeDataPath(
                "The dedicated data directory must be owned by the configured "
                "operator and private (mode 0700). Correct that directory's "
                "ownership/permissions explicitly, then retry."
            )
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = path / MARKER
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if not _regular_file(marker) or marker.read_text() != MARKER_CONTENT:
            raise UnsafeDataPath(
                "The data-directory marker is not recognized."
            ) from None
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as marker_file:
            marker_file.write(MARKER_CONTENT)
            marker_file.flush()
            os.fsync(marker_file.fileno())
