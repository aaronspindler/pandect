"""Read the two website roster endpoints in the investigator's signed-in page."""

import json
import logging
import re
from functools import partial
from time import monotonic
from urllib.parse import urlencode

from playwright.sync_api import Error as BrowserError

from instagram_pilot.endpoint_pages import (
    MAX_RESPONSE_BYTES,
    EndpointPageUnavailable,
    read_endpoint_pages,
    validate_api_status,
)
from instagram_pilot.endpoint_profile import resolve_profile
from instagram_pilot.live_roster import (
    LiveBrowserError,
    _handle,
    _official_path,
    visible_observer,
)
from socialmedia.instagram_connector import LIVE_VERSION

logger = logging.getLogger(__name__)

# Keep the authenticated request in its original browser origin. Read the bounded
# JSON as text so large numeric account IDs reach Python without loss of precision.
FETCH = """async ({path, profile, limit, timeout, appId}) => {
  const here = location.pathname.toLowerCase().replace(/\\/$/, '');
  if (location.origin !== 'https://www.instagram.com' || here !== profile)
    return {code:403};
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), timeout);
  try {
    const headers = {Accept:'application/json'};
    if (appId) headers['X-IG-App-ID'] = appId;
    const response = await fetch(path, {method:'GET', mode:'same-origin',
      credentials:'same-origin', redirect:'manual', cache:'no-store',
      headers, signal:abort.signal});
    const code = response.status;
    if (!response.ok && code !== 400) return {code};
    const contentType = (response.headers.get('content-type') || '').toLowerCase();
    if (!contentType.includes('application/json')) return {code, unexpectedType:true};
    if (!response.body || Number(response.headers.get('content-length')) > limit)
      return {code:response.ok ? 502 : code};
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8', {fatal:true});
    let size = 0;
    let text = '';
    while (true) {
      const {done,value} = await reader.read();
      if (done) return {code, text:text + decoder.decode()};
      size += value.byteLength;
      if (size > limit) { abort.abort(); return {code:502}; }
      text += decoder.decode(value, {stream:true});
    }
  } finally { clearTimeout(timer); }
}"""


def _verify_page(page, target, observer):
    if _official_path(page) != "/" + target or visible_observer(page) != observer:
        raise LiveBrowserError("challenge")


def _fetch_page(page, target, observer, base, options, app_id, deadline, cursor):
    _verify_page(page, target, observer)
    query = options.copy()
    if cursor is not None:
        query["max_id"] = cursor
    path = f"{base}?{urlencode(query)}" if query else base
    try:
        response = page.evaluate(
            FETCH,
            {
                "path": path,
                "profile": "/" + target,
                "appId": app_id,
                "limit": MAX_RESPONSE_BYTES,
                "timeout": max(1, min(15000, int(1000 * (deadline - monotonic())))),
            },
        )
    except BrowserError:
        _verify_page(page, target, observer)
        raise EndpointPageUnavailable from None
    status = response["code"]
    if response.get("unexpectedType"):
        raise LiveBrowserError("unavailable")
    if status == 429:
        raise LiveBrowserError("cooldown")
    if status in {0, 401, 403}:
        raise LiveBrowserError("challenge")
    if status == 404:
        raise LiveBrowserError("access_lost")
    if status >= 500:
        raise EndpointPageUnavailable
    if status not in {200, 400} or "text" not in response:
        logger.warning("Instagram roster request status: %d.", status)
        raise LiveBrowserError("unavailable")
    try:
        data = json.loads(response["text"])
    except ValueError, KeyError, RecursionError:
        if status == 400:
            raise LiveBrowserError("unavailable") from None
        raise EndpointPageUnavailable from None
    if not isinstance(data, dict):
        if status == 400:
            raise LiveBrowserError("unavailable")
        raise EndpointPageUnavailable
    validate_api_status(data)
    if status == 400:
        logger.warning("Instagram declined the roster request (HTTP %d).", status)
        raise LiveBrowserError("unavailable")
    return data


def collect_rosters(page, target, observer, *, deadline, before_load):
    target, observer = _handle(target), _handle(observer)
    if not target or not observer:
        raise LiveBrowserError("access_lost")
    try:
        account_id, counts, options, app_id, first_followers = resolve_profile(
            page, target, observer, deadline=deadline, before_load=before_load
        )
        if not re.fullmatch(r"[1-9][0-9]{0,29}", account_id):
            raise LiveBrowserError("access_lost")
        capture = {
            "source": "instagram",
            "collector_version": LIVE_VERSION,
            "observer": observer,
            "target": target,
            "target_id": account_id,
        }
        for direction in ("followers", "following"):
            now = monotonic()
            until = (deadline + now) / 2 if direction == "followers" else deadline
            base = f"/api/v1/friendships/{account_id}/{direction}/"
            fetch = partial(
                _fetch_page, page, target, observer, base, options, app_id, until
            )
            capture[direction] = read_endpoint_pages(
                fetch,
                expected_count=counts[direction],
                deadline=until,
                before_load=before_load,
                initial_page=first_followers if direction == "followers" else None,
            )
        _verify_page(page, target, observer)
        return capture
    except BrowserError:
        logger.warning("The Instagram page could not finish the roster request.")
        raise LiveBrowserError("unavailable") from None
