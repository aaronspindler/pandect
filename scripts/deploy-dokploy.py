"""Deploy tested GHCR digests through Dokploy's native application provider."""

import json
import os
import re
import sys
import time
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


class DeploymentError(Exception):
    pass


def required(name):
    value = os.environ.get(name, "")
    if not value:
        raise DeploymentError(f"Set {name} in the production environment.")
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
        with urlopen(request, timeout=30) as response:
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


def dokploy(path, data=None, **query):
    url = f"{required('DOKPLOY_URL').rstrip('/')}/api/{path}"
    if query:
        url += "?" + urlencode(query)
    return api(url, {"x-api-key": required("DOKPLOY_API_KEY")}, data)


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
    services = []
    for service in ("worker", "web"):
        image = required(f"{service.upper()}_IMAGE")
        expected = f"ghcr.io/{repository.lower()}-{service}@sha256:"
        if not image.startswith(expected) or not re.fullmatch(
            r"[0-9a-f]{64}", image.removeprefix(expected)
        ):
            raise DeploymentError(
                "Image must identify this repository by immutable digest."
            )
        services.append(
            (required(f"DOKPLOY_{service.upper()}_APPLICATION_ID"), image, service)
        )
    if services[0][0] == services[1][0]:
        raise DeploymentError("Web and worker must use separate Dokploy applications.")
    for application_id, image, service in services:
        deploy(application_id, image, sha, service)


if __name__ == "__main__":
    try:
        main()
    except (DeploymentError, KeyError, TypeError) as error:
        detail = (
            str(error)
            if isinstance(error, DeploymentError)
            else "Unexpected API response."
        )
        print(f"Deployment failed: {detail}", file=sys.stderr)
        sys.exit(1)
