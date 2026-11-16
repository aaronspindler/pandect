import ast
import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
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
TAG = "v1.0"
REFERENCE = {"object": {"type": "commit", "sha": SHA}}
PUBLISHED = {
    "tag_name": TAG,
    "target_commitish": SHA,
    "draft": False,
    "prerelease": False,
    "published_at": "2026-11-12T12:00:00Z",
}
ENVIRONMENT = {
    "TESTED_SHA": SHA,
    "PANDECT_VERSION": TAG,
    "GITHUB_REPOSITORY": "example/pandect",
    "GITHUB_API_URL": "https://api.github.com",
    "GITHUB_TOKEN": "synthetic-github-token",
    "GITHUB_SERVER_URL": "https://github.com",
    "CI_RUN_ID": "123",
    "GITHUB_RUN_ID": "456",
}


class GitHubReleaseCommandTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "github-output"
        environment = patch.dict(
            os.environ, {**ENVIRONMENT, "GITHUB_OUTPUT": str(self.output)}, clear=True
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_helper_parses_on_the_cd_runners_python_312(self):
        ast.parse(SCRIPT.read_text(), feature_version=(3, 12))

    def prepare(self, *responses, expected=""):
        with (
            patch.dict(os.environ, {"PANDECT_VERSION": expected}),
            patch.object(release, "github", side_effect=responses) as request,
            redirect_stdout(io.StringIO()),
        ):
            release.main(prepare=True)
        self.assertTrue(all(len(item.args) == 1 for item in request.call_args_list))
        return request

    def test_new_release_targets_the_verified_commit_and_records_provenance(self):
        with (
            patch.object(
                release, "github", side_effect=[[], None, None, PUBLISHED]
            ) as request,
            redirect_stdout(io.StringIO()),
        ):
            release.main()
        self.assertEqual(
            request.call_args_list[:3],
            [
                call("releases?per_page=100&page=1", list_result=True),
                call(f"git/ref/tags/{TAG}", missing_ok=True),
                call(f"releases/tags/{TAG}", missing_ok=True),
            ],
        )
        self.assertEqual(request.call_count, 4)
        self.assertEqual(request.call_args.args[0], "releases")
        data = request.call_args.args[1]
        self.assertEqual(data["tag_name"], TAG)
        self.assertEqual(data["target_commitish"], SHA)
        self.assertEqual(data["name"], f"Pandect {TAG}")
        self.assertTrue(data["generate_release_notes"])
        self.assertFalse(data["draft"])
        self.assertFalse(data["prerelease"])
        self.assertEqual(data["make_latest"], "false")
        self.assertIn(SHA, data["body"])
        self.assertIn(TAG, data["body"])
        self.assertIn(
            "https://github.com/example/pandect/actions/runs/123", data["body"]
        )
        self.assertIn(
            "https://github.com/example/pandect/actions/runs/456", data["body"]
        )

    def test_prepare_starts_at_v1_0_without_creating_a_tag_or_release(self):
        self.prepare([], None, None)
        self.assertEqual(self.output.read_text(), "version=v1.0\npublished=false\n")

    def test_only_published_stable_v1_releases_consume_numbers(self):
        ignored = [
            {"tag_name": "deploy-" + SHA},
            {"tag_name": "v0.1.0"},
            {"tag_name": "v1.01"},
            {**PUBLISHED, "tag_name": "v1.8", "draft": True},
            {**PUBLISHED, "tag_name": "v1.9", "prerelease": True},
            {**PUBLISHED, "tag_name": "v1.10", "published_at": None},
        ]
        self.prepare(ignored, None, None)
        self.assertEqual(self.output.read_text(), "version=v1.0\npublished=false\n")

    def test_next_version_follows_successful_release_not_ci_number(self):
        previous = {**PUBLISHED, "target_commitish": "b" * 40}
        with patch.dict(os.environ, {"CI_RUN_ID": "999999"}):
            self.prepare([previous], {"sha": "b" * 40}, None, None)
        self.assertEqual(self.output.read_text(), "version=v1.1\npublished=false\n")

    def test_successful_publication_uses_exact_next_version(self):
        previous = {**PUBLISHED, "target_commitish": "b" * 40}
        published = {**PUBLISHED, "tag_name": "v1.1"}
        with (
            patch.dict(os.environ, {"PANDECT_VERSION": "v1.1"}),
            patch.object(
                release,
                "github",
                side_effect=[[previous], {"sha": "b" * 40}, None, None, published],
            ) as request,
            redirect_stdout(io.StringIO()),
        ):
            release.main()
        self.assertEqual(request.call_args.args[1]["tag_name"], "v1.1")
        self.assertEqual(request.call_args.args[1]["target_commitish"], SHA)

    def test_same_source_rerun_reuses_the_last_successful_version(self):
        self.prepare([PUBLISHED], {"sha": SHA})
        self.assertEqual(self.output.read_text(), "version=v1.0\npublished=true\n")

    def test_preparation_reconciles_a_successful_but_unacknowledged_publication(self):
        self.prepare([PUBLISHED], {"sha": SHA}, expected=TAG)
        self.assertEqual(self.output.read_text(), "version=v1.0\npublished=true\n")

    def test_preparation_checks_expected_version_before_image_promotion(self):
        with self.assertRaisesRegex(release.ReleaseError, "history changed"):
            self.prepare([], None, None, expected="v1.1")
        self.assertFalse(self.output.exists())

    def test_history_pagination_does_not_miss_a_published_version(self):
        page = [{"tag_name": f"legacy-{number}"} for number in range(100)]
        previous = {**PUBLISHED, "target_commitish": "b" * 40}
        request = self.prepare(page, [previous], {"sha": "b" * 40}, None, None)
        self.assertEqual(
            request.call_args_list[:2],
            [
                call("releases?per_page=100&page=1", list_result=True),
                call("releases?per_page=100&page=2", list_result=True),
            ],
        )
        self.assertEqual(self.output.read_text(), "version=v1.1\npublished=false\n")

    def test_history_is_bounded_and_never_assumed_complete_at_limit(self):
        page = [{"tag_name": f"legacy-{number}"} for number in range(100)]
        with (
            patch.object(release, "github", return_value=page) as request,
            self.assertRaisesRegex(release.ReleaseError, "pagination limit"),
        ):
            release.main(prepare=True)
        self.assertEqual(request.call_count, 20)
        self.assertFalse(self.output.exists())

    def test_invalid_duplicate_or_gapped_history_fails_before_allocating(self):
        for history in (
            {},
            [None],
            [{}],
            [{**PUBLISHED, "draft": None}],
            [{**PUBLISHED, "prerelease": None}],
            [PUBLISHED, PUBLISHED],
            [{**PUBLISHED, "tag_name": "v1.1"}],
            [PUBLISHED, {**PUBLISHED, "tag_name": "v1.99999999999999999999"}],
        ):
            with (
                self.subTest(history=history),
                patch.object(release, "github", return_value=history) as request,
                self.assertRaises(release.ReleaseError),
            ):
                release.main(prepare=True)
            request.assert_called_once()
            self.assertFalse(self.output.exists())

    def test_older_published_source_cannot_allocate_after_a_newer_release(self):
        newer = {**PUBLISHED, "tag_name": "v1.1", "target_commitish": "b" * 40}
        with self.assertRaisesRegex(release.ReleaseError, "newer release"):
            self.prepare([newer, PUBLISHED], {"sha": "b" * 40})
        self.assertFalse(self.output.exists())

    def test_invalid_or_missing_version_cannot_publish(self):
        for version in (
            "",
            SHA,
            "deploy-" + SHA,
            "v1",
            "v1.2.3",
            "v01.2",
            "v1.02",
            "v2.0",
            "v1.2/other",
            "v1.2\n",
        ):
            with (
                self.subTest(version=version),
                patch.dict(os.environ, {"PANDECT_VERSION": version}),
                patch.object(release, "github") as request,
                self.assertRaises(release.ReleaseError),
            ):
                release.main()
            request.assert_not_called()

    def test_published_release_rerun_does_not_mutate_it(self):
        with (
            patch.object(
                release, "github", side_effect=[[PUBLISHED], {"sha": SHA}]
            ) as request,
            redirect_stdout(io.StringIO()) as output,
        ):
            release.main()
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(len(item.args) == 1 for item in request.call_args_list))
        self.assertIn("already exists", output.getvalue())

    def test_annotated_tag_is_resolved_to_its_commit(self):
        annotated = {"object": {"type": "tag", "sha": "b" * 40}}
        with (
            patch.object(
                release,
                "github",
                side_effect=[[], annotated, {"sha": SHA}, None, PUBLISHED],
            ) as request,
            redirect_stdout(io.StringIO()),
        ):
            release.main()
        self.assertIn(call(f"commits/refs%2Ftags%2F{TAG}"), request.call_args_list)

    def test_existing_tag_for_a_different_commit_blocks_release_creation(self):
        with (
            patch.object(
                release, "github", side_effect=[[], REFERENCE, {"sha": "b" * 40}]
            ) as request,
            self.assertRaisesRegex(release.ReleaseError, "different commit"),
        ):
            release.main()
        self.assertEqual(request.call_count, 3)

    def test_tag_only_partial_attempt_can_finish_publication(self):
        with (
            patch.object(
                release,
                "github",
                side_effect=[[], REFERENCE, {"sha": SHA}, None, PUBLISHED],
            ) as request,
            redirect_stdout(io.StringIO()),
        ):
            release.main()
        self.assertEqual(request.call_count, 5)
        self.assertEqual(request.call_args.args[0], "releases")
        self.assertEqual(request.call_args.args[1]["target_commitish"], SHA)

    def test_existing_draft_malformed_or_wrong_tag_release_is_not_changed(self):
        for existing in (
            {**PUBLISHED, "draft": True},
            {**PUBLISHED, "prerelease": True},
            {**PUBLISHED, "tag_name": "another-tag"},
            {**PUBLISHED, "published_at": None},
            {},
        ):
            with (
                self.subTest(existing=existing),
                patch.object(
                    release,
                    "github",
                    side_effect=[[], REFERENCE, {"sha": SHA}, existing],
                ) as request,
                self.assertRaisesRegex(release.ReleaseError, "existing release"),
            ):
                release.main()
            self.assertEqual(request.call_count, 4)

    def test_existing_release_without_its_tag_is_not_accepted(self):
        with (
            patch.object(
                release, "github", side_effect=[[], None, PUBLISHED]
            ) as request,
            self.assertRaisesRegex(release.ReleaseError, "existing release"),
        ):
            release.main()
        self.assertEqual(request.call_count, 3)

    def test_invalid_sha_or_ci_identity_is_rejected_before_any_request(self):
        for name, value in (
            ("TESTED_SHA", "main"),
            ("TESTED_SHA", "a" * 39),
            ("TESTED_SHA", "A" * 40),
            ("CI_RUN_ID", "0"),
            ("CI_RUN_ID", "123/other"),
        ):
            with (
                self.subTest(name=name, value=value),
                patch.dict(os.environ, {name: value}),
                patch.object(release, "github") as request,
                self.assertRaisesRegex(release.ReleaseError, "identity"),
            ):
                release.main()
            request.assert_not_called()

    def test_preparation_requires_output_before_network_access(self):
        del os.environ["GITHUB_OUTPUT"]
        with (
            patch.object(release, "github") as request,
            self.assertRaisesRegex(release.ReleaseError, "GITHUB_OUTPUT"),
        ):
            release.main(prepare=True)
        request.assert_not_called()

    def test_publication_cannot_skip_the_next_successful_version(self):
        with (
            patch.dict(os.environ, {"PANDECT_VERSION": "v1.2"}),
            patch.object(release, "github", side_effect=[[], None, None]) as request,
            self.assertRaisesRegex(release.ReleaseError, "history changed"),
        ):
            release.main()
        self.assertTrue(all(len(item.args) == 1 for item in request.call_args_list))

    def test_publication_failure_is_not_retried_and_does_not_consume_a_number(self):
        with (
            patch.object(
                release,
                "github",
                side_effect=[
                    [],
                    None,
                    None,
                    release.ReleaseError("publication failed"),
                ],
            ) as request,
            self.assertRaisesRegex(release.ReleaseError, "publication failed"),
        ):
            release.main()
        self.assertEqual(request.call_count, 4)
        self.prepare([], None, None)
        self.assertEqual(self.output.read_text(), "version=v1.0\npublished=false\n")

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

    def test_api_allows_lists_only_for_explicit_read_only_history_requests(self):
        with patch.object(OpenerDirector, "open", return_value=io.BytesIO(b"[]")):
            self.assertEqual(
                release.github("releases?per_page=100&page=1", list_result=True), []
            )
        with (
            patch.object(OpenerDirector, "open", return_value=io.BytesIO(b"{}")),
            self.assertRaisesRegex(release.ReleaseError, "Unexpected"),
        ):
            release.github("releases?per_page=100&page=1", list_result=True)
        with (
            patch.object(OpenerDirector, "open", return_value=io.BytesIO(b"[]")),
            self.assertRaisesRegex(release.ReleaseError, "Unexpected"),
        ):
            release.github("releases", {"tag_name": TAG}, list_result=True)

    def test_api_rejects_oversized_and_non_object_responses(self):
        for body in (b"x" * (2 * 1024 * 1024 + 1), b"[]", b"not-json"):
            with (
                self.subTest(size=len(body)),
                patch.object(OpenerDirector, "open", return_value=io.BytesIO(body)),
                self.assertRaises(release.ReleaseError),
            ):
                release.github("releases")
