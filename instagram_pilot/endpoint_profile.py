"""Resolve the approved profile before reading its signed-in roster endpoints."""

import json
import logging
import re
from time import monotonic
from urllib.parse import parse_qsl, urlsplit

from playwright.sync_api import Error as BrowserError

from instagram_pilot.endpoint_pages import MAX_RESPONSE_BYTES, validate_api_status
from instagram_pilot.live_roster import (
    DOM,
    LiveBrowserError,
    _exact_count,
    _handle,
    _may_load,
    _official_path,
    _timeout,
    _wait,
)

PROFILE_METADATA = r"""target => {
  const ids = new Set();
  const head = document.head;
  const canonicalLinks = 'link[rel=canonical],meta[property="og:url"]';
  for (const e of head.querySelectorAll(canonicalLinks)) {
    const url = URL.parse(e.href || e.content);
    if (!url || url.protocol !== 'https:' || url.username || url.password ||
        !['instagram.com','www.instagram.com'].includes(url.host) ||
        url.pathname.replace(/\/$/, '').toLowerCase() !== '/' + target)
      return {conflict:true};
  }
  for (const e of head.querySelectorAll('meta[property="instapp:owner_user_id"]'))
    ids.add(e.content);
  const appLinks = 'meta[property="al:ios:url"],meta[property="al:android:url"]';
  for (const e of head.querySelectorAll(appLinks)) {
    const app = URL.parse(e.content);
    if (!app || app.protocol !== 'instagram:' || app.host !== 'user' ||
        !['','/'].includes(app.pathname)) continue;
    const name = app.searchParams.get('username');
    if (name && name.toLowerCase() !== target) return {conflict:true};
    for (const id of app.searchParams.getAll('id')) ids.add(id);
  }
  if (ids.size > 1 || [...ids].some(id => !/^[1-9][0-9]{0,29}$/.test(id)))
    return {conflict:true};
  return {id:[...ids][0] || ''};
}"""


def _profile_counts(page, target, observer, deadline):
    until = min(deadline, monotonic() + 6)
    while True:
        if _official_path(page) != "/" + target:
            raise LiveBrowserError("access_lost")
        counts = {}
        for direction in ("followers", "following"):
            state = page.evaluate(
                DOM, {"target": target, "direction": direction, "action": "profile"}
            )
            if state.get("error"):
                raise LiveBrowserError(state["error"])
            if state["observer"] and state["observer"] != observer:
                raise LiveBrowserError("challenge")
            if state["inaccessible"]:
                raise LiveBrowserError("access_lost")
            if state["targetVerified"] and state["available"] and state["observer"]:
                counts[direction] = _exact_count(state["label"], direction)
        if len(counts) == 2:
            return counts
        if monotonic() >= until:
            raise LiveBrowserError(
                "challenge" if not state["observer"] else "access_lost"
            )
        _wait(page, until)


def _permit(before_load, deadline):
    if not _may_load(before_load, deadline):
        raise LiveBrowserError("time_limit")


def _check_session(page, observer, *, roster_target=None):
    path = _official_path(page)
    state = page.evaluate(DOM, {"action": "observer"})
    if isinstance(state, dict):
        raise LiveBrowserError(state.get("error", "challenge"))
    if roster_target and path not in {
        f"/{roster_target}",
        f"/{roster_target}/followers",
    }:
        raise LiveBrowserError("access_lost")
    # Opening the roster may hide the profile navigation behind its modal.
    if state != observer and (state or not roster_target):
        raise LiveBrowserError("challenge")


def _verify_response(page, response, since, deadline):
    media_type = (response.header_value("content-type") or "").partition(";")[0]
    if media_type.strip().lower() != "application/json":
        raise LiveBrowserError("unavailable")
    # Playwright exposes the decoded body at once. The same-origin resource
    # timing record gives its final *decoded* size, including for gzip or br.
    size = page.wait_for_function(
        """({url,since}) => {
          const rows = performance.getEntriesByName(url, 'resource').filter(
            r => r.startTime >= since &&
              ['fetch','xmlhttprequest'].includes(r.initiatorType)
          );
          if (!rows.length) return false;
          return rows.length === 1 && rows[0].decodedBodySize || -1;
        }""",
        arg={"url": response.url, "since": since},
        timeout=_timeout(deadline),
    )
    try:
        if not 0 < size.json_value() <= MAX_RESPONSE_BYTES:
            raise LiveBrowserError("unavailable")
    finally:
        size.dispose()
    raw = response.body()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise LiveBrowserError("unavailable")
    try:
        data = json.loads(raw)
    except ValueError, RecursionError:
        raise LiveBrowserError("unavailable") from None
    if not isinstance(data, dict):
        raise LiveBrowserError("unavailable")
    validate_api_status(data)
    if data.get("status") != "ok" or not isinstance(data.get("users"), list):
        raise LiveBrowserError("unavailable")
    return data


def _first_page_options(query):
    """Accept only a plain, unfiltered first page of the profile roster."""
    try:
        pairs = parse_qsl(
            query, keep_blank_values=True, strict_parsing=True, max_num_fields=3
        )
    except ValueError:
        return None
    options = dict(pairs)
    if len(options) != len(pairs) or options.keys() - {
        "count",
        "query",
        "search_surface",
    }:
        return None
    if (
        options.get("query", "")
        or options.get("search_surface", "follow_list_page") != "follow_list_page"
    ):
        return None
    if "count" in options and not re.fullmatch(
        r"(?:[1-9][0-9]?|100)", options["count"]
    ):
        return None
    return options


def resolve_profile(page, target, observer, *, deadline, before_load):
    target, observer = _handle(target), _handle(observer)
    if not target or not observer:
        raise LiveBrowserError("access_lost")
    url = f"https://www.instagram.com/{target}/"
    try:
        _permit(before_load, deadline)
        _check_session(page, observer)
        page.goto(url, wait_until="domcontentloaded", timeout=_timeout(deadline))
        _permit(before_load, deadline)
        _profile_counts(page, target, observer, deadline)
        meta = page.evaluate(PROFILE_METADATA, target)
        if meta.get("conflict"):
            raise LiveBrowserError("access_lost")
        origin = urlsplit(page.url).netloc
        path = re.compile(r"/api/v1/friendships/([1-9][0-9]{0,29})/followers/")

        requests = {}

        def watch(request):
            address = urlsplit(request.url)
            match = path.fullmatch(address.path)
            if (
                address.scheme == "https"
                and address.netloc == origin
                and match
                and request.method == "GET"
                and request.resource_type in {"xhr", "fetch"}
                and request.frame == page.main_frame
            ):
                options = _first_page_options(address.query)
                if options is not None and (not meta["id"] or match[1] == meta["id"]):
                    requests[request] = (match[1], options)

        control = page.evaluate_handle(
            DOM, {"action": "open", "target": target, "direction": "followers"}
        )
        page.on("request", watch)
        try:
            link = control.as_element()
            if link is None:
                raise LiveBrowserError("access_lost")
            since = page.evaluate("performance.now()")
            with page.expect_request_finished(
                lambda req: req in requests, timeout=_timeout(deadline)
            ) as event:
                link.click(timeout=_timeout(deadline))
        finally:
            page.remove_listener("request", watch)
            control.dispose()
        request = event.value
        response = request.response()
        if response is None:
            raise LiveBrowserError("unavailable")
        if response.status != 200:
            status = {401: "challenge", 403: "access_lost", 429: "cooldown"}
            raise LiveBrowserError(status.get(response.status, "unavailable"))
        owner_id, options = requests[request]
        if any(candidate_id != owner_id for candidate_id, _ in requests.values()):
            raise LiveBrowserError("access_lost")
        if sum(req.url == response.url for req in requests) != 1:
            raise LiveBrowserError("unavailable")
        first_page = _verify_response(page, response, since, deadline)
        app_id = request.header_value("x-ig-app-id") or ""
        if app_id and not re.fullmatch(r"[1-9][0-9]{0,29}", app_id):
            raise LiveBrowserError("unavailable")
        _permit(before_load, deadline)
        _check_session(page, observer, roster_target=target)
        page.goto(url, wait_until="domcontentloaded", timeout=_timeout(deadline))
        counts = _profile_counts(page, target, observer, deadline)
        return owner_id, counts, options, app_id, first_page
    except BrowserError:
        logging.getLogger(__name__).warning(
            "The browser could not resolve the Instagram profile for this request."
        )
        raise LiveBrowserError("unavailable") from None
