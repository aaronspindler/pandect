"""Validate and compare account rosters collected from Instagram."""

import re

LIVE_VERSION = "instagram-endpoints-v1"
HANDLE = re.compile(r"[A-Za-z0-9._]{1,30}", re.ASCII)
IDENTIFIER = re.compile(r"[A-Za-z0-9:._-]{1,128}", re.ASCII)
MAX_MEMBERS = 10_000
MAX_PAGES = 200


def validate_handle(value):
    if (
        not isinstance(value, str)
        or not HANDLE.fullmatch(value)
        or value in {".", ".."}
    ):
        raise ValueError("invalid_observation")
    return value


def validate_identifier(value):
    if value == "" or value is None:
        return ""
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("invalid_observation")
    return value


def normalize_members(rows):
    if not isinstance(rows, list) or len(rows) > MAX_MEMBERS:
        raise ValueError("invalid_observation")
    entries = {}
    handles_by_id = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid_observation")
        handle = validate_handle(row.get("handle"))
        key = handle.lower()
        identifier = validate_identifier(row.get("id"))
        previous_id = entries.get(key, {}).get("id", "")
        if identifier:
            if previous_id and identifier != previous_id:
                raise ValueError("inconsistent_observation")
            if handles_by_id.setdefault(identifier, key) != key:
                raise ValueError("inconsistent_observation")
        entries[key] = {"handle": handle, "id": identifier or previous_id}
    return [entries[key] for key in sorted(entries)]


def validate_endpoint_coverage(roster, entry_count):
    """Validate the page and count claims of an endpoint capture."""
    complete, reason = roster.get("complete"), roster.get("reason")
    pages, expected = roster.get("page_count"), roster.get("expected_count")
    if (
        type(complete) is not bool
        or not isinstance(reason, str)
        or complete != (reason == "")
        or type(pages) is not int
        or not 0 <= pages <= MAX_PAGES
        or (
            expected is not None
            and (type(expected) is not int or not 0 <= expected < 10**12)
        )
    ):
        raise ValueError("invalid_observation")
    if complete or reason == "count_mismatch":
        count_differs = expected is not None and expected != entry_count
        if not pages or count_differs != (reason == "count_mismatch"):
            raise ValueError("invalid_observation")
    return pages, expected


def roster_additions(prior, current):
    known_ids = {entry["id"] for entry in prior if entry["id"]}
    prior_handles = {entry["handle"].lower(): entry["id"] for entry in prior}
    incomplete_ids = any(not value for value in prior_handles.values())
    additions = []
    for entry in current:
        identifier = entry["id"]
        if identifier and identifier in known_ids:
            continue
        old_id = prior_handles.get(entry["handle"].lower())
        if old_id is not None and (not old_id or not identifier):
            continue
        additions.append({**entry, "handle_only": not identifier or incomplete_ids})
    return additions
