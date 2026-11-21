import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "compose-smoke.sh"
IMAGE_ID = "sha256:" + "a" * 64

# Exercise shell orchestration without executing fixtures or contacting Docker.
DOCKER_STUB = r"""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
entry = {"args": args}
if args and args[0] == "compose":
    override = Path(args[args.index("--file", 2) + 1])
    environment = Path(args[args.index("--env-file") + 1])
    entry.update(
        override=str(override),
        override_content=override.read_text(),
        environment=str(environment),
        environment_content=environment.read_text(),
        inherited=[
            key for key in os.environ
            if key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
            or key.startswith(("DJANGO_", "POSTGRES_", "PANDECT_", "COMPOSE_"))
        ],
    )
with open(os.environ["SMOKE_DOCKER_LOG"], "a") as output:
    output.write(json.dumps(entry) + "\n")
if args[:2] == ["image", "inspect"]:
    if os.environ.get("SMOKE_MISSING_IMAGE"):
        sys.exit(1)
    print(os.environ.get("SMOKE_RESOLVED_IMAGE", os.environ["SMOKE_IMAGE_ID"]))
elif args[0] == "inspect":
    if args[-1] == "worker" and os.environ.get("SMOKE_WRONG_WORKER_IMAGE"):
        print("sha256:" + "b" * 64)
    else:
        print(os.environ.get("SMOKE_RUNNING_IMAGE", os.environ["SMOKE_IMAGE_ID"]))
elif args[0] == "compose":
    command = args[args.index("--env-file") + 2:]
    if command[:1] == ["config"]:
        project = args[args.index("--project-name") + 1]
        print(json.dumps({
            "name": project,
            "volumes": {
                key: {"name": project + "_" + key}
                for key in ("postgres_data", "app_data")
            },
        }))
    elif command[:1] == ["ps"]:
        print(command[-1])
"""


class ComposeSmokeCommandTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        docker = self.directory / "docker"
        docker.write_text(f"#!{sys.executable}\n" + DOCKER_STUB)
        docker.chmod(0o700)
        self.log = self.directory / "docker-log"

    def run_smoke(self, *arguments, **overrides):
        environment = {
            **os.environ,
            "PATH": str(self.directory) + os.pathsep + os.environ["PATH"],
            "SMOKE_DOCKER_LOG": str(self.log),
            "SMOKE_IMAGE_ID": IMAGE_ID,
            "DJANGO_SECRET_KEY": "synthetic-inherited-secret",
            "POSTGRES_DB": "synthetic-inherited-database",
            "PANDECT_AI_GATEWAY_TOKEN": "synthetic-inherited-token",
            "OPENAI_API_KEY": "synthetic-openai-key",
            "ANTHROPIC_API_KEY": "synthetic-anthropic-key",
            "PANDECT_FUTURE_SETTING": "synthetic-inherited-value",
            "COMPOSE_FILE": "synthetic-inherited-compose",
            "COMPOSE_PROJECT_NAME": "synthetic-inherited-project",
            **overrides,
        }
        result = subprocess.run(
            ["sh", str(SCRIPT), *arguments],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        entries = (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )
        return result, entries

    def compose_commands(self, entries):
        return [
            entry["args"][entry["args"].index("--env-file") + 2 :]
            for entry in entries
            if entry["args"][0] == "compose"
        ]

    def assert_isolated_and_cleaned(self, entries):
        compose = [entry for entry in entries if entry["args"][0] == "compose"]
        self.assertTrue(compose)
        for entry in compose:
            self.assertEqual(entry["inherited"], [])
            self.assertIn("POSTGRES_DB=pandect_smoke", entry["environment_content"])
            self.assertIn("POSTGRES_PORT=0", entry["environment_content"])
            self.assertIn("PANDECT_WEB_PORT=0", entry["environment_content"])
            self.assertFalse(Path(entry["override"]).exists())
            self.assertFalse(Path(entry["environment"]).exists())
        self.assertEqual(
            self.compose_commands(entries)[-1],
            ["down", "--volumes", "--remove-orphans"],
        )

    def test_default_builds_once_and_preserves_isolation_on_restarts(self):
        result, entries = self.run_smoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        starts = [
            command for command in self.compose_commands(entries) if command[0] == "up"
        ]
        self.assertIn("--build", starts[0])
        for command in starts[1:]:
            self.assertIn("--no-build", command)
        self.assert_isolated_and_cleaned(entries)

    def test_prebuilt_image_is_resolved_locally_and_used_for_both_services(self):
        result, entries = self.run_smoke("synthetic-local-image:tested")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            entries[0]["args"],
            [
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                "--",
                "synthetic-local-image:tested",
            ],
        )
        compose = [entry for entry in entries if entry["args"][0] == "compose"]
        override = compose[0]["override_content"]
        self.assertEqual(override.count("image: " + IMAGE_ID), 2)
        self.assertEqual(override.count("pull_policy: never"), 2)
        for command in self.compose_commands(entries):
            self.assertNotIn("--build", command)
            if command[0] == "up":
                self.assertIn("--no-build", command)
        self.assert_isolated_and_cleaned(entries)

    def test_missing_or_invalid_local_image_cannot_start_or_build(self):
        for overrides in (
            {"SMOKE_MISSING_IMAGE": "1"},
            {"SMOKE_RESOLVED_IMAGE": "invalid-image-id"},
        ):
            with self.subTest(overrides=overrides):
                self.log.unlink(missing_ok=True)
                result, entries = self.run_smoke("missing-image", **overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["args"][:2], ["image", "inspect"])

    def test_prebuilt_identity_mismatch_fails_and_cleans_up(self):
        for overrides in (
            {"SMOKE_RUNNING_IMAGE": "sha256:" + "c" * 64},
            {"SMOKE_WRONG_WORKER_IMAGE": "1"},
        ):
            with self.subTest(overrides=overrides):
                self.log.unlink(missing_ok=True)
                result, entries = self.run_smoke("synthetic-local-image", **overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(
                    any(
                        command[0] == "exec"
                        for command in self.compose_commands(entries)
                    )
                )
                self.assert_isolated_and_cleaned(entries)

    def test_extra_arguments_fail_before_docker(self):
        result, entries = self.run_smoke("first-image", "second-image")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(entries, [])
