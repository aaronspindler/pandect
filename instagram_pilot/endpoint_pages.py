"""Read pages of a signed-in Instagram relationship list."""

import re
from time import monotonic

from instagram_pilot.live_roster import HANDLE, MAX_MEMBERS, LiveBrowserError
from socialmedia.instagram_rosters import MAX_PAGES

ACCOUNT_ID = re.compile(r"[1-9][0-9]{0,127}", re.ASCII)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class EndpointPageUnavailable(Exception):
    """A transient failure reading one page of an authorized roster."""


def validate_api_status(data):
    """Stop collection when Instagram declines a request in its JSON response."""
    message = data.get("message")
    if message in ("login_required", "checkpoint_required", "challenge_required"):
        raise LiveBrowserError("challenge")
    if data.get("feedback_required") or message in ("feedback_required", "cooldown"):
        raise LiveBrowserError("cooldown")
    if data.get("status") == "fail":
        raise LiveBrowserError("unavailable")


def _member(row):
    if not isinstance(row, dict):
        raise ValueError
    handle = row.get("username")
    if (
        not isinstance(handle, str)
        or not HANDLE.fullmatch(handle)
        or handle in {".", ".."}
    ):
        raise ValueError
    identifiers = set()
    for field in ("pk", "pk_id", "id"):
        value = row.get(field)
        if value is None or value == "":
            continue
        if type(value) is int and 0 < value < 10**128:
            value = str(value)
        if not isinstance(value, str) or not ACCOUNT_ID.fullmatch(value):
            raise ValueError
        identifiers.add(value)
    if len(identifiers) > 1:
        raise ValueError
    return handle.lower(), next(iter(identifiers), "")


def read_endpoint_pages(
    fetch_page, *, expected_count, deadline, before_load, initial_page=None
):
    """Fetch until the endpoint declares an end, checking any exact profile count."""
    members = {}
    handles_by_id = {}
    seen_cursors = set()
    cursor = None
    pages = 0

    def result(reason):
        return {
            "members": [
                {"handle": handle, "id": members[handle]} for handle in sorted(members)
            ],
            "complete": reason == "",
            "reason": reason,
            "page_count": pages,
            "expected_count": expected_count,
        }

    while pages < MAX_PAGES:
        if monotonic() >= deadline:
            return result("time_limit")
        if pages == 0 and initial_page is not None:
            page = initial_page
        else:
            if not before_load(deadline) or monotonic() >= deadline:
                return result("time_limit")
            try:
                page = fetch_page(cursor)
            except EndpointPageUnavailable:
                return result("unavailable")
        pages += 1
        if monotonic() >= deadline:
            return result("time_limit")
        if not isinstance(page, dict):
            return result("unavailable")
        validate_api_status(page)
        if page.get("status") != "ok":
            return result("unavailable")
        rows = page.get("users")
        if not isinstance(rows, list):
            return result("unavailable")
        if len(rows) > MAX_MEMBERS:
            return result("row_limit")
        for row in rows:
            try:
                handle, identifier = _member(row)
            except ValueError:
                return result("unavailable")
            previous_id = members.get(handle, "")
            if identifier and (
                (previous_id and previous_id != identifier)
                or handles_by_id.get(identifier, handle) != handle
            ):
                return result("unavailable")
            if handle not in members and len(members) >= MAX_MEMBERS:
                return result("row_limit")
            members[handle] = identifier or previous_id
            if identifier:
                handles_by_id[identifier] = handle

        more = page.get("has_more")
        next_cursor = page.get("next_max_id")
        if more is False and next_cursor in (None, ""):
            if expected_count is not None and len(members) != expected_count:
                return result("count_mismatch")
            return result("")
        if more is not True:
            return result("ambiguous_end")
        if type(next_cursor) is int and 0 <= next_cursor < 10**128:
            next_cursor = str(next_cursor)
        if (
            not isinstance(next_cursor, str)
            or not 0 < len(next_cursor) <= 2048
            or not next_cursor.isascii()
            or not next_cursor.isprintable()
            or not next_cursor.strip()
        ):
            return result("ambiguous_end")
        if next_cursor in seen_cursors:
            return result("stalled")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return result("row_limit")
