"""The audit harness must keep sequential image archives readable and private."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest


@pytest.mark.parametrize("fail_maintenance_scan", [False, True])
def test_security_scan_preserves_archive_identity_and_cleans_up(
    tmp_path, fail_maintenance_scan
):
    repository = tmp_path / "private source with spaces"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    harness = scripts / "security-check.sh"
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "scripts/security-check.sh", harness
    )
    for relative in (
        "pyproject.toml",
        "uv.lock",
        "security-exceptions.json",
        "scripts/security_policy.py",
    ):
        (repository / relative).write_text("synthetic public source\n")

    state_path = tmp_path / "docker-state.json"
    docker = tmp_path / "fake_docker.py"
    docker.write_text(
        dedent(
            """\
            import json, os, sys
            from pathlib import Path

            arguments = sys.argv[1:]
            state_path = Path(os.environ['TEST_STATE'])
            state = json.loads(state_path.read_text()) if state_path.exists() else {
                'saved': [], 'scanned': [], 'validated': [],
            }

            def finish(code=0):
                state_path.write_text(json.dumps(state))
                sys.exit(code)

            if arguments[:2] == ['image', 'save']:
                archive = Path(arguments[arguments.index('--output') + 1])
                # Docker Desktop can retain a removed inode after an archive
                # path has been mounted; reject reuse even after unlinking it.
                assert str(archive) not in state['saved']
                assert all(not Path(path).exists() for path in state['saved'])
                kind = 'runtime' if '-runtime:' in arguments[-1] else 'maintenance'
                archive.write_text(kind)
                state['saved'].append(str(archive))
                finish()

            if arguments[0] != 'run':
                finish()
            mounts = {}
            for index, argument in enumerate(arguments):
                if argument == '--mount':
                    mount = dict(part.split('=', 1) if '=' in part else (part, '')
                                 for part in arguments[index + 1].split(','))
                    mounts[mount['dst']] = mount
            if '--entrypoint' in arguments and 'git' in arguments:
                print('3')
            if '--input' in arguments:
                mount = mounts['/image.tar']
                assert 'readonly' in mount
                kind = Path(mount['src']).read_text()
                state['scanned'].append(kind)
                if kind == 'maintenance' and os.environ['TEST_FAIL_SCAN'] == '1':
                    finish(17)
                output = Path(arguments[arguments.index('--output') + 1])
                report = Path(mounts['/report']['src']) / output.name
                report.write_text(kind)
            if '--image-kind' in arguments:
                kind = arguments[arguments.index('--image-kind') + 1]
                report = Path(mounts['/report']['src']) / f'{kind}.json'
                assert report.read_text() == kind
                state['validated'].append(kind)
            finish()
            """
        )
    )
    scratch = tmp_path / "private scratch with spaces"
    scratch.mkdir(mode=0o700)
    result = subprocess.run(
        [
            "sh",
            "-c",
            # Interpret the stub because the hermetic tools container has a
            # noexec /tmp, matching the existing sandbox harness regression.
            'docker() { "$TEST_PYTHON" "$TEST_DOCKER" "$@"; }; '
            'git() { case "$1" in '
            'clone) for argument do :; done; mkdir "$argument" ;; '
            "ls-files) printf '%s\\0' pyproject.toml uv.lock "
            "security-exceptions.json scripts/security_policy.py ;; "
            '*) return 99 ;; esac; }; . "$0"',
            str(harness),
        ],
        env={
            **os.environ,
            "TMPDIR": str(scratch),
            "TEST_PYTHON": sys.executable,
            "TEST_DOCKER": str(docker),
            "TEST_STATE": str(state_path),
            "TEST_FAIL_SCAN": "1" if fail_maintenance_scan else "0",
        },
        capture_output=True,
        text=True,
        check=False,
        umask=0o077,
    )
    assert result.returncode == (17 if fail_maintenance_scan else 0), result.stderr
    state = json.loads(state_path.read_text())
    assert state["scanned"] == ["runtime", "maintenance"]
    assert state["validated"] == (
        ["runtime"] if fail_maintenance_scan else ["runtime", "maintenance"]
    )
    assert len(state["saved"]) == 2
    assert all(Path(path).is_relative_to(scratch.resolve()) for path in state["saved"])
    assert list(scratch.iterdir()) == []
