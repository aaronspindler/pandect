import ast
import importlib.util
import io
import itertools
import os
from contextlib import redirect_stdout
from pathlib import Path
from unittest import TestCase
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-dokploy.py"
SPEC = importlib.util.spec_from_file_location("deploy_dokploy", SCRIPT)
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)

SHA = "a" * 40
IMAGE = "ghcr.io/example/pandect-worker@sha256:" + "b" * 64
ENVIRONMENT = {
    "TESTED_SHA": SHA,
    "GITHUB_REPOSITORY": "example/pandect",
    "GITHUB_API_URL": "https://api.github.com",
    "GITHUB_TOKEN": "synthetic-github-token",
    "CI_RUN_ID": "123",
    "DOKPLOY_URL": "https://deploy.example.test",
    "DOKPLOY_API_KEY": "synthetic-dokploy-key",
    "DOKPLOY_REGISTRY_PASSWORD": "synthetic-registry-password",
    "DOKPLOY_WEB_APPLICATION_ID": "web-id",
    "DOKPLOY_WORKER_APPLICATION_ID": "worker-id",
    "WORKER_IMAGE": IMAGE,
    "WEB_IMAGE": IMAGE.replace("-worker@", "-web@"),
}


class DeploymentCommandTests(TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, ENVIRONMENT, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_helper_parses_on_the_cd_runners_python_312(self):
        ast.parse(SCRIPT.read_text(), feature_version=(3, 12))

    def test_stale_ci_cannot_mutate_dokploy(self):
        with (
            patch.object(deploy, "api", return_value={"object": {"sha": "c" * 40}}),
            patch.object(deploy, "deploy") as activate,
            redirect_stdout(io.StringIO()) as output,
        ):
            deploy.main()
        activate.assert_not_called()
        self.assertIn("Skipped stale", output.getvalue())

    def test_worker_is_verified_before_web_activation(self):
        with (
            patch.object(deploy, "api", return_value={"object": {"sha": SHA}}),
            patch.object(deploy, "deploy") as activate,
        ):
            deploy.main()
        self.assertEqual(
            activate.call_args_list,
            [
                call("worker-id", ENVIRONMENT["WORKER_IMAGE"], SHA, "worker"),
                call("web-id", ENVIRONMENT["WEB_IMAGE"], SHA, "web"),
            ],
        )

    def test_invalid_web_image_is_rejected_before_worker_mutation(self):
        os.environ["WEB_IMAGE"] = "ghcr.io/example/pandect-web:latest"
        with (
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
            set(update), {"applicationId", "replicas", "updateConfigSwarm"}
        )
        self.assertEqual(update["replicas"], 1)
        self.assertEqual(update["updateConfigSwarm"]["Order"], "stop-first")
        provider = request.call_args_list[2].args[1]
        self.assertEqual(provider["dockerImage"], IMAGE)
        self.assertEqual(provider["registryUrl"], "ghcr.io")
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
            deploy.deploy("web-id", ENVIRONMENT["WEB_IMAGE"], SHA, "web")
        update = request.call_args_list[1].args[1]
        self.assertEqual(update["replicas"], 1)
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
            patch.object(deploy, "urlopen", side_effect=error) as request,
            self.assertRaises(deploy.DeploymentError) as failure,
        ):
            deploy.api("https://example.test/private", {"x-api-key": "secret"})
        self.assertEqual(
            str(failure.exception), "Deployment API request failed (HTTP 403)."
        )
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["timeout"], 30)

    def test_transport_error_has_no_mutation_retry(self):
        with (
            patch.object(
                deploy, "urlopen", side_effect=URLError("private detail")
            ) as request,
            self.assertRaises(deploy.DeploymentError) as failure,
        ):
            deploy.api("https://example.test", data={"applicationId": "app"})
        self.assertNotIn("private detail", str(failure.exception))
        request.assert_called_once()


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
            self.assertRaisesRegex(deploy.DeploymentError, "Timed out"),
        ):
            self.wait()

    def test_public_ingress_can_become_ready_after_initial_failure(self):
        responses = itertools.chain(
            [deploy.DeploymentError("not ready")],
            itertools.repeat({"status": "ok", "commit": SHA}),
        )
        with (
            patch.object(deploy, "dokploy", side_effect=self.request),
            patch.object(deploy, "current_task", return_value="task"),
            patch.object(deploy, "api", side_effect=responses) as health,
        ):
            self.wait()
        self.assertGreaterEqual(health.call_count, 3)

    def test_worker_without_current_release_marker_times_out(self):
        def request(path, data=None, **query):
            if path == "application.readLogs":
                return "PANDECT_WORKER_PREPARED older-commit\n"
            return self.request(path, data, **query)

        with (
            patch.object(deploy, "dokploy", side_effect=request),
            patch.object(deploy, "current_task", return_value="task"),
            self.assertRaisesRegex(deploy.DeploymentError, "Timed out"),
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
