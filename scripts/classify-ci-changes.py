"""Select full CI unless a complete, trusted comparison changes only guides."""

import json
import os
import re
import subprocess
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
ROOT_GUIDES = {"README.md", "AGENTS.md", "DESIGN.md", "PRINCIPLES.md", "SECURITY.md"}
UPSTREAM = "aaronspindler/pandect"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def git(*arguments):
    return subprocess.check_output(
        ["git", "--no-optional-locks", *arguments],
        cwd=ROOT,
        stderr=subprocess.PIPE,
        timeout=30,
    ).strip(b"\n")


def commit_sha(value):
    if not isinstance(value, str) or SHA_PATTERN.fullmatch(value) is None:
        raise ValueError("Invalid commit identity.")
    if git("rev-parse", f"{value}^{{commit}}").decode() != value:
        raise ValueError("Unavailable commit identity.")
    return value


def github(path):
    api_url = os.environ["GITHUB_API_URL"].rstrip("/")
    token = os.environ["GITHUB_TOKEN"]
    if api_url != "https://api.github.com" or not token:
        raise ValueError("Unavailable trusted deployment service.")
    request = Request(
        f"{api_url}/repos/{UPSTREAM}/{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "pandect-ci/1.0",
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        error.close()
        raise ValueError("Unavailable deployment metadata.") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Deployment response exceeded its size limit.")
    payload = json.loads(body)
    if not isinstance(payload, list):
        raise ValueError("Unexpected deployment response.")
    return payload


def deployment_baseline(head):
    deployments = github(
        "deployments?environment=production&task=pandect-rollout&per_page=1"
    )
    if len(deployments) != 1 or not isinstance(deployments[0], dict):
        raise ValueError("No unambiguous rollout record is available.")
    deployment = deployments[0]
    identifier, sha, payload = (
        deployment.get("id"),
        deployment.get("sha"),
        deployment.get("payload"),
    )
    if (
        type(identifier) is not int
        or identifier <= 0
        or deployment.get("task") != "pandect-rollout"
        or deployment.get("environment") != "production"
        or not isinstance(sha, str)
        or SHA_PATTERN.fullmatch(sha) is None
        or deployment.get("ref") != sha
        or not isinstance(payload, dict)
        or not isinstance(payload.get("ci_run_id"), str)
        or re.fullmatch(r"[1-9][0-9]*", payload["ci_run_id"]) is None
        or not isinstance(payload.get("image"), str)
        or re.fullmatch(rf"ghcr\.io/{UPSTREAM}@sha256:[0-9a-f]{{64}}", payload["image"])
        is None
    ):
        raise ValueError("Rollout identity is incomplete or inconsistent.")
    statuses = github(f"deployments/{identifier}/statuses?per_page=1")
    if (
        len(statuses) != 1
        or not isinstance(statuses[0], dict)
        or statuses[0].get("state") != "success"
    ):
        raise ValueError("The latest rollout is not verified successful.")
    commit_sha(sha)
    if sha not in git("rev-list", "--first-parent", head).decode().splitlines():
        raise ValueError("The latest rollout is outside main's first-parent history.")
    return sha


def documentation_only(base, head, *, every_commit=False):
    if every_commit:
        # An intermediate application commit may already be deploying. Its
        # later revert still needs validation and publication, even when the
        # final tree matches the last completed deployment's application code.
        paths = git(
            "log",
            "--first-parent",
            "--format=",
            "--name-only",
            "--no-renames",
            "--diff-merges=first-parent",
            "-z",
            f"{base}..{head}",
            "--",
        )
    else:
        paths = git("diff", "--name-only", "-z", "--no-renames", base, head, "--")
    if not paths or not paths.endswith(b"\0"):
        return False
    for raw_path in paths[:-1].split(b"\0"):
        path = raw_path.decode()
        if path in ROOT_GUIDES:
            continue
        if not path.startswith("docs/") or not path.endswith(".md"):
            return False
    return True


def comparison():
    event_name = os.environ.get("GITHUB_EVENT_NAME")
    if event_name not in {"pull_request", "push"}:
        return None
    with open(os.environ["GITHUB_EVENT_PATH"], "rb") as event_file:
        raw_event = event_file.read(MAX_RESPONSE_BYTES + 1)
    if len(raw_event) > MAX_RESPONSE_BYTES:
        raise ValueError("Event exceeded its size limit.")
    event = json.loads(raw_event)
    head = commit_sha(os.environ["GITHUB_SHA"])
    if git("rev-parse", "HEAD").decode() != head:
        raise ValueError("Checkout differs from the event.")
    if git("rev-parse", "--is-shallow-repository") != b"false":
        raise ValueError("Complete history is required.")
    if event_name == "pull_request":
        pull_request = event["pull_request"]
        base = commit_sha(pull_request["base"]["sha"])
        head = commit_sha(pull_request["head"]["sha"])
        merge_bases = git("merge-base", "--all", base, head).decode().splitlines()
        if len(merge_bases) != 1:
            raise ValueError("Ambiguous pull request ancestry.")
        return merge_bases[0], head
    if (
        os.environ["GITHUB_REPOSITORY"] != UPSTREAM
        or event["repository"]["full_name"] != UPSTREAM
        or event["ref"] != "refs/heads/main"
        or event["after"] != head
        or event.get("forced") is not False
    ):
        return None
    return deployment_baseline(head), head


def classify():
    try:
        commits = comparison()
        if commits is not None and documentation_only(
            *commits, every_commit=os.environ.get("GITHUB_EVENT_NAME") == "push"
        ):
            return (
                False,
                "Only documentation changed in the complete comparison.",
                *commits,
            )
        base, head = commits if commits is not None else ("", "")
        return (
            True,
            "Full validation is required for this event or comparison.",
            base,
            head,
        )
    # GitHub's runner Python is 3.12; retain HTTPException's explicit catch.
    except (  # noqa: UP039
        OSError,
        HTTPException,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ):
        return True, "Classification is unavailable; running full validation.", "", ""


def main():
    full, reason, base, head = classify()
    output = f"full={str(full).lower()}\nreason={reason}\n"
    if base and head:
        output += f"base={base}\nhead={head}\n"
    print(output, end="")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output_file:
        output_file.write(output)


if __name__ == "__main__":
    main()
