"""Exercise the commit/push guard using disposable Git repositories only."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-repository-hygiene.py"
ZERO = "0" * 40


class RepositoryHygieneTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = Path(temporary.name)
        # Hooks may run these tests inside a Git environment; isolate the fixture.
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        self.git("init", "--quiet")
        self.git("config", "user.name", "Synthetic test")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.repository / ".githooks"))
        shutil.copy2(ROOT / ".gitignore", self.repository / ".gitignore")
        self.write("fixture.py", 'CONTENT = "Synthetic test content"\n')
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "Synthetic source")

    def git(self, *arguments):
        return (
            subprocess.check_output(
                ["git", *arguments],
                cwd=self.repository,
                env=self.environment,
                stderr=subprocess.PIPE,
            )
            .decode()
            .strip()
        )

    def write(self, name, content="Synthetic fixture; never real evidence\n"):
        path = self.repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def check(self, mode, input=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), mode],
            cwd=self.repository,
            env=self.environment,
            input=input,
            text=True,
            capture_output=True,
        )

    def install_hooks(self):
        shutil.copytree(ROOT / ".githooks", self.repository / ".githooks")
        (self.repository / "scripts").mkdir()
        shutil.copy2(SCRIPT, self.repository / "scripts" / SCRIPT.name)

    def test_source_fixtures_and_approved_ui_assets_are_allowed(self):
        for path in (
            "ui/static/ui/logo.png",
            "docs/design/example.png",
            ".env.example",
        ):
            self.write(path)
            self.git("add", path)
        result = self.check("--staged")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.check("--history").returncode, 0)

    def test_force_added_local_directories_block_without_exposing_filenames(self):
        for directory in (
            "data/test-evidence",
            "media",
            "uploads",
            "exports",
            "artifacts",
            "backups",
            "local-evidence",
            "local_evidence",
            "test-evidence",
            "test_evidence",
            "secrets",
            "credentials",
        ):
            with self.subTest(directory=directory):
                name = f"{directory}/private-sentinel.txt"
                self.write(name)
                self.git("check-ignore", name)
                self.git("add", "--force", name)
                result = self.check("--staged")
                self.assertEqual(result.returncode, 1)
                self.assertIn("local storage", result.stderr)
                self.assertNotIn("private-sentinel", result.stderr + result.stdout)
                self.assertNotIn("Synthetic fixture", result.stderr + result.stdout)
                self.git("rm", "--cached", "--force", name)

    def test_evidence_formats_and_config_are_blocked_outside_ignored_storage(self):
        for name in (
            "phone/tests/sample.csv",
            "sample.PDF",
            "sample.zip",
            "sample.wav",
            "sample.png",
            "sample.sqlite3-wal",
            "sample.db",
            "sample.sql",
            "sample.eml",
            "sample.geojson",
            "sample.jsonl",
            ".env",
            ".env.local",
            "nested/.env.example",
            "secret.key",
        ):
            with self.subTest(name=name):
                self.write(name)
                self.git("add", "--force", name)
                self.assertEqual(self.check("--staged").returncode, 1)
                self.git("rm", "--cached", "--force", name)

    def test_staged_tree_is_checked_instead_of_worktree(self):
        self.write("data/test-evidence/synthetic.txt")
        self.git("add", "--force", "data/test-evidence/synthetic.txt")
        (self.repository / "data/test-evidence/synthetic.txt").unlink()
        self.assertEqual(self.check("--staged").returncode, 1)
        self.git("rm", "--cached", "data/test-evidence/synthetic.txt")
        self.assertEqual(self.check("--staged").returncode, 0)

    def test_history_blocks_file_even_after_later_deletion(self):
        self.write("export.csv")
        self.git("add", "export.csv")
        self.git("commit", "--quiet", "-m", "Accidental synthetic export")
        self.git("rm", "export.csv")
        self.git("commit", "--quiet", "-m", "Remove synthetic export")
        self.assertEqual(self.check("--staged").returncode, 0)
        self.assertEqual(self.check("--history").returncode, 1)
        tip = self.git("rev-parse", "HEAD")
        self.git(
            "-c", "tag.gpgsign=false", "tag", "-a", "synthetic", "-m", "Synthetic tag"
        )
        tag = self.git("rev-parse", "refs/tags/synthetic")
        for ref, oid in (("refs/heads/main", tip), ("refs/tags/synthetic", tag)):
            with self.subTest(ref=ref):
                result = self.check("--pre-push", f"{ref} {oid} {ref} {ZERO}\n")
                self.assertEqual(result.returncode, 1)
        deletion = self.check("--pre-push", f"(delete) {ZERO} refs/heads/main {tip}\n")
        self.assertEqual(deletion.returncode, 0)

    def test_invalid_push_input_and_missing_history_fail_closed(self):
        self.assertEqual(self.check("--pre-push", "invalid\n").returncode, 1)
        self.assertEqual(
            self.check("--pre-push", f"a {'f' * 40} b {ZERO}\n").returncode, 1
        )
        (self.repository / ".git" / "shallow").write_text(
            self.git("rev-parse", "HEAD") + "\n"
        )
        result = self.check("--history")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Full history is required", result.stderr)

    def test_installed_commit_hook_blocks_force_added_evidence(self):
        self.install_hooks()
        self.write("data/test-evidence/synthetic.txt")
        self.git("add", "--force", "data/test-evidence/synthetic.txt")
        result = subprocess.run(
            ["git", "commit", "-m", "Must fail"],
            cwd=self.repository,
            env=self.environment,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Repository hygiene blocked", result.stderr)

    def test_installed_push_hook_blocks_history_before_remote_receives_it(self):
        self.write("export.csv")
        self.git("add", "export.csv")
        self.git("commit", "--quiet", "-m", "Synthetic unsafe history")
        self.install_hooks()
        with TemporaryDirectory() as remote:
            self.git("init", "--quiet", "--bare", remote)
            self.git("remote", "add", "test", remote)
            result = subprocess.run(
                ["git", "push", "test", "HEAD:refs/heads/main"],
                cwd=self.repository,
                env=self.environment,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Repository hygiene blocked", result.stderr)
            refs = self.git("--git-dir", remote, "for-each-ref", "--format=%(refname)")
            self.assertEqual(refs, "")

    def test_installed_hooks_allow_source_commit_and_push(self):
        self.install_hooks()
        self.write("fixture.py", 'CONTENT = "Updated synthetic source"\n')
        self.git("add", "fixture.py")
        self.git("commit", "--quiet", "-m", "Synthetic source update")
        with TemporaryDirectory() as remote:
            self.git("init", "--quiet", "--bare", remote)
            self.git("remote", "add", "test", remote)
            self.git("push", "test", "HEAD:refs/heads/main")
            self.assertEqual(
                self.git("--git-dir", remote, "rev-parse", "refs/heads/main"),
                self.git("rev-parse", "HEAD"),
            )
