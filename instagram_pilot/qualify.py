"""Collect one private Instagram observation after direct browser sign-in."""

import argparse
import json
import os
import re
import select
import sys
import tempfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import monotonic

from playwright.sync_api import Error as BrowserError
from playwright.sync_api import sync_playwright

from instagram_pilot.live_host import LiveBrowser
from instagram_pilot.live_roster import LiveBrowserError, _handle
from socialmedia.instagram_rosters import (
    LIVE_VERSION,
    normalize_members,
    roster_additions,
    validate_endpoint_coverage,
)

DIRECTIONS = ("followers", "following")
MAX_CAPTURE_BYTES = 8 * 1024 * 1024


class BaselineError(ValueError):
    """A baseline is invalid or cannot be compared with the current capture."""


def _capture_details(capture, target):
    if (
        not isinstance(capture, dict)
        or capture.get("source") != "instagram"
        or capture.get("target") != target
        or capture.get("collector_version") != LIVE_VERSION
    ):
        raise ValueError
    observer = _handle(capture.get("observer"))
    target_id = capture.get("target_id")
    if (
        not observer
        or not isinstance(target_id, str)
        or not re.fullmatch(r"[1-9][0-9]{0,29}", target_id)
    ):
        raise ValueError
    started = datetime.fromisoformat(capture["started_at"])
    finished = datetime.fromisoformat(capture["finished_at"])
    if started.tzinfo is None or finished.tzinfo is None or started > finished:
        raise ValueError
    comparable = {}
    for direction in DIRECTIONS:
        roster = capture[direction]
        members = normalize_members(roster["members"])
        validate_endpoint_coverage(roster, len(members))
        if roster["complete"] or roster["reason"] == "count_mismatch":
            comparable[direction] = members
    return (observer, target_id), started, finished, comparable


def _read_baseline(path, target):
    try:
        path = path.expanduser()
        if not path.is_file():
            raise ValueError
        with path.open("rb") as source:
            # A standalone capture may also contain additions from an earlier run.
            raw = source.read(MAX_CAPTURE_BYTES + 1)
        if len(raw) > MAX_CAPTURE_BYTES:
            raise ValueError
        capture = json.loads(raw)
        details = _capture_details(capture, target)
        _, _, finished, _ = details
        if finished > datetime.now(UTC):
            raise ValueError
    except OSError, ValueError, TypeError, KeyError, RecursionError:
        raise BaselineError("Choose a valid earlier capture of this target.") from None
    return capture, details, sha256(raw).hexdigest()


def _compare(baseline, observation, target):
    capture, (identity, _, finished, previous), digest = baseline
    try:
        current_identity, started, _, current = _capture_details(observation, target)
    except ValueError, TypeError, KeyError:
        raise BaselineError("The new capture could not be compared.") from None
    if current_identity != identity or started < finished:
        raise BaselineError("The new capture does not match the earlier baseline.")
    return {
        "baseline_sha256": digest,
        "baseline_started_at": capture["started_at"],
        "baseline_finished_at": capture["finished_at"],
        **{
            direction: {
                "baseline_reason": capture[direction]["reason"],
                "current_reason": observation[direction]["reason"],
                "additions": roster_additions(previous[direction], current[direction])
                if direction in previous and direction in current
                else None,
            }
            for direction in DIRECTIONS
        },
    }


def _confirm(browser):
    print(
        "Press Enter here after signing in to Instagram in the private window.",
        file=sys.stderr,
        flush=True,
    )
    while monotonic() < browser.expires:
        if select.select([sys.stdin], [], [], 0)[0]:
            if os.read(sys.stdin.fileno(), 1) != b"\n":
                raise LiveBrowserError("cancelled")
            return
        # Service the browser's network routing callbacks while the person signs
        # in. Blocking on terminal input would stall the interactive window.
        browser.page.wait_for_timeout(200)
    raise LiveBrowserError("pairing_expired")


def _collect(driver, target, *, expected_observer=None):
    browser = LiveBrowser(driver, target)
    try:
        browser.start_sign_in()
        while True:
            _confirm(browser)
            try:
                observer = browser.confirm_sign_in()
                break
            except LiveBrowserError as error:
                if error.code != "pairing_pending":
                    raise
                print(
                    "The signed-in profile could not yet be identified.",
                    file=sys.stderr,
                )
        if expected_observer and observer.lower() != expected_observer:
            raise BaselineError(
                "Sign in with the observer used for the earlier capture."
            )
        started = datetime.now(UTC)
        observation = browser.collect()
        return {
            **observation,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
        }
    finally:
        browser.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target", required=True, help="Single approved Instagram handle."
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="New private JSON file."
    )
    parser.add_argument(
        "--baseline", type=Path, help="Compare with an earlier capture."
    )
    args = parser.parse_args(argv)
    target = _handle(args.target.strip().removeprefix("@"))
    if not target:
        parser.error("Supply a single Instagram handle.")
    try:
        output = args.output.expanduser()
        directory = output.parent.resolve(strict=True)
        output = directory / output.name
        checkout = Path(__file__).resolve().parents[1]
        if directory.is_relative_to(checkout) and not directory.is_relative_to(
            checkout / "data"
        ):
            parser.error(
                "Choose the ignored data directory or a path outside the checkout."
            )
        if os.path.lexists(output):
            raise FileExistsError
        baseline = _read_baseline(args.baseline, target) if args.baseline else None
        expected_observer = baseline[0]["observer"].lower() if baseline else None
        # Stage in the destination directory. The hard link publishes the complete
        # 0600 file atomically and refuses a competing writer's destination.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory
        ) as staged:
            os.fchmod(staged.fileno(), 0o600)
            with sync_playwright() as driver:
                observation = _collect(
                    driver, target, expected_observer=expected_observer
                )
            if baseline:
                observation["comparison"] = _compare(baseline, observation, target)
            json.dump(observation, staged, ensure_ascii=False)
            staged.write("\n")
            staged.flush()
            os.fsync(staged.fileno())
            os.link(staged.name, output)
    except FileExistsError:
        print("Output already exists. Choose a new file.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Qualification interrupted.", file=sys.stderr)
        return 130
    except BaselineError as error:
        print(error, file=sys.stderr)
        return 1
    except LiveBrowserError as error:
        message = {
            "pairing_expired": "Sign-in confirmation expired.",
            "cancelled": "Sign-in confirmation cancelled.",
            "challenge": "Instagram requires account verification.",
            "cooldown": "Instagram requested a cooldown.",
            "access_lost": "The signed-in account could not access this target.",
        }.get(error.code, "The private browser could not complete the observation.")
        print(message, file=sys.stderr)
        return 1
    except BrowserError, OSError, ValueError:
        print("The private browser or output file was unavailable.", file=sys.stderr)
        return 1
    complete = True
    for direction in DIRECTIONS:
        roster = observation[direction]
        complete = complete and roster["complete"]
        coverage = "complete" if roster["complete"] else "partial"
        summary = f"{direction}: {len(roster['members'])} observed, {coverage}"
        summary += f", pages read {roster['page_count']}"
        if roster["expected_count"] is not None:
            summary += f", profile count {roster['expected_count']}"
        if not roster["complete"]:
            summary += f" ({roster['reason']})"
        if baseline:
            comparison = observation["comparison"][direction]
            additions = comparison["additions"]
            summary += (
                ", comparison skipped"
                if additions is None
                else f", {len(additions)} newly observed"
            )
            if (
                additions is not None
                and comparison["baseline_reason"] == "count_mismatch"
            ):
                summary += ", baseline count mismatch"
        print(summary)
    return 0 if complete else 3


if __name__ == "__main__":
    sys.exit(main())
