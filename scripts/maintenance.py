"""Offline, credential-free maintenance; executed by the stdlib Docker image."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pandect.config.data_paths import (  # noqa: E402
    initialize_data_directory,
    validate_data_contents,
)

DATABASES = ("application.sqlite3", "huey.sqlite3")
ARCHIVE = "data.tar.gz"
MANIFEST = "manifest.json"
CHECKSUMS = "SHA256SUMS"
MAX_RESTORE_MEMBERS = 100_000
RESTORE_INODE_RESERVE = 16
PUBLIC_SETTINGS = (
    "PANDECT_ALLOWED_HOSTS",
    "PANDECT_DEBUG",
    "PANDECT_TIME_ZONE",
    "PANDECT_CLOUDFLARE_AI_ENABLED",
    "PANDECT_CLOUDFLARE_GATEWAY_ID",
    "PANDECT_CLOUDFLARE_TEXT_MODEL",
    "PANDECT_CLOUDFLARE_VISION_MODEL",
    "PANDECT_CLOUDFLARE_TRANSCRIPTION_MODEL",
    "PANDECT_CLOUDFLARE_CLIENT_TIMEOUT_SECONDS",
)


def read_configuration(configuration: dict) -> dict:
    web = configuration["services"]["web"]
    worker = configuration["services"].get("worker", web)
    environment = web.get("environment", {})
    worker_environment = worker.get("environment", {})
    source = next(
        volume["source"]
        for volume in web["volumes"]
        if volume["target"] == "/data" and volume["type"] == "bind"
    )
    return {
        "data_path": source,
        "project": configuration["name"],
        "image": web["image"],
        "uid": str(web["user"]).split(":")[0],
        "gid": str(web["user"]).split(":")[1],
        "configuration": {
            name: environment.get(name, worker_environment.get(name))
            for name in PUBLIC_SETTINGS
            if name in environment or name in worker_environment
        },
        "secret_override": bool(environment.get("PANDECT_SECRET_KEY")),
        "remote_credential_required": bool(
            worker_environment.get("PANDECT_CLOUDFLARE_API_TOKEN")
        ),
    }


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def check_databases(data: Path) -> list[tuple[str, str]]:
    migrations = []
    for name in DATABASES:
        database = data / name
        if not database.is_file() or database.is_symlink():
            raise ValueError(
                f"Backup requires a regular {name}; run setup and the stack first."
            )
        with closing(
            sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        ) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError(f"SQLite integrity check failed for {name}.")
            if name == "application.sqlite3":
                migrations = connection.execute(
                    "SELECT app, name FROM django_migrations ORDER BY app, name"
                ).fetchall()
    return migrations


def check_archived_databases(archive_path: Path) -> list[tuple[str, str]]:
    """Validate completed archive DB/WAL bytes without opening source databases."""
    database_members = {
        f"{name}{suffix}"
        for name in DATABASES
        for suffix in ("", "-wal", "-shm", "-journal")
    }
    with tempfile.TemporaryDirectory(
        prefix=".verify-databases-", dir=archive_path.parent
    ) as temporary:
        copied = Path(temporary)
        seen = set()
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive:
                safe_archive_member(member)
                if member.name not in database_members:
                    continue
                if member.name in seen or not member.isfile():
                    raise ValueError("Invalid archived database member.")
                seen.add(member.name)
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("Archived database member has no contents.")
                with stream, (copied / member.name).open("xb") as output:
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
        return check_databases(copied)


def check_private_database_copies(data: Path) -> list[tuple[str, str]]:
    """Do not let SQLite create/delete sidecars in the tree being restored."""
    with tempfile.TemporaryDirectory(
        prefix=".verify-databases-", dir=data.parent
    ) as temporary:
        copied = Path(temporary)
        for name in DATABASES:
            for suffix in ("", "-wal", "-shm", "-journal"):
                source = data / f"{name}{suffix}"
                if source.exists() or source.is_symlink():
                    if source.is_symlink() or not source.is_file():
                        raise ValueError("Database sidecars must be regular files.")
                    shutil.copyfile(source, copied / source.name)
        return check_databases(copied)


def safe_archive_member(member: tarfile.TarInfo) -> tarfile.TarInfo:
    parts = PurePosixPath(member.name).parts
    if (
        not parts
        or member.name.startswith("/")
        or ".." in parts
        or "\\" in member.name
        or member.size < 0
        or not (member.isfile() or member.isdir())
        or member.linkname
    ):
        raise ValueError("Archive contains an unsafe path, link, or special file.")
    # Preserve public model visibility, but never privileged permission bits.
    member.mode &= 0o777
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member


def verify_backup(backup: Path) -> dict:
    for name in (ARCHIVE, MANIFEST, CHECKSUMS):
        path = backup / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Backup files must be regular files, not links.")
    if (backup / CHECKSUMS).stat().st_size > 1024:
        raise ValueError("Invalid backup checksum file.")
    expected = {}
    for line in (backup / CHECKSUMS).read_text(encoding="ascii").splitlines():
        digest, separator, name = line.partition("  ")
        if (
            separator != "  "
            or name not in (ARCHIVE, MANIFEST)
            or name in expected
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("Invalid backup checksum file.")
        expected[name] = digest
    if set(expected) != {ARCHIVE, MANIFEST}:
        raise ValueError("Incomplete backup checksum file.")
    for name, digest in expected.items():
        if sha256(backup / name) != digest:
            raise ValueError(f"Backup checksum mismatch: {name}.")
    if (backup / MANIFEST).stat().st_size > 1024 * 1024:
        raise ValueError("Backup manifest is too large.")
    manifest = json.loads((backup / MANIFEST).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
        raise ValueError("Unsupported backup format.")
    for key in (
        "source_revision",
        "tooling_revision",
        "runtime_image_id",
        "created_at",
    ):
        if (
            not isinstance(manifest.get(key), str)
            or not manifest[key]
            or len(manifest[key]) > 200
            or not manifest[key].isprintable()
        ):
            raise ValueError("Invalid backup manifest metadata.")
    for key in (
        "source_dirty",
        "tooling_dirty",
        "secret_override",
        "remote_credential_required",
    ):
        if not isinstance(manifest.get(key), bool):
            raise ValueError("Invalid backup manifest flags.")
    if not isinstance(manifest.get("configuration"), dict) or not isinstance(
        manifest.get("migrations"), list
    ):
        raise ValueError("Invalid backup manifest state.")
    return manifest


def create_backup(data: Path, destination: Path, metadata: dict) -> Path:
    validate_data_contents(data, secret_override=metadata["secret_override"])
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError("Backup destination must be an existing dedicated directory.")
    if stat.S_IMODE(destination.stat().st_mode) & 0o077:
        raise ValueError("Backup destination must be private (mode 0700).")
    staging = Path(tempfile.mkdtemp(prefix=".pandect-backup-", dir=destination))
    try:
        with tarfile.open(staging / ARCHIVE, "w:gz", dereference=False) as archive:
            for child in sorted(data.iterdir()):
                archive.add(child, arcname=child.name, filter=safe_archive_member)
        migrations = check_archived_databases(staging / ARCHIVE)
        manifest = {
            "format_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "source_revision": metadata["source_revision"],
            "source_dirty": metadata["source_dirty"],
            "tooling_revision": metadata["tooling_revision"],
            "tooling_dirty": metadata["source_dirty"],
            "runtime_image_id": metadata["runtime_image_id"],
            "runtime_image_role": "configured_tooling_image",
            "evidence_writer_image_id": None,
            "migrations": migrations,
            "configuration": metadata["configuration"],
            "secret_override": metadata["secret_override"],
            "remote_credential_required": metadata["remote_credential_required"],
        }
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
        (staging / CHECKSUMS).write_text(
            "".join(
                f"{sha256(staging / name)}  {name}\n" for name in (ARCHIVE, MANIFEST)
            )
        )
        for child in staging.iterdir():
            child.chmod(0o600)
            with child.open("rb") as stream:
                os.fsync(stream.fileno())
        verify_backup(staging)
        with tarfile.open(staging / ARCHIVE, "r:gz") as archive:
            for member in archive:
                safe_archive_member(member)
        # The random suffix keeps parallel backups unique; never overwrite.
        final = destination / staging.name.removeprefix(".")
        if final.exists():
            raise ValueError("Backup destination already exists.")
        sync_directory(staging)
        staging.rename(final)
        sync_directory(destination)
        return final
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def restore_requirements(archive_path: Path) -> tuple[int, int]:
    """Bound archive metadata and count implicit parents before creating files."""
    expanded_bytes = 0
    seen = set()
    paths = set()
    with tarfile.open(archive_path, "r:gz") as archive:
        for count, member in enumerate(archive, start=1):
            if count > MAX_RESTORE_MEMBERS:
                raise ValueError("Backup exceeds the restore archive member limit.")
            safe_archive_member(member)
            path = PurePosixPath(member.name)
            if path in seen:
                raise ValueError("Archive contains duplicate paths.")
            seen.add(path)
            while path != PurePosixPath(".") and path not in paths:
                paths.add(path)
                if len(paths) > MAX_RESTORE_MEMBERS:
                    raise ValueError(
                        "Backup exceeds the restore filesystem entry limit."
                    )
                path = path.parent
            if member.isfile():
                expanded_bytes += member.size
    return expanded_bytes, len(paths)


def restore_backup(backup: Path, destination: Path) -> dict:
    manifest = verify_backup(backup)
    if (
        destination.is_symlink()
        or not destination.is_dir()
        or any(destination.iterdir())
    ):
        raise ValueError("Restore destination must be an empty dedicated directory.")
    expanded_bytes, entries = restore_requirements(backup / ARCHIVE)
    if expanded_bytes + 8 * 1024 * 1024 > shutil.disk_usage(destination).free:
        raise ValueError("Insufficient free space for the expanded backup.")
    filesystem = os.statvfs(destination)
    # Some filesystems report no fixed inode pool. Where available, leave room
    # for staging, private database verification copies, and initialization.
    if filesystem.f_files > 0 and (
        entries + RESTORE_INODE_RESERVE > filesystem.f_favail
    ):
        raise ValueError("Insufficient free inodes for the expanded backup.")
    staging = Path(tempfile.mkdtemp(prefix=".pandect-restore-", dir=destination))
    installed = []
    directory_modes = []
    try:
        seen = set()
        with tarfile.open(backup / ARCHIVE, "r:gz") as archive:
            for member in archive:
                safe_archive_member(member)
                name = str(PurePosixPath(member.name))
                if name in seen:
                    raise ValueError("Archive contains duplicate paths.")
                seen.add(name)
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if member.isdir():
                    target.mkdir(mode=0o700, exist_ok=True)
                    directory_modes.append((target, member.mode & 0o777))
                else:
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError("Archive member has no file contents.")
                    with stream, target.open("xb") as output:
                        shutil.copyfileobj(stream, output, length=1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
                    target.chmod(member.mode & 0o777)
        check_private_database_copies(staging)
        validate_data_contents(staging, secret_override=manifest["secret_override"])
        initialize_data_directory(
            staging,
            secret_override=manifest["secret_override"],
        )
        for directory, mode in reversed(directory_modes):
            directory.chmod(mode)
            sync_directory(directory)
        # The mounted destination cannot itself be renamed. Install only after
        # verification, rolling back our files if a rename fails.
        for child in staging.iterdir():
            target = destination / child.name
            if target.exists() or target.is_symlink():
                raise ValueError("Restore destination changed during verification.")
            child.rename(target)
            installed.append(target)
        staging.rmdir()
        sync_directory(destination)
        return manifest
    except BaseException:
        for target in installed:
            target.rename(staging / target.name)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def publish_restore(parent: Path, source_name: str, target_name: str) -> None:
    """Publish a verified sibling directory without merging or replacing data."""
    for name in (source_name, target_name):
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in {".", ".."}
        ):
            raise ValueError("Restore publication requires exact sibling basenames.")
    if not source_name.startswith(".pandect-restore.") or source_name == target_name:
        raise ValueError("Unrecognized private restore staging directory.")
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        source = os.stat(source_name, dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(source.st_mode) or source.st_uid != os.geteuid():
            raise ValueError("Restore staging is not an operator-owned directory.")
        if stat.S_IMODE(source.st_mode) & 0o077:
            raise ValueError("Restore staging must be private.")
        try:
            target = os.stat(target_name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(target.st_mode) or target.st_uid != os.geteuid():
                raise ValueError(
                    "Restore destination must be an operator-owned directory."
                )
            target_fd = os.open(
                target_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            try:
                if os.listdir(target_fd):
                    raise ValueError("Restore destination must still be empty.")
            finally:
                os.close(target_fd)
        current = os.stat(source_name, dir_fd=descriptor, follow_symlinks=False)
        if (source.st_dev, source.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("Restore staging changed before publication.")
        os.rename(
            source_name, target_name, src_dir_fd=descriptor, dst_dir_fd=descriptor
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "config",
            "get",
            "validate",
            "initialize",
            "backup",
            "restore",
            "publish",
        ),
    )
    parser.add_argument("--key")
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--destination", type=Path, default=Path("/backup"))
    parser.add_argument("--revision", default="unknown")
    parser.add_argument("--tooling-revision", default="unknown")
    parser.add_argument("--dirty", action="store_true")
    parser.add_argument("--image-id", default="unknown")
    parser.add_argument("--secret-override", action="store_true")
    parser.add_argument("--source-name")
    parser.add_argument("--target-name")
    args = parser.parse_args()
    try:
        if args.command == "config":
            print(json.dumps(read_configuration(json.load(sys.stdin))))
        elif args.command == "get":
            result = json.load(sys.stdin)[args.key]
            print(str(result).lower() if isinstance(result, bool) else result)
        elif args.command == "validate":
            validate_data_contents(args.data, secret_override=args.secret_override)
        elif args.command == "initialize":
            initialize_data_directory(
                args.data,
                secret_override=args.secret_override,
            )
            for directory in ("models", "work"):
                path = args.data / directory
                if path.is_symlink():
                    raise ValueError("Data subdirectories must not be symbolic links.")
                path.mkdir(mode=0o700, exist_ok=True)
        elif args.command == "backup":
            metadata = json.load(sys.stdin)
            metadata.update(
                source_revision=args.revision,
                tooling_revision=args.tooling_revision,
                source_dirty=args.dirty,
                runtime_image_id=args.image_id,
            )
            result = create_backup(args.data, args.destination, metadata)
            print(result.name)
        elif args.command == "publish":
            publish_restore(Path("/parent"), args.source_name, args.target_name)
        else:
            manifest = restore_backup(args.destination, args.data)
            print(
                "Restored verified data. Use source revision "
                f"{manifest['source_revision']} before starting."
            )
            print(
                "No migrations were applied. Restore environment credentials "
                "separately; they are not in the manifest."
            )
    except (
        OSError,
        ValueError,
        KeyError,
        StopIteration,
        sqlite3.Error,
        tarfile.TarError,
    ) as exc:
        # Do not print configuration or file contents when validation fails.
        print(f"Maintenance failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
