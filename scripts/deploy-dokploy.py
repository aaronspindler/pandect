"""Deploy one tested GHCR image to Dokploy's separate web and worker applications."""

import json
import os
import re
import sys
import time
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class DeploymentError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def required(name):
    value = os.environ.get(name, "")
    if not value:
        raise DeploymentError(f"Set {name} in the workflow environment.")
    return value


def api(url, headers=None, data=None):
    request = Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "pandect-cd/1.0",
            **(headers or {}),
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise DeploymentError("Deployment API response exceeded its size limit.")
        return json.loads(body) if body else None
    except HTTPError as error:
        status = error.code
        error.close()
        raise DeploymentError(
            f"Deployment API request failed (HTTP {status})."
        ) from None
    # The CD runner uses Python 3.12 rather than the application's runtime.
    except (OSError, HTTPException, ValueError):  # noqa: UP039  # fmt: skip
        raise DeploymentError(
            "Deployment API request failed; inspect service status."
        ) from None


def preflight():
    """Check the triggering CI attempt without accessing production credentials."""
    sha = required("TESTED_SHA")
    repository = required("GITHUB_REPOSITORY")
    run_id = required("CI_RUN_ID")
    attempt = required("CI_RUN_ATTEMPT")
    if (
        not re.fullmatch(r"[0-9a-f]{40}", sha)
        or not re.fullmatch(r"[1-9][0-9]{0,19}", run_id)
        or not re.fullmatch(r"[1-9][0-9]{0,19}", attempt)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
    ):
        raise DeploymentError("Invalid CI run identity.")
    output_path = required("GITHUB_OUTPUT")
    attempt = int(attempt)
    base = f"{required('GITHUB_API_URL')}/repos/{repository}/actions/runs/{run_id}"
    headers = {"Authorization": f"Bearer {required('GITHUB_TOKEN')}"}
    run = api(f"{base}/attempts/{attempt}", headers)
    if (
        not isinstance(run, dict)
        or run.get("id") != int(run_id)
        or run.get("run_attempt") != attempt
        or run.get("name") != "CI"
        or str(run.get("path", "")).split("@", 1)[0] != ".github/workflows/ci.yml"
        or run.get("head_sha") != sha
        or run.get("head_branch") != "main"
        or run.get("event") != "push"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or not isinstance(run.get("head_repository"), dict)
        or run["head_repository"].get("full_name") != repository
    ):
        raise DeploymentError("The triggering run is not trusted successful main CI.")

    # Partial reruns may reuse a successful job from an earlier attempt. Inspect
    # exact attempts so a later rerun cannot change this event's decision.
    image_job = None
    for number in range(attempt, max(0, attempt - 20), -1):
        payload = api(f"{base}/attempts/{number}/jobs?per_page=100", headers)
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("jobs"), list)
            or not all(isinstance(job, dict) for job in payload["jobs"])
            or payload.get("total_count") != len(payload["jobs"])
            or len(payload["jobs"]) > 100
        ):
            raise DeploymentError("The CI job list is incomplete or invalid.")
        matches = [
            job
            for job in payload["jobs"]
            if job.get("name") == "Publish production image"
        ]
        if len(matches) > 1:
            raise DeploymentError("The CI image publication job is ambiguous.")
        if matches:
            image_job = matches[0]
            break
    if (
        image_job is None
        or image_job.get("run_id") != int(run_id)
        or image_job.get("head_sha") != sha
        or image_job.get("status") != "completed"
        or image_job.get("conclusion") not in {"success", "skipped"}
    ):
        raise DeploymentError(
            "CI did not report a completed image publication decision."
        )
    eligible = image_job["conclusion"] == "success"
    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"deploy={'true' if eligible else 'false'}\n")
    message = (
        "CI published the tested image; production deployment is eligible."
        if eligible
        else (
            "Skipped production deployment and release: "
            "CI explicitly skipped image publication."
        )
    )
    print(message)
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(message + "\n")


def dokploy(path, data=None, **query):
    url = f"{required('DOKPLOY_URL').rstrip('/')}/api/{path}"
    if query:
        url += "?" + urlencode(query)
    return api(url, {"x-api-key": required("DOKPLOY_API_KEY")}, data)


def rollout_log_url():
    origin = urlsplit(required("GITHUB_SERVER_URL"))
    run_id = required("GITHUB_RUN_ID")
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.path
        or origin.query
        or origin.fragment
        or not re.fullmatch(r"[1-9][0-9]{0,19}", run_id)
    ):
        raise DeploymentError("Invalid GitHub rollout log identity.")
    return f"{origin.geturl()}/{required('GITHUB_REPOSITORY')}/actions/runs/{run_id}"


def create_rollout(sha, image):
    """Record the exact image revision before changing either runtime service."""
    rollout_log_url()
    repository = required("GITHUB_REPOSITORY")
    ci_run_id = required("CI_RUN_ID")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", ci_run_id):
        raise DeploymentError("Invalid CI run identity.")
    result = api(
        f"{required('GITHUB_API_URL')}/repos/{repository}/deployments",
        {"Authorization": f"Bearer {required('GITHUB_TOKEN')}"},
        {
            "ref": sha,
            "task": "pandect-rollout",
            "environment": "production",
            "auto_merge": False,
            "required_contexts": [],
            "production_environment": True,
            "transient_environment": False,
            "payload": {"ci_run_id": ci_run_id, "image": image},
        },
    )
    if (
        not isinstance(result, dict)
        or type(result.get("id")) is not int
        or result["id"] <= 0
        or result.get("sha") != sha
        or result.get("ref") != sha
        or result.get("task") != "pandect-rollout"
        or result.get("environment") != "production"
    ):
        raise DeploymentError("GitHub did not confirm the exact rollout identity.")
    return result["id"]


def mark_rollout(deployment_id, state):
    repository = required("GITHUB_REPOSITORY")
    result = api(
        f"{required('GITHUB_API_URL')}/repos/{repository}"
        f"/deployments/{deployment_id}/statuses",
        {"Authorization": f"Bearer {required('GITHUB_TOKEN')}"},
        {"state": state, "auto_inactive": False, "log_url": rollout_log_url()},
    )
    if (
        not isinstance(result, dict)
        or type(result.get("id")) is not int
        or result["id"] <= 0
        or result.get("state") != state
    ):
        raise DeploymentError("GitHub did not confirm the rollout status.")


def records(payload):
    if isinstance(payload, dict):
        payload = payload.get("data")
    if not isinstance(payload, list) or not all(isinstance(x, dict) for x in payload):
        raise DeploymentError("Unexpected Dokploy collection response.")
    return payload


def task_field(task, raw_name, mapped_name):
    return str(task.get(raw_name) or task.get(mapped_name) or "").strip()


def current_task(application, image):
    query = {"appName": application["appName"]}
    server = (
        {"serverId": application["serverId"]} if application.get("serverId") else {}
    )
    tasks = records(dokploy("docker.getServiceContainersByAppName", **query, **server))
    active = [
        task
        for task in tasks
        if task_field(task, "DesiredState", "state").lower() == "running"
    ]
    if any(
        task_field(task, "DesiredState", "state").lower() != "running"
        and task_field(task, "CurrentState", "currentState")
        .lower()
        .startswith("running")
        for task in tasks
    ):
        return None
    if len(active) != 1 or not task_field(
        active[0], "CurrentState", "currentState"
    ).lower().startswith("running"):
        return None
    task_id = task_field(active[0], "ID", "containerId")
    if not task_id:
        return None
    inspection = dokploy("docker.getConfig", containerId=task_id, **server)
    if inspection.get("Spec", {}).get("ContainerSpec", {}).get("Image") != image:
        return None
    return task_id


def wait_ready(application_id, image, sha, title, previous_ids, started_at, worker):
    deadline = time.monotonic() + 12 * 60
    stable_task = None
    stable_since = None
    waiting_for = "the matching Dokploy deployment to finish"
    while time.monotonic() < deadline:
        deployments = records(dokploy("deployment.all", applicationId=application_id))
        deployment = next(
            (
                item
                for item in deployments
                if item["deploymentId"] not in previous_ids
                and item.get("title") == title
            ),
            None,
        )
        if deployment and deployment["status"] not in {"running", "queued", "done"}:
            raise DeploymentError("Dokploy reported an unsuccessful deployment.")
        waiting_for = "the matching Dokploy deployment to finish"
        task = None
        if deployment and deployment["status"] == "done":
            waiting_for = "one running Swarm task on the expected image"
            application = dokploy("application.one", applicationId=application_id)
            task = current_task(application, image)
        ready = False
        if task and worker:
            waiting_for = "the worker startup marker for the expected commit"
            logs = dokploy(
                "application.readLogs",
                applicationId=application_id,
                tail="1000",
                since=f"{int(time.monotonic() - started_at) + 1}s",
            )
            if isinstance(logs, dict):
                logs = logs.get("logs", "")
            marker = f"PANDECT_WORKER_PREPARED {sha}"
            ready = isinstance(logs, str) and any(
                re.sub(
                    r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?"
                    r"(?:Z|[+-]\d\d:\d\d) ",
                    "",
                    line,
                )
                == marker
                for line in logs.splitlines()
            )
        elif task:
            waiting_for = "public web health on the expected commit"
            try:
                health = api("https://openpandect.com/health/")
            except DeploymentError:
                health = {}
            ready = (
                isinstance(health, dict)
                and health.get("status") == "ok"
                and health.get("commit") == sha
            )
        if ready:
            waiting_for = "the 60-second readiness window"
            if stable_task is None:
                stable_task, stable_since = task, time.monotonic()
            elif task != stable_task:
                raise DeploymentError(
                    "The application replaced its task during readiness."
                )
            elif time.monotonic() - stable_since >= 60:
                return
        elif stable_task is not None:
            raise DeploymentError(
                "The application lost readiness during its stability check."
            )
        time.sleep(5)
    raise DeploymentError(
        f"Timed out waiting for {waiting_for}. The remote deployment may still "
        "be running; inspect it before retrying."
    )


def deploy(application_id, image, sha, service):
    print(f"Deploying {service} to Dokploy.", flush=True)
    previous = records(dokploy("deployment.all", applicationId=application_id))
    if any(item["status"] in {"running", "queued"} for item in previous):
        raise DeploymentError("Another deployment is active; wait before retrying.")
    dokploy(
        "application.update",
        {
            "applicationId": application_id,
            "replicas": 1,
            "command": "sh" if service == "worker" else None,
            "args": ["/app/scripts/docker-entrypoint-worker.sh"]
            if service == "worker"
            else None,
            "updateConfigSwarm": {
                "Parallelism": 1,
                "Order": "stop-first" if service == "worker" else "start-first",
            },
        },
    )
    dokploy(
        "application.saveDockerProvider",
        {
            "applicationId": application_id,
            "dockerImage": image,
            "registryUrl": "ghcr.io",
            "username": required("GITHUB_REPOSITORY").split("/", 1)[0],
            "password": required("DOKPLOY_REGISTRY_PASSWORD"),
        },
    )
    title = f"Deploy {service} CI {required('CI_RUN_ID')} {sha}"
    started_at = time.monotonic()
    dokploy(
        "application.deploy",
        {"applicationId": application_id, "title": title, "description": image},
    )
    wait_ready(
        application_id,
        image,
        sha,
        title,
        {item["deploymentId"] for item in previous},
        started_at,
        service == "worker",
    )
    print(f"Verified {service} running {image} for {sha}.")


def main():
    sha = required("TESTED_SHA")
    repository = required("GITHUB_REPOSITORY")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise DeploymentError("Invalid CI commit identity.")
    github_url = f"{required('GITHUB_API_URL')}/repos/{repository}/git/ref/heads/main"
    current = api(github_url, {"Authorization": f"Bearer {required('GITHUB_TOKEN')}"})
    if current["object"]["sha"] != sha:
        print("Skipped stale CI run: main has advanced beyond the tested commit.")
        return
    output_path = required("GITHUB_OUTPUT")
    if os.environ.get("PANDECT_PRODUCTION_DATABASE_BASELINE") != "domain-apps-v1":
        raise DeploymentError(
            "Production requires a fresh PostgreSQL database and empty queue for "
            "the domain-app migration baseline. Configure both Dokploy applications "
            "for it, then set the GitHub production environment variable "
            "PANDECT_PRODUCTION_DATABASE_BASELINE=domain-apps-v1. "
            "See docs/operations.md."
        )
    origin = urlsplit(required("DOKPLOY_URL").rstrip("/"))
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.path
        or origin.query
        or origin.fragment
    ):
        raise DeploymentError("DOKPLOY_URL must be an HTTPS origin.")
    required("DOKPLOY_API_KEY")
    required("DOKPLOY_REGISTRY_PASSWORD")
    required("CI_RUN_ID")
    image = required("PANDECT_IMAGE")
    expected = f"ghcr.io/{repository.lower()}@sha256:"
    if not image.startswith(expected) or not re.fullmatch(
        r"[0-9a-f]{64}", image.removeprefix(expected)
    ):
        raise DeploymentError(
            "Image must identify this repository by immutable digest."
        )
    services = []
    for service in ("worker", "web"):
        services.append(
            (required(f"DOKPLOY_{service.upper()}_APPLICATION_ID"), service)
        )
    if services[0][0] == services[1][0]:
        raise DeploymentError("Web and worker must use separate Dokploy applications.")
    deployment_id = create_rollout(sha, image)
    mark_rollout(deployment_id, "in_progress")
    try:
        # Publish the pending marker before this last main check: a docs-only CI
        # decision made just before marker creation cannot race an old rollout.
        current = api(
            github_url, {"Authorization": f"Bearer {required('GITHUB_TOKEN')}"}
        )
        if current["object"]["sha"] != sha:
            mark_rollout(deployment_id, "inactive")
            print("Skipped stale CI run after recording the pending rollout.")
            return
        for application_id, service in services:
            deploy(application_id, image, sha, service)
    except Exception:
        try:
            mark_rollout(deployment_id, "failure")
        except Exception:
            print("Could not mark the failed rollout; it remains unverified.")
        raise
    # If this status write fails, leave the marker unverified and block release
    # publication even though both services may already be running the image.
    mark_rollout(deployment_id, "success")
    with open(output_path, "a", encoding="utf-8") as output:
        output.write("deployed=true\n")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--preflight"]:
            preflight()
        elif sys.argv[1:]:
            raise DeploymentError("Usage: deploy-dokploy.py [--preflight]")
        else:
            main()
    except (DeploymentError, KeyError, TypeError) as error:
        detail = (
            str(error)
            if isinstance(error, DeploymentError)
            else "Unexpected API response."
        )
        print(f"Deployment failed: {detail}", file=sys.stderr)
        sys.exit(1)
