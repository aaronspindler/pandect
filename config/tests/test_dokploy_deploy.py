import ast
import importlib.util
import io
import itertools
import os
from contextlib import redirect_stdout
from email.message import Message
from http.client import IncompleteRead
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError
from urllib.request import HTTPHandler, HTTPSHandler, OpenerDirector
from urllib.response import addinfourl

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-dokploy.py"
SPEC = importlib.util.spec_from_file_location("deploy_dokploy", SCRIPT)
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)

SHA = "a" * 40
VERSION = "v1.42"
IMAGE = "ghcr.io/example/pandect@sha256:" + "b" * 64
ENVIRONMENT = {
    "TESTED_SHA": SHA,
    "PANDECT_VERSION": VERSION,
    "GITHUB_REPOSITORY": "example/pandect",
    "GITHUB_API_URL": "https://api.github.com",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_RUN_ID": "456",
    "GITHUB_TOKEN": "synthetic-github-token",
    "CI_RUN_ID": "123",
    "PANDECT_PRODUCTION_DATABASE_BASELINE": "domain-apps-v1",
    "DOKPLOY_URL": "https://deploy.example.test",
    "DOKPLOY_API_KEY": "synthetic-dokploy-key",
    "DOKPLOY_REGISTRY_PASSWORD": "synthetic-registry-password",
    "DOKPLOY_WEB_APPLICATION_ID": "web-id",
    "DOKPLOY_WORKER_APPLICATION_ID": "worker-id",
    "PANDECT_IMAGE": IMAGE,
}


class DeploymentCommandTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "github-output"
        self.output.touch()
        self.environment = patch.dict(
            os.environ,
            {
                **ENVIRONMENT,
                "GITHUB_OUTPUT": str(self.output),
                "RUNNER_TEMP": str(self.output.parent),
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        marker = patch.object(deploy, "create_rollout", return_value=77)
        status = patch.object(deploy, "mark_rollout")
        marker.start()
        status.start()
        self.addCleanup(marker.stop)
        self.addCleanup(status.stop)
        history = patch.object(deploy, "eligible_release", return_value=True)
        history.start()
        self.addCleanup(history.stop)

    def test_helper_parses_on_the_cd_runners_python_312(self):
        ast.parse(SCRIPT.read_text(), feature_version=(3, 12))

    def test_runner_output_paths_cannot_escape_through_symlinks_or_traversal(self):
        with TemporaryDirectory() as outside:
            link = self.output.parent / "outside"
            link.symlink_to(outside, target_is_directory=True)
            for name in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
                for path in (
                    outside,
                    self.output.parent / "../escape",
                    str(self.output.parent) + "-sibling",
                    link / "file",
                ):
                    with (
                        self.subTest(name=name, path=str(path)),
                        patch.dict(os.environ, {name: str(path)}),
                        self.assertRaises(deploy.DeploymentError),
                    ):
                        deploy.runner_file(name)

    def test_untrusted_github_url_or_repository_fails_before_requests(self):
        for name, value in (
            ("GITHUB_API_URL", "https://api.github.com.attacker.test"),
            ("GITHUB_API_URL", "http://api.github.com"),
            ("GITHUB_API_URL", "https://api.github.com@attacker.test"),
            ("GITHUB_API_URL", "https://api.github.com/private"),
            ("GITHUB_REPOSITORY", "../private"),
            ("GITHUB_REPOSITORY", "example/pandect/../../private"),
            ("GITHUB_REPOSITORY", "-" * 100000 + "/repo"),
        ):
            with (
                self.subTest(name=name),
                patch.dict(os.environ, {name: value}),
                patch.object(OpenerDirector, "open") as request,
                self.assertRaises(deploy.DeploymentError),
            ):
                deploy.preflight()
            request.assert_not_called()

    def test_invalid_or_missing_version_blocks_mutations(self):
        for version in (
            "",
            SHA,
            "deploy-" + SHA,
            "v1",
            "v1.2.3",
            "v01.2",
            "v1.02",
            "v1.2/other",
            "v1.2\n",
        ):
            with (
                self.subTest(version=version),
                patch.dict(os.environ, {"PANDECT_VERSION": version}),
                patch.object(deploy, "api") as request,
                patch.object(deploy, "dokploy") as remote,
                self.assertRaises(deploy.DeploymentError),
            ):
                deploy.main()
            request.assert_not_called()
            remote.assert_not_called()

    def test_native_deploy_accepts_an_empty_success_response(self):
        with patch.object(OpenerDirector, "open", return_value=io.BytesIO(b"")):
            self.assertIsNone(
                deploy.api("https://deploy.example.test/api/application.deploy")
            )

    def test_dokploy_requests_validate_the_origin_before_sending_credentials(self):
        for value in (
            "http://deploy.example.test",
            "https://user:password@deploy.example.test",
            "https://deploy.example.test/api",
            "https://deploy.example.test?redirect=elsewhere",
            "https://deploy.example.test#fragment",
            "https://deploy.example.test\n",
        ):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"DOKPLOY_URL": value}),
                patch.object(deploy, "api") as request,
                self.assertRaises(deploy.DeploymentError),
            ):
                deploy.dokploy("application.one", applicationId="synthetic")
            request.assert_not_called()

    def test_stale_ci_cannot_mutate_dokploy(self):
        with (
            patch.object(deploy, "eligible_release", return_value=False),
            patch.object(deploy, "deploy") as activate,
            redirect_stdout(io.StringIO()),
        ):
            deploy.main()
        activate.assert_not_called()
        self.assertEqual(self.output.read_text(), "")

    def test_worker_is_verified_before_web_activation(self):
        self.output.write_text("existing=retained\n")

        def verify(application_id, image, sha, service):
            self.assertEqual(self.output.read_text(), "existing=retained\n")

        with (
            patch.object(deploy, "api", return_value={"object": {"sha": SHA}}),
            patch.object(deploy, "deploy", side_effect=verify) as activate,
        ):
            deploy.main()
        self.assertEqual(
            activate.call_args_list,
            [
                call("worker-id", IMAGE, SHA, "worker"),
                call("web-id", IMAGE, SHA, "web"),
            ],
        )
        self.assertEqual(self.output.read_text(), "existing=retained\ndeployed=true\n")

    def test_missing_output_blocks_deployment(self):
        del os.environ["GITHUB_OUTPUT"]
        with (
            patch.object(deploy, "api", return_value={"object": {"sha": SHA}}),
            patch.object(deploy, "deploy") as activate,
            self.assertRaisesRegex(deploy.DeploymentError, "GITHUB_OUTPUT"),
        ):
            deploy.main()
        activate.assert_not_called()

    def test_missing_database_baseline_blocks_all_dokploy_requests(self):
        del os.environ["PANDECT_PRODUCTION_DATABASE_BASELINE"]
        with (
            patch.object(deploy, "api", return_value={"object": {"sha": SHA}}),
            patch.object(deploy, "dokploy") as request,
            self.assertRaisesRegex(deploy.DeploymentError, "fresh PostgreSQL database"),
        ):
            deploy.main()
        request.assert_not_called()

    def test_invalid_shared_image_is_rejected_before_worker_mutation(self):
        for image in (
            "ghcr.io/example/pandect:latest",
            IMAGE.replace("example/", "unrelated/"),
            IMAGE.replace("pandect@", "pandect-worker@"),
            IMAGE.replace("pandect@", "pandect-web@"),
            IMAGE[:-1],
        ):
            with (
                self.subTest(image=image),
                patch.dict(os.environ, {"PANDECT_IMAGE": image}),
                patch.object(deploy, "api", return_value={"object": {"sha": SHA}}),
                patch.object(deploy, "deploy") as activate,
                self.assertRaisesRegex(deploy.DeploymentError, "immutable digest"),
            ):
                deploy.main()
            activate.assert_not_called()

    def test_distinct_applications_are_required(self):
        os.environ["DOKPLOY_WEB_APPLICATION_ID"] = "worker-id"
        with (
            patch.object(deploy, "api", return_value={"object": {"sha": SHA}}),
            patch.object(deploy, "deploy") as activate,
            self.assertRaisesRegex(deploy.DeploymentError, "separate"),
        ):
            deploy.main()
        activate.assert_not_called()

    def test_failed_worker_prevents_web_deployment(self):
        with (
            patch.object(deploy, "api", return_value={"object": {"sha": SHA}}),
            patch.object(
                deploy, "deploy", side_effect=deploy.DeploymentError("failed")
            ) as activate,
            self.assertRaises(deploy.DeploymentError),
        ):
            deploy.main()
        self.assertEqual(activate.call_count, 1)
        self.assertEqual(activate.call_args.args[-1], "worker")
        self.assertEqual(self.output.read_text(), "")

    def test_failed_web_does_not_report_deployment_success(self):
        with (
            patch.object(deploy, "api", return_value={"object": {"sha": SHA}}),
            patch.object(
                deploy, "deploy", side_effect=[None, deploy.DeploymentError("failed")]
            ) as activate,
            self.assertRaises(deploy.DeploymentError),
        ):
            deploy.main()
        self.assertEqual(activate.call_count, 2)
        self.assertEqual(activate.call_args.args[-1], "web")
        self.assertEqual(self.output.read_text(), "")

    def test_native_provider_update_preserves_runtime_configuration(self):
        with (
            patch.object(deploy, "dokploy", return_value=[]) as request,
            patch.object(deploy, "wait_ready") as readiness,
            redirect_stdout(io.StringIO()) as output,
        ):
            deploy.deploy("worker-id", IMAGE, SHA, "worker")
        self.assertEqual(
            [item.args[0] for item in request.call_args_list],
            [
                "deployment.all",
                "application.update",
                "application.saveDockerProvider",
                "application.deploy",
            ],
        )
        update = request.call_args_list[1].args[1]
        self.assertEqual(
            set(update),
            {"applicationId", "replicas", "command", "args", "updateConfigSwarm"},
        )
        self.assertEqual(update["replicas"], 1)
        self.assertEqual(update["command"], "sh")
        self.assertEqual(update["args"], ["/app/scripts/docker-entrypoint-worker.sh"])
        self.assertEqual(update["updateConfigSwarm"]["Order"], "stop-first")
        provider = request.call_args_list[2].args[1]
        self.assertEqual(provider["dockerImage"], IMAGE)
        self.assertEqual(provider["registryUrl"], "ghcr.io")
        activation = request.call_args_list[3].args[1]
        self.assertEqual(activation["title"], f"Deploy worker {VERSION} CI 123")
        self.assertNotIn(ENVIRONMENT["DOKPLOY_REGISTRY_PASSWORD"], output.getvalue())
        readiness.assert_called_once()

    def test_active_deployment_blocks_mutation(self):
        with (
            patch.object(
                deploy, "dokploy", return_value=[{"status": "running"}]
            ) as request,
            self.assertRaisesRegex(deploy.DeploymentError, "active"),
        ):
            deploy.deploy("worker-id", IMAGE, SHA, "worker")
        request.assert_called_once()

    def test_web_rollout_starts_the_new_immutable_release_first(self):
        with (
            patch.object(deploy, "dokploy", return_value=[]) as request,
            patch.object(deploy, "wait_ready"),
            redirect_stdout(io.StringIO()),
        ):
            deploy.deploy("web-id", IMAGE, SHA, "web")
        update = request.call_args_list[1].args[1]
        self.assertEqual(update["replicas"], 1)
        self.assertIsNone(update["command"])
        self.assertIsNone(update["args"])
        self.assertEqual(update["updateConfigSwarm"]["Order"], "start-first")
        self.assertNotIn("healthCheckSwarm", update)

    def test_failed_provider_write_is_not_retried_or_activated(self):
        with (
            patch.object(
                deploy,
                "dokploy",
                side_effect=[[], {}, deploy.DeploymentError("failed")],
            ) as request,
            self.assertRaises(deploy.DeploymentError),
        ):
            deploy.deploy("worker-id", IMAGE, SHA, "worker")
        self.assertEqual(request.call_count, 3)

    def test_api_errors_do_not_expose_response_or_credentials(self):
        error = HTTPError(
            "https://example.test/private",
            403,
            "private reason",
            {},
            io.BytesIO(b"secret"),
        )
        with (
            patch.object(OpenerDirector, "open", side_effect=error) as request,
            self.assertRaises(deploy.DeploymentError) as failure,
        ):
            deploy.api("https://example.test/private", {"x-api-key": "secret"})
        self.assertEqual(
            str(failure.exception), "Deployment API request failed (HTTP 403)."
        )
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["timeout"], 30)

    def test_api_rejects_redirect_without_forwarding_credentials(self):
        url = "https://deploy.example.test/api/application.one"
        headers = Message()
        headers["Location"] = "http://redirect.example.test/"
        response = addinfourl(io.BytesIO(), headers, url, 302)
        response.msg = "Found"
        with (
            patch.object(HTTPSHandler, "https_open", return_value=response) as initial,
            patch.object(HTTPHandler, "http_open") as redirected,
            self.assertRaisesRegex(deploy.DeploymentError, "HTTP 302"),
        ):
            deploy.api(url, {"X-Api-Key": "synthetic"})
        initial.assert_called_once()
        redirected.assert_not_called()

    def test_transport_error_has_no_mutation_retry(self):
        with (
            patch.object(
                OpenerDirector, "open", side_effect=URLError("private detail")
            ) as request,
            self.assertRaises(deploy.DeploymentError) as failure,
        ):
            deploy.api("https://example.test", data={"applicationId": "app"})
        self.assertNotIn("private detail", str(failure.exception))
        request.assert_called_once()

    def test_incomplete_response_is_reported_without_response_bytes(self):
        with (
            patch.object(
                OpenerDirector,
                "open",
                side_effect=IncompleteRead(b"private content", 1),
            ),
            self.assertRaisesRegex(deploy.DeploymentError, "inspect service status"),
        ):
            deploy.api("https://example.test")


class RolloutMarkerTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "github-output"
        self.output.touch()
        self.environment = patch.dict(
            os.environ,
            {
                **ENVIRONMENT,
                "GITHUB_OUTPUT": str(self.output),
                "RUNNER_TEMP": str(self.output.parent),
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.record = {
            "id": 77,
            "sha": SHA,
            "ref": SHA,
            "task": "pandect-rollout",
            "environment": "production",
        }
        self.events = []
        self.accepted_statuses = []
        self.fail_states = set()
        self.main_reads = 0
        self.current_sha = SHA
        history = patch.object(deploy, "eligible_release", side_effect=self.eligible)
        history.start()
        self.addCleanup(history.stop)

    def eligible(self, sha):
        self.events.append("history")
        self.main_reads += 1
        return self.main_reads == 1 or self.current_sha == sha

    def request(self, url, headers=None, data=None):
        self.assertEqual(headers, {"Authorization": "Bearer synthetic-github-token"})
        if url.endswith("/git/ref/heads/main"):
            self.events.append("main")
            self.main_reads += 1
            return {
                "object": {"sha": SHA if self.main_reads == 1 else self.current_sha}
            }
        if url.endswith("/deployments"):
            self.events.append("record")
            return self.record
        if url.endswith("/deployments/77/statuses"):
            self.events.append(data["state"])
            if data["state"] in self.fail_states:
                raise deploy.DeploymentError("Synthetic status outage")
            self.accepted_statuses.append(data["state"])
            return {"id": len(self.accepted_statuses), "state": data["state"]}
        self.fail("Unexpected API endpoint")

    def activate(self, application_id, image, sha, service):
        self.assertEqual(image, IMAGE)
        self.assertEqual(sha, SHA)
        self.assertEqual(self.output.read_text(), "")
        self.events.append(service)

    def test_creation_binds_actual_tested_sha_and_image_without_extra_checks(self):
        with patch.object(deploy, "api", return_value=self.record) as request:
            self.assertEqual(deploy.create_rollout(SHA, IMAGE), 77)
        self.assertEqual(
            request.call_args.args,
            (
                "https://api.github.com/repos/example/pandect/deployments",
                {"Authorization": "Bearer synthetic-github-token"},
                {
                    "ref": SHA,
                    "task": "pandect-rollout",
                    "environment": "production",
                    "auto_merge": False,
                    "required_contexts": [],
                    "production_environment": True,
                    "transient_environment": False,
                    "payload": {"ci_run_id": "123", "image": IMAGE, "version": VERSION},
                },
            ),
        )

    def test_creation_rejects_missing_or_conflicting_record_identity(self):
        for changes in (
            {"id": 0},
            {"id": True},
            {"id": "77"},
            {"sha": "b" * 40},
            {"ref": "main"},
            {"task": "deploy"},
            {"environment": "preview"},
        ):
            with (
                self.subTest(changes=changes),
                patch.object(deploy, "api", return_value={**self.record, **changes}),
                self.assertRaisesRegex(deploy.DeploymentError, "rollout identity"),
            ):
                deploy.create_rollout(SHA, IMAGE)

    def test_invalid_log_or_ci_identity_blocks_record_creation(self):
        for name, value in (
            ("GITHUB_SERVER_URL", "http://github.example.test"),
            ("GITHUB_SERVER_URL", "https://github.example.test/untrusted-path"),
            ("GITHUB_SERVER_URL", "https://token@github.example.test"),
            ("GITHUB_RUN_ID", "invalid"),
            ("CI_RUN_ID", "invalid"),
        ):
            with (
                self.subTest(name=name, value=value),
                patch.dict(os.environ, {name: value}),
                patch.object(deploy, "api") as request,
                self.assertRaises(deploy.DeploymentError),
            ):
                deploy.create_rollout(SHA, IMAGE)
            request.assert_not_called()

    def test_status_links_runner_and_preserves_older_record_states(self):
        with patch.object(
            deploy, "api", return_value={"id": 1, "state": "in_progress"}
        ) as request:
            deploy.mark_rollout(77, "in_progress")
        self.assertEqual(
            request.call_args.args[2],
            {
                "state": "in_progress",
                "auto_inactive": False,
                "log_url": "https://github.com/example/pandect/actions/runs/456",
            },
        )

    def test_marker_precedes_last_main_check_and_both_verified_services(self):
        with (
            patch.object(deploy, "api", side_effect=self.request),
            patch.object(deploy, "deploy", side_effect=self.activate),
        ):
            deploy.main()
        self.assertEqual(
            self.events,
            ["history", "record", "in_progress", "history", "worker", "web", "success"],
        )
        self.assertEqual(self.output.read_text(), "deployed=true\n")

    def test_creation_or_pending_status_failure_blocks_all_runtime_mutations(self):
        for creation_failure in (True, False):
            self.events.clear()
            self.accepted_statuses.clear()
            self.main_reads = 0
            self.fail_states = {"in_progress"}
            with (
                self.subTest(creation_failure=creation_failure),
                patch.object(
                    deploy,
                    "api",
                    side_effect=(
                        [deploy.DeploymentError("Unavailable")]
                        if creation_failure
                        else self.request
                    ),
                ),
                patch.object(deploy, "deploy") as activate,
                self.assertRaises(deploy.DeploymentError),
            ):
                deploy.main()
            activate.assert_not_called()
            self.assertEqual(self.output.read_text(), "")
            self.assertEqual(self.accepted_statuses, [])

    def test_ineligible_history_after_record_creation_marks_inactive_without_activation(
        self,
    ):
        self.current_sha = "c" * 40
        with (
            patch.object(deploy, "api", side_effect=self.request),
            patch.object(deploy, "deploy") as activate,
            redirect_stdout(io.StringIO()),
        ):
            deploy.main()
        activate.assert_not_called()
        self.assertEqual(self.accepted_statuses, ["in_progress", "inactive"])
        self.assertEqual(self.output.read_text(), "")

    def test_rollout_failure_is_recorded_and_original_error_is_preserved(self):
        failure = deploy.DeploymentError("Synthetic worker failure")
        with (
            patch.object(deploy, "api", side_effect=self.request),
            patch.object(deploy, "deploy", side_effect=failure) as activate,
            self.assertRaises(deploy.DeploymentError) as error,
        ):
            deploy.main()
        self.assertIs(error.exception, failure)
        self.assertEqual(activate.call_count, 1)
        self.assertEqual(self.accepted_statuses, ["in_progress", "failure"])
        self.assertEqual(self.output.read_text(), "")

    def test_failure_status_outage_leaves_pending_and_preserves_original_error(self):
        self.fail_states = {"failure"}
        failure = deploy.DeploymentError("Synthetic worker failure")
        with (
            patch.object(deploy, "api", side_effect=self.request),
            patch.object(deploy, "deploy", side_effect=failure),
            redirect_stdout(io.StringIO()),
            self.assertRaises(deploy.DeploymentError) as error,
        ):
            deploy.main()
        self.assertIs(error.exception, failure)
        self.assertEqual(self.accepted_statuses, ["in_progress"])
        self.assertEqual(self.output.read_text(), "")

    def test_success_status_outage_leaves_pending_and_blocks_release_output(self):
        self.fail_states = {"success"}
        with (
            patch.object(deploy, "api", side_effect=self.request),
            patch.object(deploy, "deploy", side_effect=self.activate),
            self.assertRaises(deploy.DeploymentError),
        ):
            deploy.main()
        self.assertEqual(self.events[-3:], ["worker", "web", "success"])
        self.assertEqual(self.accepted_statuses, ["in_progress"])
        self.assertEqual(self.output.read_text(), "")


class DeploymentPreflightTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "github-output"
        self.output.touch()
        environment = patch.dict(
            os.environ,
            {
                "TESTED_SHA": SHA,
                "GITHUB_REPOSITORY": "example/pandect",
                "GITHUB_API_URL": "https://api.github.com",
                "GITHUB_TOKEN": "synthetic-github-token",
                "GITHUB_OUTPUT": str(self.output),
                "RUNNER_TEMP": str(self.output.parent),
                "CI_RUN_ID": "123",
                "CI_RUN_ATTEMPT": "1",
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.run = {
            "id": 123,
            "run_number": 42,
            "run_attempt": 1,
            "name": "CI",
            "path": ".github/workflows/ci.yml",
            "head_sha": SHA,
            "head_branch": "main",
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "head_repository": {"full_name": "example/pandect"},
        }
        self.required_jobs = [
            {
                "name": name,
                "run_id": 123,
                "head_sha": SHA,
                "status": "completed",
                "conclusion": "success",
            }
            for name in (
                "Classify changes and validate source",
                "CI status",
                "Test and validate (amd64)",
                "Test and validate (arm64)",
                "Investigator journeys (chromium)",
                "Investigator journeys (webkit)",
                "Locked Python dependency audit",
            )
        ]
        self.required_jobs[1]["steps"] = [
            {
                "name": "Require successful application checks",
                "status": "completed",
                "conclusion": "success",
            }
        ]

    def jobs(self, *jobs):
        return {"total_count": len(jobs), "jobs": list(jobs)}

    def skip_application(self):
        self.required_jobs[1]["steps"][0]["name"] = (
            "Require deliberate documentation-only skips"
        )
        for job in self.required_jobs[2:]:
            job["conclusion"] = "skipped"

    def preflight(self, *responses, eligible=True):
        with (
            patch.object(deploy, "api", side_effect=responses) as request,
            patch.object(deploy, "eligible_release", return_value=eligible) as history,
            patch.object(deploy, "dokploy") as remote,
            patch.object(deploy, "deploy") as activate,
            redirect_stdout(io.StringIO()) as output,
        ):
            deploy.preflight()
        remote.assert_not_called()
        activate.assert_not_called()
        return request, output.getvalue(), history

    def test_successful_checks_enable_deployment_without_production_secrets(self):
        self.output.write_text("existing=retained\n")
        request, output, history = self.preflight(
            self.run, self.jobs(*self.required_jobs)
        )
        self.assertEqual(
            self.output.read_text(), f"existing=retained\ndeploy=true\nsource={SHA}\n"
        )
        self.assertIn("eligible", output)
        self.assertEqual(request.call_count, 2)
        history.assert_called_once_with(SHA)
        self.assertTrue(
            request.call_args.args[0].endswith("/attempts/1/jobs?per_page=100")
        )

    def test_explicit_skip_disables_deployment_and_release(self):
        self.skip_application()
        summary = self.output.with_name("github-summary")
        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
            _, output, history = self.preflight(
                self.run,
                self.jobs(
                    *self.required_jobs[:2],
                    {"name": "Test and validate", "conclusion": "skipped"},
                    {"name": "Investigator journeys", "conclusion": "skipped"},
                ),
            )
        self.assertEqual(self.output.read_text(), f"deploy=false\nsource={SHA}\n")
        self.assertIn("Skipped production deployment and release", output)
        self.assertEqual(summary.read_text(), output)
        history.assert_not_called()

    def test_older_release_does_not_publish_or_deploy(self):
        self.preflight(self.run, self.jobs(*self.required_jobs), eligible=False)
        self.assertEqual(self.output.read_text(), f"deploy=false\nsource={SHA}\n")

    def test_missing_ambiguous_or_unsuccessful_ci_decision_fails(self):
        marker = self.required_jobs[1]["steps"][0]
        for steps in (
            None,
            [],
            [None],
            [marker, marker],
            [{**marker, "name": "Unrecognized validation"}],
            [{**marker, "status": "in_progress"}],
            [{**marker, "conclusion": "skipped"}],
            [{**marker, "conclusion": "failure"}],
        ):
            jobs = [
                self.required_jobs[0],
                {**self.required_jobs[1], "steps": steps},
                *self.required_jobs[2:],
            ]
            with self.subTest(steps=steps), self.assertRaises(deploy.DeploymentError):
                self.preflight(self.run, self.jobs(*jobs))
            self.assertEqual(self.output.read_text(), "")

    def test_full_validation_requires_both_architectures_and_browsers(self):
        for missing in self.required_jobs[2:]:
            jobs = [job for job in self.required_jobs if job is not missing]
            with (
                self.subTest(missing=missing["name"]),
                self.assertRaisesRegex(deploy.DeploymentError, "required checks"),
            ):
                self.preflight(self.run, self.jobs(*jobs))
            self.assertEqual(self.output.read_text(), "")

    def test_untrusted_or_mismatching_run_cannot_enable_deployment(self):
        for change in (
            {"id": 456},
            {"run_number": "42"},
            {"run_number": 0},
            {"run_attempt": 2},
            {"name": "Different workflow"},
            {"path": ".github/workflows/unrelated.yml"},
            {"head_sha": "b" * 40},
            {"head_branch": "untrusted-branch"},
            {"event": "pull_request"},
            {"status": "in_progress"},
            {"conclusion": "failure"},
            {"head_repository": {"full_name": "unrelated/pandect"}},
            {"head_repository": None},
        ):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(deploy.DeploymentError, "not trusted"),
            ):
                self.preflight({**self.run, **change})
            self.assertEqual(self.output.read_text(), "")

    def test_invalid_run_identifiers_fail_before_api_requests(self):
        for name, value in (
            ("TESTED_SHA", "invalid"),
            ("CI_RUN_ID", "123/other"),
            ("CI_RUN_ATTEMPT", "0"),
            ("GITHUB_REPOSITORY", "invalid-repository"),
        ):
            with (
                self.subTest(name=name),
                patch.dict(os.environ, {name: value}),
                patch.object(deploy, "api") as request,
                self.assertRaises(deploy.DeploymentError),
            ):
                deploy.preflight()
            request.assert_not_called()
            self.assertEqual(self.output.read_text(), "")

    def test_missing_output_blocks_preflight_before_api_requests(self):
        del os.environ["GITHUB_OUTPUT"]
        with (
            patch.object(deploy, "api") as request,
            self.assertRaisesRegex(deploy.DeploymentError, "GITHUB_OUTPUT"),
        ):
            deploy.preflight()
        request.assert_not_called()

    def test_ambiguous_failed_mixed_or_incomplete_jobs_fail(self):
        for payload in (
            self.jobs(*self.required_jobs, self.required_jobs[0]),
            self.jobs(
                *self.required_jobs[:2],
                *[{**job, "conclusion": "skipped"} for job in self.required_jobs[2:3]],
                *self.required_jobs[3:],
            ),
            *[
                self.jobs({**self.required_jobs[0], **change}, *self.required_jobs[1:])
                for change in (
                    {"conclusion": "failure"},
                    {"conclusion": "skipped"},
                    {"status": "in_progress"},
                    {"run_id": 456},
                    {"head_sha": "b" * 40},
                )
            ],
            self.jobs(
                *self.required_jobs[:-1],
                {**self.required_jobs[-1], "conclusion": "cancelled"},
            ),
            {"total_count": 101, "jobs": self.required_jobs},
            {"total_count": 1, "jobs": [None]},
            None,
        ):
            with (
                self.subTest(payload=payload),
                self.assertRaises(deploy.DeploymentError),
            ):
                self.preflight(self.run, payload)
            self.assertEqual(self.output.read_text(), "")

    def test_api_failure_does_not_enable_deployment(self):
        with self.assertRaises(deploy.DeploymentError):
            self.preflight(self.run, deploy.DeploymentError("API unavailable"))
        self.assertEqual(self.output.read_text(), "")

    def test_partial_rerun_reuses_latest_jobs_before_triggering_attempt(self):
        os.environ["CI_RUN_ATTEMPT"] = "3"
        self.run["run_attempt"] = 3
        request, _, _ = self.preflight(
            self.run,
            self.jobs(self.required_jobs[0]),
            self.jobs(*self.required_jobs[1:]),
        )
        self.assertEqual(self.output.read_text(), f"deploy=true\nsource={SHA}\n")
        self.assertEqual(
            [call.args[0].split("/attempts/")[1] for call in request.call_args_list],
            ["3", "3/jobs?per_page=100", "2/jobs?per_page=100"],
        )

    def test_newer_attempt_cannot_replace_triggering_attempt_identity(self):
        self.run["run_attempt"] = 2
        with self.assertRaisesRegex(deploy.DeploymentError, "not trusted"):
            self.preflight(self.run)
        self.assertEqual(self.output.read_text(), "")

    def test_newer_explicit_skip_takes_precedence_over_older_success(self):
        os.environ["CI_RUN_ATTEMPT"] = "2"
        self.run["run_attempt"] = 2
        self.skip_application()
        request, _, _ = self.preflight(self.run, self.jobs(*self.required_jobs))
        self.assertEqual(self.output.read_text(), f"deploy=false\nsource={SHA}\n")
        self.assertEqual(request.call_count, 2)

    def test_missing_job_history_is_bounded(self):
        os.environ["CI_RUN_ATTEMPT"] = "100"
        self.run["run_attempt"] = 100
        with (
            patch.object(
                deploy, "api", side_effect=[self.run] + [self.jobs()] * 20
            ) as request,
            self.assertRaisesRegex(deploy.DeploymentError, "required checks"),
        ):
            deploy.preflight()
        self.assertEqual(request.call_count, 21)
        self.assertEqual(self.output.read_text(), "")


class ReleaseHistoryTests(TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, ENVIRONMENT, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.previous_sha = "c" * 40
        self.record = {
            "id": 77,
            "task": "pandect-rollout",
            "environment": "production",
            "sha": self.previous_sha,
            "ref": self.previous_sha,
            "payload": {"version": "v1.41"},
        }

    def eligible(self, *responses):
        with (
            patch.object(deploy, "api", side_effect=responses) as request,
            redirect_stdout(io.StringIO()),
        ):
            result = deploy.eligible_release(SHA)
        return result, request

    def test_queued_ancestor_of_main_can_start_the_first_release(self):
        result, request = self.eligible({"status": "ahead"}, [])
        self.assertTrue(result)
        self.assertIn(f"/compare/{SHA}...main", request.call_args_list[0].args[0])

    def test_descendant_commit_follows_previous_attempt(self):
        result, request = self.eligible(
            {"status": "ahead"}, [self.record], {"status": "ahead"}
        )
        self.assertTrue(result)
        self.assertTrue(
            request.call_args.args[0].endswith(f"/compare/{self.previous_sha}...{SHA}")
        )

    def test_unconsumed_version_can_be_reused_by_a_descendant_commit(self):
        self.record["payload"]["version"] = VERSION
        result, _ = self.eligible(
            {"status": "identical"}, [self.record], {"status": "ahead"}
        )
        self.assertTrue(result)

    def test_version_labels_do_not_override_forward_source_history(self):
        self.record["payload"]["version"] = "v1.43"
        result, _ = self.eligible(
            {"status": "identical"}, [self.record], {"status": "ahead"}
        )
        self.assertTrue(result)

    def test_same_source_attempt_can_be_retried(self):
        self.record.update(sha=SHA, ref=SHA)
        result, request = self.eligible({"status": "identical"}, [self.record])
        self.assertTrue(result)
        self.assertEqual(request.call_count, 2)

    def test_historical_versionless_record_requires_forward_commit_history(self):
        self.record["payload"] = {}
        result, _ = self.eligible(
            {"status": "ahead"}, [self.record], {"status": "ahead"}
        )
        self.assertTrue(result)
        result, _ = self.eligible(
            {"status": "ahead"}, [self.record], {"status": "behind"}
        )
        self.assertFalse(result)

    def test_force_pushed_or_reversed_main_history_is_skipped(self):
        for status in ("diverged", "behind"):
            with self.subTest(status=status):
                result, request = self.eligible({"status": status})
            self.assertFalse(result)
            self.assertEqual(request.call_count, 1)

    def test_reused_version_cannot_reverse_or_diverge_from_previous_attempt(self):
        for status in ("diverged", "behind"):
            with self.subTest(status=status):
                result, _ = self.eligible(
                    {"status": "ahead"}, [self.record], {"status": status}
                )
            self.assertFalse(result)

    def test_ambiguous_or_malformed_history_fails_closed(self):
        for history in (
            None,
            {},
            [self.record, self.record],
            [None],
            *[
                [{**self.record, **change}]
                for change in (
                    {"id": 0},
                    {"id": True},
                    {"sha": "invalid"},
                    {"ref": "main"},
                    {"task": "other"},
                    {"environment": "other"},
                    {"payload": None},
                )
            ],
        ):
            with (
                self.subTest(history=history),
                self.assertRaises(deploy.DeploymentError),
            ):
                self.eligible({"status": "ahead"}, history)

    def test_missing_or_unknown_comparisons_fail_closed(self):
        for comparison in (None, {}, {"status": "unknown"}):
            with (
                self.subTest(comparison=comparison),
                self.assertRaises(deploy.DeploymentError),
            ):
                self.eligible(comparison)
            with (
                self.subTest(comparison=comparison),
                self.assertRaises(deploy.DeploymentError),
            ):
                self.eligible({"status": "ahead"}, [self.record], comparison)


class ReleaseReadinessTests(TestCase):
    def setUp(self):
        self.clock = patch.object(
            deploy.time, "monotonic", side_effect=itertools.count(0, 5)
        )
        self.sleep = patch.object(deploy.time, "sleep")
        self.clock.start()
        self.sleep.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.sleep.stop)

    def request(self, path, data=None, **query):
        if path == "deployment.all":
            return [{"deploymentId": "new", "title": "release", "status": "done"}]
        if path == "application.one":
            return {"appName": "app"}
        if path == "application.readLogs":
            return f"2026-11-06T00:00:00Z PANDECT_WORKER_PREPARED {SHA}\n"
        self.fail(f"Unexpected API path: {path}")

    def wait(self, worker=False):
        deploy.wait_ready("app", IMAGE, SHA, "release", {"old"}, 0, worker)

    def test_current_image_and_public_commit_must_remain_ready(self):
        with (
            patch.object(deploy, "dokploy", side_effect=self.request),
            patch.object(deploy, "current_task", return_value="task") as task,
            patch.object(deploy, "api", return_value={"status": "ok", "commit": SHA}),
        ):
            self.wait()
        self.assertGreaterEqual(task.call_count, 2)

    def test_worker_requires_release_marker_and_stable_task(self):
        with (
            patch.object(deploy, "dokploy", side_effect=self.request),
            patch.object(deploy, "current_task", return_value="task") as task,
            patch.object(deploy, "api") as public,
        ):
            self.wait(worker=True)
        self.assertGreaterEqual(task.call_count, 2)
        public.assert_not_called()

    def test_replaced_task_cannot_pass_stability_check(self):
        with (
            patch.object(deploy, "dokploy", side_effect=self.request),
            patch.object(deploy, "current_task", side_effect=["first", "second"]),
            patch.object(deploy, "api", return_value={"status": "ok", "commit": SHA}),
            self.assertRaisesRegex(deploy.DeploymentError, "replaced"),
        ):
            self.wait()

    def test_healthy_old_web_commit_cannot_pass_readiness(self):
        with (
            patch.object(deploy, "dokploy", side_effect=self.request),
            patch.object(deploy, "current_task", return_value="task"),
            patch.object(
                deploy, "api", return_value={"status": "ok", "commit": "older"}
            ),
            self.assertRaisesRegex(deploy.DeploymentError, "public web health"),
        ):
            self.wait()

    def test_public_ingress_can_become_ready_after_initial_failure(self):
        responses = itertools.chain(
            [deploy.DeploymentError("not ready"), None, ["unexpected response"]],
            itertools.repeat({"status": "ok", "commit": SHA}),
        )
        with (
            patch.object(deploy, "dokploy", side_effect=self.request),
            patch.object(deploy, "current_task", return_value="task"),
            patch.object(deploy, "api", side_effect=responses) as health,
        ):
            self.wait()
        self.assertGreaterEqual(health.call_count, 4)

    def test_worker_without_current_release_marker_times_out(self):
        def request(path, data=None, **query):
            if path == "application.readLogs":
                return "PANDECT_WORKER_PREPARED older-commit\n"
            return self.request(path, data, **query)

        with (
            patch.object(deploy, "dokploy", side_effect=request),
            patch.object(deploy, "current_task", return_value="task"),
            self.assertRaisesRegex(deploy.DeploymentError, "worker startup marker"),
        ):
            self.wait(worker=True)

    def test_failed_deployment_cannot_use_an_old_healthy_task(self):
        with (
            patch.object(
                deploy,
                "dokploy",
                return_value=[
                    {"deploymentId": "new", "title": "release", "status": "error"}
                ],
            ),
            patch.object(deploy, "current_task") as task,
            self.assertRaisesRegex(deploy.DeploymentError, "unsuccessful"),
        ):
            self.wait()
        task.assert_not_called()

    def test_task_inspection_rejects_a_different_running_digest(self):
        with patch.object(
            deploy,
            "dokploy",
            side_effect=[
                [
                    {
                        "ID": "task",
                        "DesiredState": "running",
                        "CurrentState": "Running 5s",
                    }
                ],
                {"Spec": {"ContainerSpec": {"Image": "older-image"}}},
            ],
        ):
            self.assertIsNone(deploy.current_task({"appName": "app"}, IMAGE))

    def test_native_capitalized_running_state_proves_the_expected_task(self):
        with patch.object(
            deploy,
            "dokploy",
            side_effect=[
                [
                    {
                        "ID": "task",
                        "DesiredState": "Running",
                        "CurrentState": "Running 5s",
                    }
                ],
                {"Spec": {"ContainerSpec": {"Image": IMAGE}}},
            ],
        ):
            self.assertEqual(deploy.current_task({"appName": "app"}, IMAGE), "task")

    def test_live_dokploy_mapped_task_fields_prove_the_expected_image(self):
        task_id = "synthetic-native-task"
        with patch.object(
            deploy,
            "dokploy",
            side_effect=[
                [
                    {
                        "containerId": task_id,
                        "name": "synthetic-app.1",
                        "state": "running",
                        "currentState": "Running 11 minutes ago",
                        "node": "synthetic-node",
                        "error": "Error:",
                    }
                ],
                {"Spec": {"ContainerSpec": {"Image": IMAGE}}},
            ],
        ) as request:
            self.assertEqual(deploy.current_task({"appName": "app"}, IMAGE), task_id)
        self.assertEqual(
            request.call_args_list[-1],
            call("docker.getConfig", containerId=task_id),
        )

    def test_old_running_task_blocks_new_singleton_readiness(self):
        with patch.object(
            deploy,
            "dokploy",
            return_value=[
                {"ID": "new", "DesiredState": "Running", "CurrentState": "Running 5s"},
                {
                    "ID": "old",
                    "DesiredState": "Shutdown",
                    "CurrentState": "Running 10s",
                },
            ],
        ) as request:
            self.assertIsNone(deploy.current_task({"appName": "app"}, IMAGE))
        request.assert_called_once()
