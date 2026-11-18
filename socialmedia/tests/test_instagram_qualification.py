import io
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from instagram_pilot import qualify
from instagram_pilot.live_roster import LiveBrowserError
from socialmedia.instagram_connector import LIVE_VERSION


class LocalQualificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.output = Path(self.directory) / "capture.json"
        self.args = ["--target", "@test.account", "--output", str(self.output)]
        self.playwright = self.enterContext(patch.object(qualify, "sync_playwright"))
        self.browser = self.enterContext(
            patch.object(qualify, "LiveBrowser")
        ).return_value
        self.confirm = self.enterContext(patch.object(qualify, "_confirm"))
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))

    def test_reconfirms_pending_sign_in_and_publishes_private_partial_capture(self):
        confirmed = []
        self.confirm.side_effect = lambda browser: confirmed.append(True)
        operations = []

        def operation(path, data):
            operations.append(path)
            if path == "/pair/start":
                return {"id": "private-pair"}
            self.assertTrue(confirmed)
            if path == "/pair/finish":
                if len(confirmed) == 1:
                    raise LiveBrowserError("pairing_pending")
                return {"session": "private-ticket", "observer": "private.observer"}
            self.assertEqual(
                data, {"session": "private-ticket", "target": "test.account"}
            )
            return {
                "observer": "private.observer",
                "target": "test.account",
                "followers": {
                    "members": [{"handle": "only.in.file"}],
                    "complete": True,
                    "reason": "",
                    "page_count": 1,
                    "expected_count": 1,
                },
                "following": {
                    "members": [],
                    "complete": False,
                    "reason": "time_limit",
                    "page_count": 0,
                    "expected_count": None,
                },
            }

        self.browser.operation.side_effect = operation
        self.assertEqual(qualify.main(self.args), 3)
        self.assertEqual(
            operations, ["/pair/start", "/pair/finish", "/pair/finish", "/collect"]
        )
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        capture = json.loads(self.output.read_text())
        self.assertEqual(capture["followers"]["members"][0]["handle"], "only.in.file")
        self.assertLessEqual(capture["started_at"], capture["finished_at"])
        self.assertNotIn("private-ticket", self.output.read_text())
        self.assertEqual(
            self.stdout.getvalue(),
            "followers: 1 observed, complete, pages read 1, profile count 1\n"
            "following: 0 observed, partial, pages read 0 (time_limit)\n",
        )
        self.browser.close.assert_called_once()

    def test_preexisting_destination_never_opens_browser(self):
        self.output.write_text("existing private material")
        self.assertEqual(qualify.main(self.args), 1)
        self.assertEqual(self.output.read_text(), "existing private material")
        self.playwright.assert_not_called()

    def test_interrupt_or_expiry_publishes_nothing_and_closes_browser(self):
        self.browser.operation.return_value = {"id": "private-pair"}
        for error, status in (
            (KeyboardInterrupt(), 130),
            (LiveBrowserError("pairing_expired"), 1),
        ):
            with self.subTest(status=status):
                self.confirm.side_effect = error
                self.assertEqual(qualify.main(self.args), status)
                self.assertEqual(list(Path(self.directory).iterdir()), [])
                self.assertEqual(self.browser.operation.call_count, 1)
                self.browser.close.assert_called_once()
                self.browser.reset_mock()

    def _observation(self, following, **metadata):
        return {
            "source": "instagram",
            "collector_version": LIVE_VERSION,
            "observer": "test.observer",
            "target": "test.account",
            "target_id": "1",
            "followers": {
                "members": [{"handle": "unchanged", "id": "2"}],
                "complete": True,
                "reason": "",
                "page_count": 1,
                "expected_count": 1,
            },
            "following": following,
            **metadata,
        }

    def _mismatched_following(self, handle, identifier):
        return {
            "members": [{"handle": handle, "id": identifier}],
            "complete": False,
            "reason": "count_mismatch",
            "expected_count": 2,
            "page_count": 1,
        }

    def _write_baseline(self, **metadata):
        baseline = Path(self.directory) / "baseline.json"
        baseline.write_text(
            json.dumps(
                self._observation(
                    self._mismatched_following("observed.before", "3"),
                    started_at="2020-01-01T00:00:00+00:00",
                    finished_at="2020-01-01T00:00:01+00:00",
                    **metadata,
                )
            )
        )
        self.args.extend(["--baseline", str(baseline)])
        return baseline

    def _supply_observation(self, observation, *, observer="test.observer"):
        self.browser.operation.side_effect = [
            {"id": "private-pair"},
            {"observer": observer, "session": "private-ticket"},
            observation,
        ]

    def test_compares_mismatched_captures_and_retains_the_prior_file(self):
        baseline = self._write_baseline()
        original = baseline.read_bytes()
        self._supply_observation(
            self._observation(self._mismatched_following("observed.now", "4"))
        )

        self.assertEqual(qualify.main(self.args), 3)
        self.assertEqual(baseline.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        comparison = json.loads(self.output.read_text())["comparison"]
        self.assertEqual(comparison["baseline_sha256"], sha256(original).hexdigest())
        self.assertEqual(comparison["followers"]["additions"], [])
        self.assertEqual(
            comparison["following"],
            {
                "baseline_reason": "count_mismatch",
                "current_reason": "count_mismatch",
                "additions": [
                    {"handle": "observed.now", "id": "4", "handle_only": False}
                ],
            },
        )
        self.assertIn("1 newly observed", self.stdout.getvalue())
        self.assertIn("baseline count mismatch", self.stdout.getvalue())
        self.assertNotIn("observed.now", self.stdout.getvalue())

    def test_skips_interrupted_direction_and_compares_the_other_list(self):
        self._write_baseline()
        interrupted = self._mismatched_following("observed.now", "4")
        interrupted["reason"] = "time_limit"
        self._supply_observation(self._observation(interrupted))

        self.assertEqual(qualify.main(self.args), 3)
        comparison = json.loads(self.output.read_text())["comparison"]
        self.assertEqual(comparison["followers"]["additions"], [])
        self.assertIsNone(comparison["following"]["additions"])
        self.assertIn("comparison skipped", self.stdout.getvalue())

    def test_invalid_baseline_target_stops_before_opening_browser(self):
        self._write_baseline(target="different.account")

        self.assertEqual(qualify.main(self.args), 1)
        self.playwright.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_different_observer_stops_before_collection(self):
        self._write_baseline()
        self._supply_observation({}, observer="different.account")

        self.assertEqual(qualify.main(self.args), 1)
        self.assertEqual(self.browser.operation.call_count, 2)
        self.assertFalse(self.output.exists())

    def test_different_numeric_target_id_refuses_comparison(self):
        self._write_baseline()
        self._supply_observation(
            self._observation(
                self._mismatched_following("observed.now", "4"), target_id="5"
            )
        )

        self.assertEqual(qualify.main(self.args), 1)
        self.assertFalse(self.output.exists())
