import ast
import importlib.util
import io
import json
import os
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError
from urllib.request import OpenerDirector

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "classify-ci-changes.py"
SPEC = importlib.util.spec_from_file_location("classify_ci_changes", SCRIPT)
changes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(changes)


DEPLOYMENTS_PATH = "deployments?environment=production&task=pandect-rollout&per_page=1"


class CIChangeClassificationTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="pandect-ci-classification-")
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.root = self.temporary / "repo"
        self.root.mkdir()
        self.event_path = self.temporary / "event.json"
        self.output_path = self.temporary / "output"
        environment = patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_EVENT_PATH": str(self.event_path),
                "RUNNER_TEMP": str(self.temporary),
                "GITHUB_REPOSITORY": "aaronspindler/pandect",
                "GITHUB_API_URL": "https://api.github.com",
                "GITHUB_TOKEN": "synthetic-test-token",
                "GITHUB_OUTPUT": str(self.output_path),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        root = patch.object(changes, "ROOT", self.root)
        root.start()
        self.addCleanup(root.stop)
        self.git("init", "--initial-branch=main", "--quiet")
        self.write("README.md", "# Synthetic project\n")
        self.write("docs/manual.md", "# Synthetic guide\n")
        self.write("app.py", "synthetic = True\n")
        self.initial = self.save()
        self.deployments = [self.record(self.initial)]
        self.statuses = [{"state": "success"}]
        github = patch.object(changes, "github", side_effect=self.response)
        self.github = github.start()
        self.addCleanup(github.stop)

    def record(self, sha=None):
        sha = sha or self.initial
        return {
            "id": 123,
            "task": "pandect-rollout",
            "environment": "production",
            "sha": sha,
            "ref": sha,
            "payload": {
                "ci_run_id": "456",
                "image": "ghcr.io/aaronspindler/pandect@sha256:" + "b" * 64,
            },
        }

    def response(self, path):
        if path == DEPLOYMENTS_PATH:
            return self.deployments
        if path == f"deployments/{self.deployments[0]['id']}/statuses?per_page=1":
            return self.statuses
        raise AssertionError("Unexpected deployment metadata lookup")

    def git(self, *arguments):
        return (
            subprocess.check_output(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "user.name=Synthetic CI test",
                    "-c",
                    "user.email=synthetic@example.test",
                    *arguments,
                ],
                cwd=self.root,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def save(self):
        self.git("add", "--all")
        self.git("commit", "--quiet", "--allow-empty", "-m", "Synthetic CI test")
        return self.git("rev-parse", "HEAD")

    def run_classifier(self, event_name="push", event=None):
        head = self.git("rev-parse", "HEAD")
        if event is None:
            event = {
                "repository": {"full_name": "aaronspindler/pandect"},
                "ref": "refs/heads/main",
                "after": head,
                "forced": False,
            }
        self.event_path.write_text(json.dumps(event))
        self.output_path.write_text("retained=value\n")
        with (
            patch.dict(
                os.environ, {"GITHUB_EVENT_NAME": event_name, "GITHUB_SHA": head}
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            changes.main()
        return dict(
            line.split("=", 1)
            for line in self.output_path.read_text().splitlines()
            if not line.startswith("reason=")
        ), output.getvalue()

    def test_helper_parses_on_the_ci_runners_python_312(self):
        ast.parse(SCRIPT.read_text(), feature_version=(3, 12))

    def test_runner_paths_reject_traversal_siblings_and_symlinks(self):
        with TemporaryDirectory() as outside:
            link = self.temporary / "outside"
            link.symlink_to(outside, target_is_directory=True)
            for name in ("GITHUB_EVENT_PATH", "GITHUB_OUTPUT"):
                for path in (
                    outside,
                    self.temporary / "../escape",
                    str(self.temporary) + "-sibling",
                    link / "file",
                ):
                    with (
                        self.subTest(name=name, path=str(path)),
                        patch.dict(os.environ, {name: str(path)}),
                        self.assertRaises(ValueError),
                    ):
                        changes.runner_file(name)

    def test_docs_only_main_has_verified_cumulative_comparison(self):
        self.write("docs/manual.md", "# Updated synthetic guide\n")
        head = self.save()
        result, output = self.run_classifier()
        self.assertEqual(
            result,
            {"retained": "value", "full": "false", "base": self.initial, "head": head},
        )
        self.assertEqual(
            self.github.call_args_list,
            [call(DEPLOYMENTS_PATH), call("deployments/123/statuses?per_page=1")],
        )
        self.assertIn("Only documentation", output)

    def test_application_push_superseded_by_docs_still_requires_full_ci(self):
        self.write("app.py", "synthetic = False\n")
        self.save()
        self.write("docs/manual.md", "# Subsequent synthetic guide\n")
        self.save()
        result, _ = self.run_classifier()
        self.assertEqual(result["full"], "true")
        self.assertEqual(result["base"], self.initial)

    def test_reverted_in_flight_application_still_requires_a_main_image(self):
        self.write("app.py", "synthetic = False\n")
        self.save()
        # A net endpoint diff must not hide this intermediate application change.
        self.write("app.py", "synthetic = True\n")
        self.write("README.md", "# Documentation accompanying the revert\n")
        head = self.save()
        result, _ = self.run_classifier()
        self.assertEqual(result["full"], "true")
        self.assertEqual(result["base"], self.initial)
        self.assertEqual(result["head"], head)

        # A PR still classifies its final proposed tree, without deployments.
        result, _ = self.run_classifier(
            "pull_request",
            {
                "pull_request": {
                    "base": {"sha": self.initial},
                    "head": {"sha": head},
                }
            },
        )
        self.assertEqual(result["full"], "false")

    def test_multiple_documentation_commits_still_skip_main_application_ci(self):
        self.write("README.md", "# First synthetic documentation update\n")
        self.save()
        self.write("docs/manual.md", "# Second synthetic documentation update\n")
        self.save()
        result, _ = self.run_classifier()
        self.assertEqual(result["full"], "false")

    def test_merged_application_change_is_seen_even_when_later_reverted(self):
        self.git("checkout", "--quiet", "-b", "synthetic-feature")
        self.write("app.py", "synthetic = False\n")
        self.save()
        self.git("checkout", "--quiet", "main")
        self.write("README.md", "# Synthetic main documentation\n")
        self.save()
        self.git(
            "merge", "--no-ff", "--quiet", "synthetic-feature", "-m", "Synthetic merge"
        )
        self.write("app.py", "synthetic = True\n")
        self.save()
        result, _ = self.run_classifier()
        self.assertEqual(result["full"], "true")

    def test_documentation_only_merge_keeps_main_skip(self):
        self.git("checkout", "--quiet", "-b", "synthetic-docs")
        self.write("docs/manual.md", "# Synthetic branch documentation\n")
        self.save()
        self.git("checkout", "--quiet", "main")
        self.write("README.md", "# Synthetic main documentation\n")
        self.save()
        self.git(
            "merge",
            "--no-ff",
            "--quiet",
            "synthetic-docs",
            "-m",
            "Synthetic docs merge",
        )
        result, _ = self.run_classifier()
        self.assertEqual(result["full"], "false")

    def test_reverting_deployed_application_requires_full_ci(self):
        self.write("app.py", "synthetic = False\n")
        deployed = self.save()
        self.deployments = [self.record(deployed)]
        self.write("app.py", "synthetic = True\n")
        self.save()
        self.write("README.md", "# Synthetic docs after revert\n")
        self.save()
        result, _ = self.run_classifier()
        self.assertEqual(result["full"], "true")
        self.assertEqual(result["base"], deployed)

    def test_successful_latest_rollout_outside_current_history_requires_full(self):
        self.write("app.py", "synthetic = False\n")
        deployed = self.save()
        self.deployments = [self.record(deployed)]
        self.git("reset", "--hard", self.initial)
        self.write("README.md", "# Synthetic docs after forced rewrite\n")
        self.save()
        result, _ = self.run_classifier()
        self.assertEqual(result, {"retained": "value", "full": "true"})
        self.assertEqual(
            self.github.call_args_list,
            [call(DEPLOYMENTS_PATH), call("deployments/123/statuses?per_page=1")],
        )

    def test_forced_push_cannot_hide_an_untagged_application_revision(self):
        self.write("app.py", "synthetic = False\n")
        before = self.save()
        self.git("reset", "--hard", self.initial)
        self.write("README.md", "# Synthetic docs after forced removal\n")
        head = self.save()
        result, _ = self.run_classifier(
            event={
                "repository": {"full_name": "aaronspindler/pandect"},
                "ref": "refs/heads/main",
                "before": before,
                "after": head,
                "forced": True,
            }
        )
        self.assertEqual(result, {"retained": "value", "full": "true"})
        self.github.assert_not_called()

    def test_missing_or_malformed_force_metadata_requires_full_checks(self):
        self.write("README.md", "# Synthetic documentation update\n")
        head = self.save()
        event = {
            "repository": {"full_name": "aaronspindler/pandect"},
            "ref": "refs/heads/main",
            "after": head,
        }
        result, _ = self.run_classifier(event=event)
        self.assertEqual(result, {"retained": "value", "full": "true"})
        for forced in (True, None, 0, "false", [], {}):
            with self.subTest(forced=forced):
                result, _ = self.run_classifier(event={**event, "forced": forced})
                self.assertEqual(result, {"retained": "value", "full": "true"})
        self.github.assert_not_called()

    def test_documentation_pr_uses_merge_base_when_base_advances(self):
        self.git("checkout", "--quiet", "-b", "synthetic-docs")
        self.write("README.md", "# Synthetic PR docs\n")
        head = self.save()
        self.git("checkout", "--quiet", "main")
        self.write("app.py", "synthetic = False\n")
        base = self.save()
        result, _ = self.run_classifier(
            "pull_request",
            {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}},
        )
        self.assertEqual(result["full"], "false")
        self.assertEqual(result["base"], self.initial)
        self.assertEqual(result["head"], head)
        self.github.assert_not_called()

    def test_mixed_pr_and_runtime_markdown_require_full_ci(self):
        for path in (
            "app.py",
            "ui/templates/runtime.md",
            "docs/example.py",
            "Dockerfile",
            ".github/workflows/ci.yml",
            "uv.lock",
        ):
            with self.subTest(path=path):
                self.git("reset", "--hard", self.initial)
                self.write("README.md", "# Synthetic PR documentation\n")
                self.write(path, "Synthetic runtime change\n")
                head = self.save()
                result, _ = self.run_classifier(
                    "pull_request",
                    {
                        "pull_request": {
                            "base": {"sha": self.initial},
                            "head": {"sha": head},
                        }
                    },
                )
                self.assertEqual(result["full"], "true")

    def test_documentation_deletion_is_allowed_and_runtime_moves_are_not(self):
        (self.root / "docs/manual.md").unlink()
        self.save()
        result, _ = self.run_classifier()
        self.assertEqual(result["full"], "false")
        self.git("mv", "app.py", "docs/app.md")
        self.save()
        result, _ = self.run_classifier()
        self.assertEqual(result["full"], "true")

    def test_all_explicit_guides_and_nested_markdown_can_skip(self):
        for path in changes.ROOT_GUIDES | {"docs/nested/synthetic.md"}:
            self.write(path, "# Synthetic contributor documentation\n")
        self.save()
        result, _ = self.run_classifier()
        self.assertEqual(result["full"], "false")

    def test_shallow_history_and_multiple_merge_bases_require_full_checks(self):
        self.write("README.md", "# Synthetic PR docs\n")
        head = self.save()
        event = {
            "pull_request": {
                "base": {"sha": self.initial},
                "head": {"sha": head},
            }
        }
        original_git = changes.git

        def shallow_git(*arguments):
            if arguments == ("rev-parse", "--is-shallow-repository"):
                return b"true"
            return original_git(*arguments)

        def ambiguous_git(*arguments):
            if arguments[:2] == ("merge-base", "--all"):
                return f"{self.initial}\n{'a' * 40}".encode()
            return original_git(*arguments)

        for git in (shallow_git, ambiguous_git):
            with (
                self.subTest(comparison=git.__name__),
                patch.object(changes, "git", side_effect=git),
            ):
                result, _ = self.run_classifier("pull_request", event)
                self.assertEqual(result, {"retained": "value", "full": "true"})
        self.github.assert_not_called()

    def test_paths_with_newlines_are_not_split_into_docs_only_names(self):
        self.write("runtime\nREADME.md", "Synthetic runtime change\n")
        self.save()
        result, output = self.run_classifier()
        self.assertEqual(result["full"], "true")
        self.assertNotIn("runtime", output)

    def test_manual_fork_and_non_main_events_run_full(self):
        for name in ("workflow_dispatch", "schedule", "unknown"):
            with self.subTest(event=name):
                result, _ = self.run_classifier(name)
                self.assertEqual(result["full"], "true")
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "example/fork"}):
            result, _ = self.run_classifier()
            self.assertEqual(result["full"], "true")
        result, _ = self.run_classifier(
            event={"ref": "refs/heads/other", "after": self.initial}
        )
        self.assertEqual(result["full"], "true")
        self.github.assert_not_called()

    def test_bad_event_missing_git_object_and_wrong_checkout_run_full(self):
        for event in (
            None,
            [],
            {},
            {"pull_request": {"base": {"sha": "main"}, "head": {"sha": self.initial}}},
            {
                "pull_request": {
                    "base": {"sha": "b" * 40},
                    "head": {"sha": self.initial},
                }
            },
        ):
            with self.subTest(event=event):
                result, _ = self.run_classifier("pull_request", event=event)
                self.assertEqual(result["full"], "true")
        result, _ = self.run_classifier(
            event={"ref": "refs/heads/main", "after": "b" * 40}
        )
        self.assertEqual(result, {"retained": "value", "full": "true"})

    def test_latest_pending_or_failed_rollout_masks_older_success_after_forced_reset(
        self,
    ):
        self.write("app.py", "synthetic = False\n")
        attempted = self.save()
        self.git("reset", "--hard", self.initial)
        self.write("README.md", "# Synthetic forced revert documentation\n")
        reverted = self.save()
        result, _ = self.run_classifier(
            event={
                "repository": {"full_name": "aaronspindler/pandect"},
                "ref": "refs/heads/main",
                "before": attempted,
                "after": reverted,
                "forced": True,
            }
        )
        self.assertEqual(result["full"], "true")
        self.write("docs/manual.md", "# Subsequent ordinary documentation\n")
        self.save()
        self.deployments = [self.record(attempted)]
        for state in ("pending", "in_progress", "failure", "error", "success"):
            with self.subTest(state=state):
                self.statuses = [{"state": state}]
                self.github.reset_mock()
                result, _ = self.run_classifier()
                self.assertEqual(result, {"retained": "value", "full": "true"})
                self.assertEqual(
                    self.github.call_args_list,
                    [
                        call(DEPLOYMENTS_PATH),
                        call("deployments/123/statuses?per_page=1"),
                    ],
                )

    def test_newest_successful_rollout_sets_the_comparison_baseline(self):
        self.write("app.py", "synthetic = False\n")
        deployed = self.save()
        self.deployments = [self.record(deployed)]
        self.write("README.md", "# Documentation after verified rollout\n")
        self.save()
        result, _ = self.run_classifier()
        self.assertEqual(result["full"], "false")
        self.assertEqual(result["base"], deployed)
        self.assertEqual(
            self.github.call_args_list,
            [call(DEPLOYMENTS_PATH), call("deployments/123/statuses?per_page=1")],
        )

    def test_legacy_tags_and_late_release_publication_do_not_supply_a_baseline(self):
        self.git("tag", f"deploy-{self.initial}", self.initial)
        self.write("README.md", "# Synthetic documentation update\n")
        self.save()
        self.deployments = []
        result, _ = self.run_classifier()
        self.assertEqual(result, {"retained": "value", "full": "true"})
        self.github.assert_called_once_with(DEPLOYMENTS_PATH)

    def test_current_status_alone_controls_the_latest_rollout(self):
        self.write("README.md", "# Synthetic documentation update\n")
        self.save()
        for statuses in (
            [],
            [{"state": "pending"}],
            [{"state": "in_progress"}],
            [{"state": "failure"}],
            [{"state": "error"}],
            [{"state": "inactive"}],
            [{"state": "unknown"}],
            [{}],
            [None],
            [{"state": "success"}, {"state": "failure"}],
        ):
            with self.subTest(statuses=statuses):
                self.statuses = statuses
                result, _ = self.run_classifier()
                self.assertEqual(result, {"retained": "value", "full": "true"})

    def test_missing_ambiguous_or_malformed_rollout_identity_requires_full_checks(self):
        self.write("README.md", "# Synthetic documentation update\n")
        self.save()
        valid = self.record()
        invalid_records = [
            {**valid, "id": True},
            {**valid, "id": 0},
            {**valid, "task": "deploy"},
            {**valid, "environment": "preview"},
            {**valid, "sha": "main"},
            {**valid, "ref": "main"},
            {**valid, "sha": "c" * 40, "ref": "c" * 40},
            {**valid, "payload": "synthetic"},
            {},
            None,
        ]
        for payload in (
            {},
            {**valid["payload"], "ci_run_id": 456},
            {**valid["payload"], "ci_run_id": "0"},
            {**valid["payload"], "ci_run_id": "-1"},
            {**valid["payload"], "image": "ghcr.io/aaronspindler/pandect:latest"},
            {**valid["payload"], "image": "ghcr.io/example/pandect@sha256:" + "b" * 64},
        ):
            invalid_records.append({**valid, "payload": payload})
        for records in ([], [valid, valid], *([item] for item in invalid_records)):
            with self.subTest(records=records):
                self.deployments = records
                result, _ = self.run_classifier()
                self.assertEqual(result, {"retained": "value", "full": "true"})

    def test_successful_rollout_must_be_on_current_first_parent_history(self):
        self.git("checkout", "--quiet", "-b", "synthetic-docs")
        self.write("docs/manual.md", "# Synthetic branch documentation\n")
        branch = self.save()
        self.git("checkout", "--quiet", "main")
        self.write("README.md", "# Synthetic main documentation\n")
        self.save()
        self.git(
            "merge", "--no-ff", "--quiet", "synthetic-docs", "-m", "Synthetic merge"
        )
        self.deployments = [self.record(branch)]
        result, _ = self.run_classifier()
        self.assertEqual(result, {"retained": "value", "full": "true"})

    def test_deployment_and_status_transport_errors_require_full_checks_without_details(
        self,
    ):
        self.write("README.md", "# Synthetic documentation update\n")
        self.save()
        for responses in (
            [URLError("Private synthetic deployment detail")],
            [self.deployments, URLError("Private synthetic status detail")],
            [ValueError("Private synthetic malformed response")],
        ):
            with self.subTest(stage=len(responses)):
                self.github.side_effect = responses
                result, output = self.run_classifier()
                self.assertEqual(result, {"retained": "value", "full": "true"})
                self.assertNotIn("Private synthetic", output)


class CIClassificationTransportTests(TestCase):
    def setUp(self):
        environment = patch.dict(
            os.environ,
            {
                "GITHUB_API_URL": "https://api.github.com",
                "GITHUB_REPOSITORY": "aaronspindler/pandect",
                "GITHUB_TOKEN": "synthetic-test-token",
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_array_transport_is_bounded_authenticated_and_rejects_redirects(self):
        body = json.dumps([{"id": 123}]).encode()
        with patch.object(OpenerDirector, "open") as request:
            request.return_value.__enter__.return_value.read.return_value = body
            self.assertEqual(changes.github(DEPLOYMENTS_PATH), [{"id": 123}])
        self.assertEqual(request.call_args.kwargs["timeout"], 30)
        sent = request.call_args.args[0]
        self.assertEqual(
            sent.get_header("Authorization"), "Bearer synthetic-test-token"
        )
        self.assertEqual(
            sent.full_url,
            "https://api.github.com/repos/aaronspindler/pandect/" + DEPLOYMENTS_PATH,
        )
        request.return_value.__enter__.return_value.read.assert_called_once_with(
            changes.MAX_RESPONSE_BYTES + 1
        )
        self.assertIsNone(
            changes.NoRedirect().redirect_request(
                None, None, 302, "Synthetic", {}, "https://example.test/"
            )
        )

    def test_http_network_malformed_and_oversized_responses_require_full_checks(self):
        error = HTTPError(
            "https://api.github.com/synthetic",
            404,
            "Synthetic",
            {},
            io.BytesIO(b"private synthetic body"),
        )
        for failure in (error, URLError("private synthetic network detail")):
            with (
                self.subTest(failure=type(failure).__name__),
                patch.object(OpenerDirector, "open", side_effect=failure),
                self.assertRaises((ValueError, OSError)),
            ):
                changes.github(DEPLOYMENTS_PATH)
        for body in (
            b"{}",
            b"null",
            b"not-json",
            b"x" * (changes.MAX_RESPONSE_BYTES + 1),
        ):
            with (
                self.subTest(length=len(body)),
                patch.object(OpenerDirector, "open") as request,
            ):
                request.return_value.__enter__.return_value.read.return_value = body
                with self.assertRaises(ValueError):
                    changes.github(DEPLOYMENTS_PATH)
