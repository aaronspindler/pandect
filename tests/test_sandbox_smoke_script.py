"""The sandbox harness must work with private Linux source exports."""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("fail_client", [False, True])
def test_sandbox_stages_readable_public_fixtures_without_changing_sources(
    tmp_path, fail_client
):
    repository = tmp_path / "private source with spaces"
    repository.mkdir(mode=0o700)
    scripts = repository / "scripts"
    scripts.mkdir(mode=0o700)
    harness = scripts / "sandbox-smoke.sh"
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "scripts/sandbox-smoke.sh", harness
    )
    fixtures = {
        "tests/sandbox_smoke.py": b"synthetic client fixture\n",
        "tests/sandbox_fixtures/operations.py": b"synthetic parser fixture\n",
        "tests/sandbox_fixtures/public-model.txt": b"synthetic public model\n",
    }
    for relative, content in fixtures.items():
        source = repository / relative
        source.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        source.write_bytes(content)
        source.chmod(0o600)
    original_modes = {
        path: stat.S_IMODE(path.stat().st_mode)
        for path in (repository, *repository.rglob("*"))
    }

    mounted = tmp_path / "mounts.jsonl"
    docker = tmp_path / "fake_docker.py"
    docker.write_text(
        "import json, os, stat, sys\n"
        "from pathlib import Path\n"
        "arguments = sys.argv[1:]\n"
        "if arguments[0] == 'run':\n"
        "    for index, argument in enumerate(arguments):\n"
        "        if argument != '--mount':\n"
        "            continue\n"
        "        mount = dict(part.split('=', 1) if '=' in part else (part, '') "
        "for part in arguments[index + 1].split(','))\n"
        "        if mount.get('type') != 'bind':\n"
        "            continue\n"
        "        source = Path(mount['source'])\n"
        "        record = {**mount, 'mode': stat.S_IMODE(source.stat().st_mode), "
        "'parent_mode': stat.S_IMODE(source.parent.stat().st_mode), "
        "'content': source.read_text()}\n"
        "        with Path(os.environ['TEST_MOUNTS']).open('a') as log:\n"
        "            log.write(json.dumps(record) + '\\n')\n"
        "    if '/sandbox_smoke.py' in arguments "
        "and os.environ['TEST_FAIL_CLIENT'] == '1':\n"
        "        sys.exit(17)\n"
    )
    scratch = tmp_path / "private scratch with spaces"
    scratch.mkdir(mode=0o700)
    result = subprocess.run(
        [
            "sh",
            "-c",
            # The tools container deliberately mounts /tmp noexec. Interpret
            # the stub rather than adding an executable exception to the tests.
            'docker() { "$TEST_PYTHON" "$TEST_DOCKER" "$@"; }; . "$0"',
            str(harness),
            "synthetic-runtime:local",
        ],
        env={
            **os.environ,
            "TMPDIR": str(scratch),
            "TEST_PYTHON": sys.executable,
            "TEST_DOCKER": str(docker),
            "TEST_MOUNTS": str(mounted),
            "TEST_FAIL_CLIENT": "1" if fail_client else "0",
        },
        capture_output=True,
        text=True,
        check=False,
        umask=0o077,
    )
    assert result.returncode == (17 if fail_client else 0), result.stderr
    records = [json.loads(line) for line in mounted.read_text().splitlines()]
    assert {record["target"] for record in records} == {
        "/app/pandect/media_runner/operations.py",
        "/models/public-model.txt",
        "/sandbox_smoke.py",
    }
    for record in records:
        source = Path(record["source"])
        assert source.is_relative_to(scratch.resolve())
        assert record["mode"] == 0o444
        assert record["parent_mode"] == 0o700
        assert "readonly" in record
        relative = next(
            relative for relative in fixtures if Path(relative).name == source.name
        )
        assert record["content"].encode() == fixtures[relative]
        assert not source.parent.exists()
    for relative, content in fixtures.items():
        source = repository / relative
        assert source.read_bytes() == content
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
    for path, mode in original_modes.items():
        assert stat.S_IMODE(path.stat().st_mode) == mode
    assert list(scratch.iterdir()) == []
