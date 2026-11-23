"""Deploy one tested GHCR image to Dokploy's worker and web applications."""

import json
import os
import re
import sys
import time
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
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


def runner_file(name):
    path = os.path.realpath(required(name))
    root = os.path.realpath(required("RUNNER_TEMP"))
    if not path.startswith(root + os.sep):
        raise DeploymentError(f"{name} must stay inside RUNNER_TEMP.")
    return path


def github_base():
    if required("GITHUB_API_URL").rstrip("/") != "https://api.github.com":
        raise DeploymentError("Only the public GitHub API is supported.")
    repository = required("GITHUB_REPOSITORY")
    if not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}", repository
    ):
        raise DeploymentError("Invalid GitHub repository identity.")
    return f"https://api.github.com/repos/{quote(repository, safe='/')}"


def release_version():
    version = required("PANDECT_VERSION")
    if not re.fullmatch(r"v(0|[1-9][0-9]{0,19})\.(0|[1-9][0-9]{0,19})", version):
        raise DeploymentError("Invalid deployment version; expected vN.N.")
    return version


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
    ):
        raise DeploymentError("Invalid CI run identity.")
    output_path = runner_file("GITHUB_OUTPUT")
    attempt = int(attempt)
    base = f"{github_base()}/actions/runs/{run_id}"
    headers = {"Authorization": f"Bearer {required('GITHUB_TOKEN')}"}
    run = api(f"{base}/attempts/{attempt}", headers)
    if (
        not isinstance(run, dict)
        or run.get("id") != int(run_id)
        or run.get("run_attempt") != attempt
        or type(run.get("run_number")) is not int
        or run["run_number"] <= 0
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
    names = {
        "Classify changes and validate source",
        "CI status",
        "Test and validate (amd64)",
        "Test and validate (arm64)",
        "Investigator journeys (chromium)",
        "Investigator journeys (webkit)",
        "Locked Python dependency audit",
    }
    baseline = {"Classify changes and validate source", "CI status"}
    markers = {
        "Require successful application checks": True,
        "Require deliberate documentation-only skips": False,
    }
    jobs = {}
    full = None
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
        for name in names - jobs.keys():
            matches = [job for job in payload["jobs"] if job.get("name") == name]
            if len(matches) > 1:
                raise DeploymentError("A required CI job is ambiguous.")
            if matches:
                jobs[name] = matches[0]
        if full is None and "CI status" in jobs:
            steps = jobs["CI status"].get("steps")
            if not isinstance(steps, list) or not all(
                isinstance(step, dict) for step in steps
            ):
                raise DeploymentError("CI did not report its validation decision.")
            decisions = [step for step in steps if step.get("name") in markers]
            if (
                len(decisions) != 1
                or decisions[0].get("status") != "completed"
                or decisions[0].get("conclusion") != "success"
            ):
                raise DeploymentError("CI did not confirm its validation decision.")
            full = markers[decisions[0]["name"]]
        required_names = names if full else baseline
        if full is not None and required_names <= jobs.keys():
            break
    if (
        full is None
        or not required_names <= jobs.keys()
        or any(
            jobs[name].get("run_id") != int(run_id)
            or jobs[name].get("head_sha") != sha
            or jobs[name].get("status") != "completed"
            or jobs[name].get("conclusion") != "success"
            for name in required_names
        )
    ):
        raise DeploymentError("CI did not report all completed required checks.")
    eligible = full and eligible_release(sha)
    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"deploy={'true' if eligible else 'false'}\nsource={sha}\n")
    message = (
        "CI checks passed; production publication and deployment are eligible."
        if eligible
        else (
            "Skipped production deployment and release: "
            "CI explicitly skipped application checks."
            if not full
            else "Skipped production deployment and release: history has advanced."
        )
    )
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(runner_file("GITHUB_STEP_SUMMARY"), "a", encoding="utf-8") as summary:
            summary.write(message + "\n")


def eligible_release(sha):
    """Accept only forward commits on the current main history."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise DeploymentError("Invalid CI commit identity.")
    base = github_base()
    headers = {"Authorization": f"Bearer {required('GITHUB_TOKEN')}"}
    comparison = api(f"{base}/compare/{sha}...main", headers)
    if not isinstance(comparison, dict) or comparison.get("status") not in {
        "ahead",
        "identical",
        "behind",
        "diverged",
    }:
        raise DeploymentError("GitHub did not confirm the main commit history.")
    if comparison["status"] not in {"ahead", "identical"}:
        print("Skipped CI commit outside current main history.")
        return False
    latest = api(
        f"{base}/deployments?environment=production&task=pandect-rollout&per_page=1",
        headers,
    )
    if latest == []:
        return True
    if (
        not isinstance(latest, list)
        or len(latest) != 1
        or not isinstance(latest[0], dict)
    ):
        raise DeploymentError("GitHub did not return an unambiguous rollout history.")
    record = latest[0]
    previous_sha, payload = record.get("sha"), record.get("payload")
    if (
        type(record.get("id")) is not int
        or record["id"] <= 0
        or record.get("task") != "pandect-rollout"
        or record.get("environment") != "production"
        or not isinstance(previous_sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}", previous_sha)
        or record.get("ref") != previous_sha
        or not isinstance(payload, dict)
    ):
        raise DeploymentError("The latest rollout identity is invalid.")
    if previous_sha != sha:
        comparison = api(f"{base}/compare/{previous_sha}...{sha}", headers)
        if not isinstance(comparison, dict) or comparison.get("status") not in {
            "ahead",
            "identical",
            "behind",
            "diverged",
        }:
            raise DeploymentError("GitHub did not confirm the rollout commit history.")
        if comparison["status"] not in {"ahead", "identical"}:
            print(
                "Skipped CI commit that does not follow the latest production attempt."
            )
            return False
    return True


def dokploy_origin():
    value = required("DOKPLOY_URL").rstrip("/")
    if not re.fullmatch(r"https://[A-Za-z0-9.:\[\]-]{1,260}", value):
        raise DeploymentError("DOKPLOY_URL must be an HTTPS origin.")
    origin = urlsplit(value)
    if not origin.hostname or origin.username or origin.password or origin.path:
        raise DeploymentError("DOKPLOY_URL must be an HTTPS origin.")
    return value


def dokploy(path, data=None, **query):
    url = f"{dokploy_origin()}/api/{path}"
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
    version = release_version()
    rollout_log_url()
    ci_run_id = required("CI_RUN_ID")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", ci_run_id):
        raise DeploymentError("Invalid CI run identity.")
    result = api(
        f"{github_base()}/deployments",
        {"Authorization": f"Bearer {required('GITHUB_TOKEN')}"},
        {
            "ref": sha,
            "task": "pandect-rollout",
            "environment": "production",
            "auto_merge": False,
            "required_contexts": [],
            "production_environment": True,
            "transient_environment": False,
            "payload": {"ci_run_id": ci_run_id, "image": image, "version": version},
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
    result = api(
        f"{github_base()}/deployments/{deployment_id}/statuses",
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


def public_health_ready(path, sha):
    try:
        health = api(f"https://openpandect.com{path}")
    except DeploymentError:
        return False
    return (
        isinstance(health, dict)
        and health.get("status") == "ok"
        and health.get("commit") == sha
    )


def wait_ready(application_id, image, sha, title, previous_ids, started_at, service):
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
        if task and service == "worker":
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
            for path in ("/health/", "/events/health/"):
                waiting_for = f"public web health at {path} on the expected commit"
                if not public_health_ready(path, sha):
                    break
            else:
                ready = True
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
    version = release_version()
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
    title = f"Deploy {service} {version} CI {required('CI_RUN_ID')}"
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
        service,
    )
    print(f"Verified {service} running {version} ({sha}) from {image}.")


def main():
    release_version()
    sha = required("TESTED_SHA")
    repository = required("GITHUB_REPOSITORY")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise DeploymentError("Invalid CI commit identity.")
    if not eligible_release(sha):
        return
    output_path = runner_file("GITHUB_OUTPUT")
    if os.environ.get("PANDECT_PRODUCTION_DATABASE_BASELINE") != "domain-apps-v1":
        raise DeploymentError(
            "Production requires a fresh PostgreSQL database and empty queue for "
            "the domain-app migration baseline. Configure the Dokploy applications "
            "for it, then set the GitHub production environment variable "
            "PANDECT_PRODUCTION_DATABASE_BASELINE=domain-apps-v1. "
            "See docs/operations.md."
        )
    dokploy_origin()
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
    worker_id = required("DOKPLOY_WORKER_APPLICATION_ID")
    web_id = required("DOKPLOY_WEB_APPLICATION_ID")
    if worker_id == web_id:
        raise DeploymentError("Worker and web must use separate Dokploy applications.")
    deployment_id = create_rollout(sha, image)
    mark_rollout(deployment_id, "in_progress")
    try:
        # Publish the pending marker before the final history check so CI cannot
        # mistake an in-progress rollout for a verified documentation baseline.
        if not eligible_release(sha):
            mark_rollout(deployment_id, "inactive")
            return
        deploy(worker_id, image, sha, "worker")
        deploy(web_id, image, sha, "web")
    except Exception:
        try:
            mark_rollout(deployment_id, "failure")
        except Exception:
            print("Could not mark the failed rollout; it remains unverified.")
        raise
    # If this status write fails, leave the marker unverified and block release
    # publication even though all services may already be running the image.
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
