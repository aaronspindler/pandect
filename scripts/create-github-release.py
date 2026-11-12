"""Publish one GitHub release per verified production commit."""

import json
import os
import re
import sys
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ReleaseError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def required(name):
    value = os.environ.get(name, "")
    if not value:
        raise ReleaseError(f"Set {name} in the release job.")
    return value


def github(path, data=None, missing_ok=False):
    url = (
        f"{required('GITHUB_API_URL').rstrip('/')}/repos/"
        f"{required('GITHUB_REPOSITORY')}/{path}"
    )
    request = Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Authorization": f"Bearer {required('GITHUB_TOKEN')}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "pandect-cd/1.0",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ReleaseError("GitHub API response exceeded its size limit.")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ReleaseError("Unexpected GitHub API response.")
        return payload
    except HTTPError as error:
        status = error.code
        error.close()
        if status == 404 and missing_ok and data is None:
            return None
        raise ReleaseError(f"GitHub API request failed (HTTP {status}).") from None
    # The CD runner uses Python 3.12 rather than the application's runtime.
    except (OSError, HTTPException, ValueError):  # noqa: UP039  # fmt: skip
        raise ReleaseError(
            "GitHub API request failed; inspect the release job."
        ) from None


def main():
    sha = required("TESTED_SHA")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ReleaseError("Invalid deployed commit identity.")
    repository = required("GITHUB_REPOSITORY")
    server = required("GITHUB_SERVER_URL").rstrip("/")
    ci_run_id = required("CI_RUN_ID")
    cd_run_id = required("GITHUB_RUN_ID")
    tag = f"deploy-{sha}"
    reference = github(f"git/ref/tags/{tag}", missing_ok=True)
    if reference is not None:
        commit = github(f"commits/{quote(f'refs/tags/{tag}', safe='')}")
        if commit.get("sha") != sha:
            raise ReleaseError(
                "The deployment tag points to a different commit; inspect it "
                "before rerunning the release job."
            )
    existing = github(f"releases/tags/{tag}", missing_ok=True)
    if existing is not None:
        if (
            reference is None
            or existing.get("tag_name") != tag
            or existing.get("draft") is not False
            or not existing.get("published_at")
        ):
            raise ReleaseError(
                "The existing release is not a published release for the verified "
                "deployment tag; inspect it before rerunning the release job."
            )
        print(f"Release {tag} already exists; leaving it unchanged.")
        return
    body = (
        f"Verified production deployment of `{sha}`.\n\n"
        f"- [Successful CI]({server}/{repository}/actions/runs/{ci_run_id})\n"
        f"- [Verified deployment]({server}/{repository}/actions/runs/{cd_run_id})\n"
    )
    created = github(
        "releases",
        {
            "tag_name": tag,
            "target_commitish": sha,
            "name": f"Deployment {sha[:12]}",
            "body": body,
            "draft": False,
            "prerelease": False,
            "generate_release_notes": True,
            "make_latest": "false",
        },
    )
    if (
        created.get("tag_name") != tag
        or created.get("draft") is not False
        or not created.get("published_at")
    ):
        raise ReleaseError("GitHub did not return a published deployment release.")
    print(f"Published release {tag} for the verified deployment.")


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, KeyError, TypeError) as error:
        detail = (
            str(error)
            if isinstance(error, ReleaseError)
            else "Unexpected API response."
        )
        print(f"Release failed: {detail}", file=sys.stderr)
        sys.exit(1)
