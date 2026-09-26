"""Hermetic tests for the host guards and offline archive format."""

import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

import scripts.maintenance as maintenance_module
from pandect.config.data_paths import (
    MARKER,
    UnsafeDataPath,
    initialize_data_directory,
    validate_data_contents,
    validate_data_path,
)
from scripts.maintenance import (
    ARCHIVE,
    CHECKSUMS,
    MANIFEST,
    create_backup,
    publish_restore,
    read_configuration,
    restore_backup,
    sha256,
)


@pytest.fixture
def sample_data(tmp_path):
    data = tmp_path / "data"
    initialize_data_directory(data)
    (data / "secret.key").write_text("synthetic-secret\n")
    (data / "originals").mkdir(mode=0o700)
    (data / "originals" / "example.txt").write_bytes(b"synthetic evidence\x00\xff")
    (data / "models").mkdir(mode=0o755)
    (data / "models" / "NOTICE").write_text("synthetic model license")
    for name in ("application.sqlite3", "huey.sqlite3"):
        with sqlite3.connect(data / name) as database:
            database.execute("CREATE TABLE django_migrations (app TEXT, name TEXT)")
            database.execute(
                "INSERT INTO django_migrations VALUES ('catalog', '0001_initial')"
            )
    return data


@pytest.fixture
def metadata():
    return {
        "source_revision": "abc123",
        "tooling_revision": "def456",
        "source_dirty": False,
        "runtime_image_id": "sha256:synthetic",
        "configuration": {"PANDECT_TIME_ZONE": "UTC"},
        "secret_override": False,
        "remote_credential_required": True,
    }


def test_backup_restore_roundtrip(sample_data, tmp_path, metadata):
    destination = tmp_path / "backups"
    destination.mkdir(mode=0o700)
    backup = create_backup(sample_data, destination, metadata)
    restored = tmp_path / "restored"
    restored.mkdir(mode=0o700)
    manifest = restore_backup(backup, restored)

    assert manifest["source_revision"] == "abc123"
    assert manifest["tooling_revision"] == "def456"
    assert manifest["tooling_dirty"] is False
    assert manifest["runtime_image_role"] == "configured_tooling_image"
    assert manifest["evidence_writer_image_id"] is None
    assert manifest["migrations"] == [["catalog", "0001_initial"]]
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(file.stat().st_mode) == 0o600 for file in backup.iterdir())
    for original in sample_data.rglob("*"):
        if original.is_file():
            assert sha256(original) == sha256(
                restored / original.relative_to(sample_data)
            )
    assert stat.S_IMODE((restored / "models").stat().st_mode) == 0o755


def test_backup_refuses_world_readable_destination(sample_data, tmp_path, metadata):
    destination = tmp_path / "public-backups"
    destination.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        create_backup(sample_data, destination, metadata)
    assert not list(destination.iterdir())


def test_backup_refuses_links_without_leaving_partial_archive(
    sample_data, tmp_path, metadata
):
    (sample_data / "link").symlink_to(sample_data / "secret.key")
    destination = tmp_path / "backups"
    destination.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="unsafe"):
        create_backup(sample_data, destination, metadata)
    assert not list(destination.iterdir())


def test_restore_refuses_corruption_and_nonempty_target(
    sample_data, tmp_path, metadata
):
    destination = tmp_path / "backups"
    destination.mkdir(mode=0o700)
    backup = create_backup(sample_data, destination, metadata)
    restored = tmp_path / "restored"
    restored.mkdir()
    (restored / "keep").write_text("untouched")
    with pytest.raises(ValueError, match="empty"):
        restore_backup(backup, restored)
    assert (restored / "keep").read_text() == "untouched"
    (backup / ARCHIVE).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum mismatch"):
        restore_backup(backup, restored)


@pytest.mark.parametrize(
    "kind", ["traversal", "symlink", "hardlink", "device", "duplicate", "absolute"]
)
def test_restore_rejects_hostile_archive(sample_data, tmp_path, metadata, kind):
    destination = tmp_path / "backups"
    destination.mkdir(mode=0o700)
    backup = create_backup(sample_data, destination, metadata)
    with tarfile.open(backup / ARCHIVE, "w:gz") as archive:
        member = tarfile.TarInfo("../escaped" if kind == "traversal" else "unsafe")
        if kind == "absolute":
            member.name = "/absolute"
        if kind in {"symlink", "hardlink"}:
            member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            member.linkname = "../../escaped"
        elif kind == "device":
            member.type = tarfile.CHRTYPE
        else:
            member.size = 1
        archive.addfile(member, io.BytesIO(b"x") if member.isfile() else None)
        if kind == "duplicate":
            archive.addfile(member, io.BytesIO(b"x"))
    (backup / CHECKSUMS).write_text(
        "".join(f"{sha256(backup / name)}  {name}\n" for name in (ARCHIVE, MANIFEST))
    )
    restored = tmp_path / "restored"
    restored.mkdir()
    with pytest.raises(ValueError, match="unsafe|duplicate"):
        restore_backup(backup, restored)
    assert not list(restored.iterdir())
    assert not (tmp_path / "escaped").exists()


def test_config_allowlist_does_not_expose_credentials():
    result = read_configuration(
        {
            "name": "pandect-example",
            "services": {
                "web": {
                    "image": "pandect-example:local",
                    "user": "1000:1000",
                    "volumes": [
                        {
                            "type": "bind",
                            "source": "/dedicated/data with spaces",
                            "target": "/data",
                        }
                    ],
                    "environment": {
                        "PANDECT_SECRET_KEY": "never-print",
                        "PANDECT_CLOUDFLARE_API_TOKEN": "never-print",
                        "PANDECT_TIME_ZONE": "UTC",
                    },
                }
            },
        }
    )
    assert "never-print" not in json.dumps(result)
    assert result["secret_override"] is True
    assert result["remote_credential_required"] is True
    assert result["data_path"].endswith("data with spaces")


def test_legacy_data_recognized_without_modifying_existing_files(sample_data):
    (sample_data / MARKER).unlink()
    secret = sample_data / "secret.key"
    before = secret.stat()
    validate_data_contents(sample_data)
    initialize_data_directory(sample_data)
    assert secret.stat().st_mtime_ns == before.st_mtime_ns
    assert secret.stat().st_mode == before.st_mode


def test_legacy_override_needs_explicit_flag(sample_data):
    (sample_data / MARKER).unlink()
    (sample_data / "secret.key").unlink()
    with pytest.raises(UnsafeDataPath):
        validate_data_contents(sample_data)
    validate_data_contents(sample_data, secret_override=True)


@pytest.mark.parametrize("marked", [False, True])
def test_initialization_refuses_nonprivate_existing_data(tmp_path, marked):
    data = tmp_path / "data"
    if marked:
        initialize_data_directory(data)
    else:
        data.mkdir(mode=0o700)
    data.chmod(0o755)
    before = set(data.iterdir())
    with pytest.raises(UnsafeDataPath, match="private"):
        initialize_data_directory(data)
    assert stat.S_IMODE(data.stat().st_mode) == 0o755
    assert set(data.iterdir()) == before


@pytest.mark.parametrize(
    "choice", ["root", "home", "checkout", "ancestor", "nested", "file", "symlink"]
)
def test_unsafe_data_paths_do_not_change_files(tmp_path, choice):
    home = tmp_path / "home"
    home.mkdir()
    checkout = home / "checkout"
    checkout.mkdir()
    file = tmp_path / "file"
    file.write_text("keep")
    link = tmp_path / "link"
    link.symlink_to(checkout / "data")
    choices = {
        "root": Path("/"),
        "home": home,
        "checkout": checkout,
        "ancestor": tmp_path,
        "nested": checkout / "other",
        "file": file,
        "symlink": link,
    }
    with pytest.raises(UnsafeDataPath):
        validate_data_path(choices[choice], base_dir=checkout, home_dir=home)
    assert not (checkout / "data").exists()
    assert file.read_text() == "keep"


def test_host_shell_guard_matches_default_external_and_unsafe_paths(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    checkout = home / "checkout"
    checkout.mkdir()
    common = Path(__file__).resolve().parents[1] / "scripts" / "maintenance-common.sh"
    script = (
        '. "$1"; project_root=$2; pandect_data_dir=$3; '
        'pandect_validate_data_path; printf "%s" "$pandect_data_dir"'
    )
    for target, succeeds in [
        (checkout / "data", True),
        (tmp_path / "external data", True),
        (checkout, False),
        (home, False),
        (Path("/"), False),
        (checkout / "other", False),
    ]:
        result = subprocess.run(
            ["sh", "-eu", "-c", script, "sh", str(common), str(checkout), str(target)],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(home)},
            check=False,
        )
        assert (result.returncode == 0) is succeeds, result.stderr
    assert not (checkout / "data").exists()


@pytest.mark.parametrize("existing", [False, True])
def test_atomic_publication_replaces_only_empty_directory(tmp_path, existing):
    source = tmp_path / ".pandect-restore.test"
    initialize_data_directory(source)
    (source / "original.txt").write_text("synthetic evidence")
    target = tmp_path / "data"
    if existing:
        target.mkdir(mode=0o700)
    publish_restore(tmp_path, source.name, target.name)
    assert not source.exists()
    assert (target / "original.txt").read_text() == "synthetic evidence"


@pytest.mark.parametrize("kind", ["populated", "file", "symlink"])
def test_atomic_publication_refuses_raced_in_destination(tmp_path, monkeypatch, kind):
    source = tmp_path / ".pandect-restore.test"
    initialize_data_directory(source)
    (source / "original.txt").write_text("synthetic evidence")
    target = tmp_path / "data"
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep").write_text("private")
    original_rename = os.rename

    def race_before_rename(*args, **kwargs):
        if kind == "populated":
            target.mkdir()
            (target / "keep").write_text("private")
        elif kind == "file":
            target.write_text("private")
        else:
            target.symlink_to(other, target_is_directory=True)
        return original_rename(*args, **kwargs)

    monkeypatch.setattr(os, "rename", race_before_rename)
    with pytest.raises(OSError):
        publish_restore(tmp_path, source.name, target.name)
    assert (source / "original.txt").read_text() == "synthetic evidence"
    assert (other / "keep").read_text() == "private"
    assert not (target / "original.txt").exists()


def test_restore_rejects_invalid_manifest_before_extracting(
    sample_data, tmp_path, metadata
):
    destination = tmp_path / "backups"
    destination.mkdir(mode=0o700)
    backup = create_backup(sample_data, destination, metadata)
    (backup / MANIFEST).write_text("[]")
    (backup / CHECKSUMS).write_text(
        "".join(f"{sha256(backup / name)}  {name}\n" for name in (ARCHIVE, MANIFEST))
    )
    restored = tmp_path / "restored"
    restored.mkdir()
    with pytest.raises(ValueError, match="Unsupported backup"):
        restore_backup(backup, restored)
    assert not list(restored.iterdir())


def test_restore_rejects_terminal_controls_in_manifest_revision(
    sample_data, tmp_path, metadata
):
    destination = tmp_path / "backups"
    destination.mkdir(mode=0o700)
    backup = create_backup(sample_data, destination, metadata)
    manifest = json.loads((backup / MANIFEST).read_text())
    manifest["source_revision"] = "\x1b[2J"
    (backup / MANIFEST).write_text(json.dumps(manifest))
    (backup / CHECKSUMS).write_text(
        "".join(f"{sha256(backup / name)}  {name}\n" for name in (ARCHIVE, MANIFEST))
    )
    restored = tmp_path / "restored"
    restored.mkdir()
    with pytest.raises(ValueError, match="Invalid backup manifest metadata"):
        restore_backup(backup, restored)
    assert not list(restored.iterdir())


def test_maintenance_signal_exits_and_releases_its_lock(tmp_path):
    common = Path(__file__).resolve().parents[1] / "scripts" / "maintenance-common.sh"
    script = (
        '. "$1"; project_root=$2; pandect_lock; kill -TERM $$; '
        'printf "incorrectly resumed"'
    )
    result = subprocess.run(
        ["sh", "-eu", "-c", script, "sh", str(common), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 143
    assert "resumed" not in result.stdout
    assert not (tmp_path / ".pandect-maintenance.lock").exists()


def test_maintenance_refuses_an_existing_lock_without_removing_it(tmp_path):
    lock = tmp_path / ".pandect-maintenance.lock"
    lock.mkdir()
    (lock / "pid").write_text("123")
    common = Path(__file__).resolve().parents[1] / "scripts" / "maintenance-common.sh"
    result = subprocess.run(
        [
            "sh",
            "-eu",
            "-c",
            '. "$1"; project_root=$2; pandect_lock',
            "sh",
            str(common),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert (lock / "pid").read_text() == "123"


@pytest.mark.parametrize("available", [False, True])
def test_image_identity_does_not_require_a_runtime_build(available):
    common = Path(__file__).resolve().parents[1] / "scripts" / "maintenance-common.sh"
    fake_inspect = (
        'printf "sha256:synthetic\\n"'
        if available
        else 'printf "No such image\\n" >&2; return 1'
    )
    script = (
        '. "$1"; pandect_runtime_image=first-legacy-runtime:local; '
        f"docker() {{ {fake_inspect}; }}; "
        "pandect_runtime_image_identity"
    )
    result = subprocess.run(
        ["sh", "-eu", "-c", script, "sh", str(common)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ("sha256:synthetic" if available else "unknown")
    assert result.stderr == ""


def test_first_legacy_backup_roundtrip_with_unknown_runtime_image(
    sample_data, tmp_path, metadata
):
    destination = tmp_path / "backups"
    destination.mkdir(mode=0o700)
    metadata["runtime_image_id"] = "unknown"
    backup = create_backup(sample_data, destination, metadata)
    restored = tmp_path / "restored"
    restored.mkdir(mode=0o700)
    manifest = restore_backup(backup, restored)
    assert manifest["source_revision"] == "abc123"
    assert manifest["tooling_revision"] == "def456"
    assert manifest["runtime_image_id"] == "unknown"
    assert manifest["runtime_image_role"] == "configured_tooling_image"
    assert manifest["evidence_writer_image_id"] is None
    assert sha256(restored / "application.sqlite3") == sha256(
        sample_data / "application.sqlite3"
    )


def test_backup_refuses_corrupt_archived_database(sample_data, tmp_path, metadata):
    destination = tmp_path / "backups"
    destination.mkdir(mode=0o700)
    (sample_data / "application.sqlite3").write_bytes(b"corrupted SQLite database")
    with pytest.raises(sqlite3.DatabaseError):
        create_backup(sample_data, destination, metadata)
    assert not list(destination.iterdir())


def test_backup_opens_only_copied_databases(
    sample_data, tmp_path, metadata, monkeypatch
):
    destination = tmp_path / "backups"
    destination.mkdir(mode=0o700)
    original_check = maintenance_module.check_databases
    checked = []

    def check_only_private_copy(data):
        assert data != sample_data
        checked.append(data)
        return original_check(data)

    monkeypatch.setattr(maintenance_module, "check_databases", check_only_private_copy)
    backup = create_backup(sample_data, destination, metadata)
    assert (backup / ARCHIVE).is_file()
    assert len(checked) == 1


def test_legacy_wal_backup_without_shm_preserves_pending_committed_data(
    tmp_path, metadata, monkeypatch
):
    data = tmp_path / "legacy"
    data.mkdir(mode=0o700)
    (data / "secret.key").write_text("synthetic-secret")
    application = data / "application.sqlite3"
    writer = sqlite3.connect(application)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE django_migrations (app TEXT, name TEXT)")
        writer.execute(
            "INSERT INTO django_migrations VALUES ('catalog', '0001_in_wal')"
        )
        writer.commit()
        huey = sqlite3.connect(data / "huey.sqlite3")
        huey.execute("CREATE TABLE task (name TEXT)")
        huey.commit()
        huey.close()
        # A stopped WAL database can retain a WAL but have no SHM. A reader
        # must not create SHM beside evidence in a read-only source mount.
        (data / "application.sqlite3-shm").unlink()
        before = {path.name: path.read_bytes() for path in data.iterdir()}
        original_connect = sqlite3.connect

        def only_private_connections(database, *args, **kwargs):
            assert not str(database).startswith(data.as_uri())
            return original_connect(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", only_private_connections)
        validate_data_contents(data)
        destination = tmp_path / "backups"
        destination.mkdir(mode=0o700)
        backup = create_backup(data, destination, metadata)
        manifest = json.loads((backup / MANIFEST).read_text())
        assert manifest["migrations"] == [["catalog", "0001_in_wal"]]
        assert {path.name: path.read_bytes() for path in data.iterdir()} == before
        restored = tmp_path / "restored"
        restored.mkdir(mode=0o700)
        restore_backup(backup, restored)
        # The only new file is the explicit legacy-data marker; SQLite must
        # not create SHM, checkpoint WAL, or change any restored evidence bytes.
        after = {
            path.name: path.read_bytes()
            for path in restored.iterdir()
            if path.name != MARKER
        }
        assert after == before
    finally:
        writer.close()


@pytest.fixture
def lock_wrapper(tmp_path):
    source = Path(__file__).resolve().parents[1] / "scripts"
    destination = tmp_path / "scripts"
    destination.mkdir()
    for name in ("with-maintenance-lock.sh", "maintenance-common.sh"):
        shutil.copy2(source / name, destination / name)
    return destination / "with-maintenance-lock.sh"


def test_writer_wrapper_holds_lock_preserves_arguments_and_releases(lock_wrapper):
    result = subprocess.run(
        [
            "sh",
            str(lock_wrapper),
            "sh",
            "-c",
            'test -f .pandect-maintenance.lock/pid && printf "<%s>" "$1"',
            "sh",
            "one spaced argument",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "<one spaced argument>"
    assert not (lock_wrapper.parents[1] / ".pandect-maintenance.lock").exists()


def test_writer_wrapper_releases_lock_after_command_failure(lock_wrapper):
    result = subprocess.run(
        ["sh", str(lock_wrapper), "sh", "-c", "exit 17"], check=False
    )
    assert result.returncode == 17
    assert not (lock_wrapper.parents[1] / ".pandect-maintenance.lock").exists()


def test_writer_wrapper_refuses_backup_lock_without_running_command(lock_wrapper):
    lock = lock_wrapper.parents[1] / ".pandect-maintenance.lock"
    lock.mkdir()
    (lock / "pid").write_text("existing-backup")
    result = subprocess.run(
        ["sh", str(lock_wrapper), "sh", "-c", "touch incorrectly-ran"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert (lock / "pid").read_text() == "existing-backup"
    assert not (lock_wrapper.parents[1] / "incorrectly-ran").exists()


@pytest.mark.parametrize(
    ("mounted", "conflicts"),
    [
        ("/", True),
        ("/private", True),
        ("/private/data", True),
        ("/private/data/models", True),
        ("/private/database", False),
        ("/other", False),
        ("", False),
    ],
)
def test_writable_mount_overlap_checks_both_ancestor_directions(mounted, conflicts):
    common = Path(__file__).resolve().parents[1] / "scripts" / "maintenance-common.sh"
    script = (
        '. "$1"; pandect_data_dir=/private/data; '
        'printf "%s\\n" "$2" | pandect_conflicting_mount'
    )
    result = subprocess.run(
        ["sh", "-eu", "-c", script, "sh", str(common), mounted],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is conflicts


@pytest.mark.parametrize(
    ("is_mount", "accessible", "accepted"),
    [(True, True, True), (False, True, False), (True, False, False)],
)
def test_private_data_allows_only_effectively_accessible_mapped_mount_root(
    tmp_path, monkeypatch, is_mount, accessible, accepted
):
    data = tmp_path / "data"
    initialize_data_directory(data)
    original_stat = Path.stat

    def mapped_stat(path, *args, **kwargs):
        metadata = original_stat(path, *args, **kwargs)
        if path == data:
            values = list(metadata)
            values[4] = 0  # Docker Desktop's bind-root uid presentation.
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(Path, "stat", mapped_stat)
    monkeypatch.setattr(Path, "is_mount", lambda path: path == data and is_mount)
    monkeypatch.setattr(os, "geteuid", lambda: 9999)
    monkeypatch.setattr(os, "access", lambda path, _mode: path == data and accessible)
    if accepted:
        initialize_data_directory(data)
    else:
        with pytest.raises(UnsafeDataPath, match="owned"):
            initialize_data_directory(data)


@pytest.mark.parametrize("mode", [0o700, 0o755])
def test_host_directory_guard_checks_real_private_permissions(tmp_path, mode):
    directory = tmp_path / "data with spaces"
    directory.mkdir(mode=mode)
    directory.chmod(mode)
    common = Path(__file__).resolve().parents[1] / "scripts" / "maintenance-common.sh"
    result = subprocess.run(
        [
            "sh",
            "-eu",
            "-c",
            '. "$1"; pandect_validate_private_directory "$2"',
            "sh",
            str(common),
            str(directory),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is (mode == 0o700)
    assert stat.S_IMODE(directory.stat().st_mode) == mode


@pytest.mark.parametrize(
    ("target", "variable"), [("backup", "BACKUP_DIR"), ("restore", "BACKUP")]
)
def test_backup_make_recipes_preserve_literal_shell_characters(
    tmp_path, target, variable
):
    makefile = (Path(__file__).resolve().parents[1] / "Makefile").read_text()
    assert f"export {variable}\n" in makefile
    lines = makefile.split(f"{target}: ##", 1)[1].splitlines()[1:]
    recipe = []
    for line in lines:
        if not line.startswith("\t"):
            break
        recipe.append(line.removeprefix("\t").removeprefix("@").replace("$$", "$"))
    assert f"$({variable})" not in "\n".join(recipe)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    fake_maintenance = scripts / "maintenance.sh"
    fake_maintenance.write_text('#!/bin/sh\nprintf "%s\\n" "$1" "$2"\n')
    fake_maintenance.chmod(0o700)
    literal_path = (
        '/private/quote" dollar$HOME `touch injected` $(touch injected2) path'
    )
    # Run the real recipe after Make's $$ escaping, without requiring Make in
    # the dependency-minimal test image. The exported value is shell data.
    result = subprocess.run(
        [
            "sh",
            "-eu",
            "-c",
            "\n".join(recipe).replace(
                "./scripts/maintenance.sh", "sh ./scripts/maintenance.sh"
            ),
        ],
        cwd=tmp_path,
        env={**os.environ, variable: literal_path},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [target, literal_path]
    assert not (tmp_path / "injected").exists()
    assert not (tmp_path / "injected2").exists()
