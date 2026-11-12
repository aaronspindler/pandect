import ast
import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from unittest import TestCase
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError
from urllib.request import HTTPHandler, HTTPSHandler, OpenerDirector
from urllib.response import addinfourl

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "create-github-release.py"
SPEC = importlib.util.spec_from_file_location("create_github_release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)

SHA = "a" * 40
TAG = f"deploy-{SHA}"
REFERENCE = {"object": {"type": "commit", "sha": SHA}}
PUBLISHED = {"tag_name": TAG, "draft": False, "published_at": "2026-11-12T12:00:00Z"}
ENVIRONMENT = {
    "TESTED_SHA": SHA,
    "GITHUB_REPOSITORY": "example/pandect",
    "GITHUB_API_URL": "https://api.github.com",
    "GITHUB_TOKEN": "synthetic-github-token",
    "GITHUB_SERVER_URL": "https://github.com",
    "CI_RUN_ID": "123",
    "GITHUB_RUN_ID": "456",
}


class GitHubReleaseCommandTests(TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, ENVIRONMENT, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_helper_parses_on_the_cd_runners_python_312(self):
        ast.parse(SCRIPT.read_text(), feature_version=(3, 12))

    def test_new_release_targets_the_verified_commit_and_records_provenance(self):
        with (
            patch.object(
                release, "github", side_effect=[None, None, PUBLISHED]
            ) as request,
            redirect_stdout(io.StringIO()),
        ):
            release.main()
        self.assertEqual(
            request.call_args_list[:2],
            [
                call(f"git/ref/tags/{TAG}", missing_ok=True),
                call(f"releases/tags/{TAG}", missing_ok=True),
            ],
        )
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args.args[0], "releases")
        data = request.call_args.args[1]
        self.assertEqual(data["tag_name"], TAG)
        self.assertEqual(data["target_commitish"], SHA)
        self.assertEqual(data["name"], f"Deployment {SHA[:12]}")
        self.assertTrue(data["generate_release_notes"])
        self.assertFalse(data["draft"])
        self.assertFalse(data["prerelease"])
        self.assertEqual(data["make_latest"], "false")
        self.assertIn(SHA, data["body"])
        self.assertIn(
            "https://github.com/example/pandect/actions/runs/123", data["body"]
        )
        self.assertIn(
            "https://github.com/example/pandect/actions/runs/456", data["body"]
        )

    def test_published_release_rerun_does_not_mutate_it(self):
        with (
            patch.object(
                release, "github", side_effect=[REFERENCE, {"sha": SHA}, PUBLISHED]
            ) as request,
            redirect_stdout(io.StringIO()) as output,
        ):
            release.main()
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all(len(item.args) == 1 for item in request.call_args_list))
        self.assertIn("already exists", output.getvalue())

    def test_annotated_tag_is_resolved_to_its_commit(self):
        annotated = {"object": {"type": "tag", "sha": "b" * 40}}
        with (
            patch.object(
                release, "github", side_effect=[annotated, {"sha": SHA}, PUBLISHED]
            ) as request,
            redirect_stdout(io.StringIO()),
        ):
            release.main()
        self.assertIn(call(f"commits/refs%2Ftags%2F{TAG}"), request.call_args_list)

    def test_existing_tag_for_a_different_commit_blocks_release_creation(self):
        with (
            patch.object(
                release, "github", side_effect=[REFERENCE, {"sha": "b" * 40}]
            ) as request,
            self.assertRaisesRegex(release.ReleaseError, "different commit"),
        ):
            release.main()
        self.assertEqual(request.call_count, 2)

    def test_tag_only_partial_attempt_can_finish_publication(self):
        with (
            patch.object(
                release,
                "github",
                side_effect=[REFERENCE, {"sha": SHA}, None, PUBLISHED],
            ) as request,
            redirect_stdout(io.StringIO()),
        ):
            release.main()
        self.assertEqual(request.call_count, 4)
        self.assertEqual(request.call_args.args[0], "releases")
        self.assertEqual(request.call_args.args[1]["target_commitish"], SHA)

    def test_existing_draft_malformed_or_wrong_tag_release_is_not_changed(self):
        for existing in (
            {**PUBLISHED, "draft": True},
            {**PUBLISHED, "tag_name": "another-tag"},
            {**PUBLISHED, "published_at": None},
            {},
        ):
            with (
                self.subTest(existing=existing),
                patch.object(
                    release, "github", side_effect=[REFERENCE, {"sha": SHA}, existing]
                ) as request,
                self.assertRaisesRegex(release.ReleaseError, "existing release"),
            ):
                release.main()
            self.assertEqual(request.call_count, 3)

    def test_existing_release_without_its_tag_is_not_accepted(self):
        with (
            patch.object(release, "github", side_effect=[None, PUBLISHED]) as request,
            self.assertRaisesRegex(release.ReleaseError, "existing release"),
        ):
            release.main()
        self.assertEqual(request.call_count, 2)

    def test_invalid_sha_is_rejected_before_any_request(self):
        for sha in ("main", "a" * 39, "A" * 40):
            with (
                self.subTest(sha=sha),
                patch.dict(os.environ, {"TESTED_SHA": sha}),
                patch.object(release, "github") as request,
                self.assertRaisesRegex(release.ReleaseError, "commit identity"),
            ):
                release.main()
            request.assert_not_called()

    def test_publication_failure_is_not_retried(self):
        with (
            patch.object(
                release,
                "github",
                side_effect=[None, None, release.ReleaseError("publication failed")],
            ) as request,
            self.assertRaisesRegex(release.ReleaseError, "publication failed"),
        ):
            release.main()
        self.assertEqual(request.call_count, 3)

    def test_only_lookup_404_is_treated_as_absent(self):
        for status in (403, 404, 429, 500):
            error = HTTPError(
                "https://api.github.com/private",
                status,
                "private",
                {},
                io.BytesIO(b"secret"),
            )
            with (
                self.subTest(status=status),
                patch.object(OpenerDirector, "open", side_effect=error) as request,
            ):
                if status == 404:
                    self.assertIsNone(
                        release.github(f"releases/tags/{TAG}", missing_ok=True)
                    )
                else:
                    with self.assertRaisesRegex(release.ReleaseError, f"HTTP {status}"):
                        release.main()
            request.assert_called_once()

    def test_publication_404_is_an_error_even_if_missing_is_allowed(self):
        error = HTTPError(
            "https://api.github.com/private", 404, "private", {}, io.BytesIO(b"secret")
        )
        with (
            patch.object(OpenerDirector, "open", side_effect=error) as request,
            self.assertRaisesRegex(release.ReleaseError, "HTTP 404"),
        ):
            release.github("releases", {"tag_name": TAG}, missing_ok=True)
        request.assert_called_once()

    def test_transport_failure_does_not_publish_or_expose_private_detail(self):
        with (
            patch.object(
                OpenerDirector, "open", side_effect=URLError("private detail")
            ) as request,
            self.assertRaises(release.ReleaseError) as failure,
        ):
            release.main()
        self.assertNotIn("private detail", str(failure.exception))
        request.assert_called_once()

    def test_api_sends_authorized_json_with_a_bounded_timeout(self):
        with patch.object(
            OpenerDirector, "open", return_value=io.BytesIO(b"{}")
        ) as request:
            release.github("releases", {"tag_name": TAG})
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_method(), "POST")
        self.assertEqual(
            sent.get_header("Authorization"), "Bearer synthetic-github-token"
        )
        self.assertEqual(json.loads(sent.data), {"tag_name": TAG})
        self.assertEqual(request.call_args.kwargs["timeout"], 30)

    def test_api_rejects_redirect_without_forwarding_credentials(self):
        url = "https://api.github.com/repos/example/pandect/releases"
        headers = Message()
        headers["Location"] = "http://redirect.example.test/"
        response = addinfourl(io.BytesIO(), headers, url, 302)
        response.msg = "Found"
        with (
            patch.object(HTTPSHandler, "https_open", return_value=response) as initial,
            patch.object(HTTPHandler, "http_open") as redirected,
            self.assertRaisesRegex(release.ReleaseError, "HTTP 302"),
        ):
            release.github("releases")
        initial.assert_called_once()
        redirected.assert_not_called()

    def test_api_rejects_oversized_and_non_object_responses(self):
        for body in (b"x" * (2 * 1024 * 1024 + 1), b"[]", b"not-json"):
            with (
                self.subTest(size=len(body)),
                patch.object(OpenerDirector, "open", return_value=io.BytesIO(body)),
                self.assertRaises(release.ReleaseError),
            ):
                release.github("releases")
