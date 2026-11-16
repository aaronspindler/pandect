"""Publish a versioned GitHub release for a verified production deployment."""

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


def runner_file(name):
    path = os.path.realpath(required(name))
    root = os.path.realpath(required("RUNNER_TEMP"))
    if not path.startswith(root + os.sep):
        raise ReleaseError(f"{name} must stay inside RUNNER_TEMP.")
    return path


def github(path, data=None, missing_ok=False, list_result=False):
    if required("GITHUB_API_URL").rstrip("/") != "https://api.github.com":
        raise ReleaseError("Only the public GitHub API is supported.")
    repository = required("GITHUB_REPOSITORY")
    if not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}", repository
    ):
        raise ReleaseError("Invalid GitHub repository identity.")
    url = f"https://api.github.com/repos/{quote(repository, safe='/')}/{path}"
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
        expected = list if list_result and data is None else dict
        if not isinstance(payload, expected):
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


VERSION = re.compile(r"v1\.(0|[1-9][0-9]{0,19})")


def release_history():
    """Read a complete bounded history; only published stable releases count."""
    versions = {}
    for page in range(1, 21):
        entries = github(f"releases?per_page=100&page={page}", list_result=True)
        if (
            not isinstance(entries, list)
            or len(entries) > 100
            or not all(isinstance(entry, dict) for entry in entries)
        ):
            raise ReleaseError("GitHub returned an invalid release history.")
        for entry in entries:
            tag = entry.get("tag_name")
            if not isinstance(tag, str):
                raise ReleaseError("GitHub returned an invalid release identity.")
            match = VERSION.fullmatch(tag)
            if match is None:
                continue
            if (
                type(entry.get("draft")) is not bool
                or type(entry.get("prerelease")) is not bool
            ):
                raise ReleaseError("GitHub returned an invalid release state.")
            if entry["draft"] or entry["prerelease"] or not entry.get("published_at"):
                continue
            number = int(match[1])
            if number in versions:
                raise ReleaseError("GitHub returned duplicate release versions.")
            versions[number] = entry
        if len(entries) < 100:
            break
    else:
        raise ReleaseError("Release history exceeds the supported pagination limit.")
    if any(number != expected for expected, number in enumerate(sorted(versions))):
        raise ReleaseError("Published release history has a gap; inspect it first.")
    return versions


def tag_commit(tag):
    commit = github(f"commits/{quote(f'refs/tags/{tag}', safe='')}")
    sha = commit.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ReleaseError("GitHub did not resolve the release tag to a commit.")
    return sha


def allocate_version(sha):
    """Reuse the latest successful source or propose its unconsumed successor."""
    versions = release_history()
    if versions:
        latest = versions[len(versions) - 1]
        latest_tag = latest["tag_name"]
        if tag_commit(latest_tag) == sha:
            return latest_tag, True
        if any(entry.get("target_commitish") == sha for entry in versions.values()):
            raise ReleaseError("A newer release already follows this commit.")
    tag = f"v1.{len(versions)}"
    reference = github(f"git/ref/tags/{tag}", missing_ok=True)
    if reference is not None and tag_commit(tag) != sha:
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
            or existing.get("prerelease") is not False
            or not existing.get("published_at")
        ):
            raise ReleaseError(
                "The existing release is not a published stable release for the "
                "verified deployment tag; inspect it before rerunning."
            )
        return tag, True
    return tag, False


def main(prepare=False):
    sha = required("TESTED_SHA")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ReleaseError("Invalid deployed commit identity.")
    ci_run_id = required("CI_RUN_ID")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", ci_run_id):
        raise ReleaseError("Invalid CI run identity.")
    expected = os.environ.get("PANDECT_VERSION", "")
    if (not prepare or expected) and VERSION.fullmatch(expected) is None:
        raise ReleaseError("Invalid deployment version; expected v1.N.")
    output_path = runner_file("GITHUB_OUTPUT") if prepare else None
    tag, published = allocate_version(sha)
    if expected and tag != expected:
        raise ReleaseError("Release history changed; prepare the version again.")
    if prepare:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"version={tag}\npublished={str(published).lower()}\n")
        print(
            f"Prepared {tag}; {'already published' if published else 'not consumed'}."
        )
        return
    if published:
        print(f"Release {tag} already exists; leaving it unchanged.")
        return
    repository = required("GITHUB_REPOSITORY")
    server = required("GITHUB_SERVER_URL").rstrip("/")
    cd_run_id = required("GITHUB_RUN_ID")
    body = (
        f"Verified production deployment of {tag} (`{sha}`).\n\n"
        f"- [Successful CI]({server}/{repository}/actions/runs/{ci_run_id})\n"
        f"- [Verified deployment]({server}/{repository}/actions/runs/{cd_run_id})\n"
    )
    created = github(
        "releases",
        {
            "tag_name": tag,
            "target_commitish": sha,
            "name": f"Pandect {tag}",
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
        or created.get("prerelease") is not False
        or not created.get("published_at")
    ):
        raise ReleaseError("GitHub did not return a published deployment release.")
    print(f"Published release {tag} for the verified deployment.")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--prepare"]:
            main(prepare=True)
        elif sys.argv[1:]:
            raise ReleaseError("Usage: create-github-release.py [--prepare]")
        else:
            main()
    except (ReleaseError, KeyError, TypeError) as error:
        detail = (
            str(error)
            if isinstance(error, ReleaseError)
            else "Unexpected API response."
        )
        print(f"Release failed: {detail}", file=sys.stderr)
        sys.exit(1)
