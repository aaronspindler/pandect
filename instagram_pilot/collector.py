"""Exercise list traversal against generated pages in a network-isolated browser."""

import json
from time import monotonic, time

from playwright.sync_api import TimeoutError as BrowserTimeout
from playwright.sync_api import sync_playwright

TARGET = "demo.account"
VERSION = "synthetic-browser-v1"
MAX_MEMBERS = 10_000
# Realistic fixture behaviors, including a duplicate across page boundaries and
# delayed replacement of virtualized rows. No platform content or credentials.
PAGE = """<!DOCTYPE html><html lang="en"><meta charset="utf-8"><title>Pilot list</title>
<style>#list{height:85px;overflow:auto} a{display:block;height:55px}</style>
<div id="list" aria-label="Accounts"></div><button id="more">Load more</button>
<p id="end" hidden>End of list</p><script>(() => {
const pages = ROWS, panel = document.querySelector('#list');
const more = document.querySelector('#more');
const end = document.querySelector('#end');
let index = 0, busy = false;
function render() {
  panel.replaceChildren();
  for (const [handle, id] of pages[index]) {
    const a = document.createElement('a');
    a.textContent = handle; a.dataset.handle = handle; a.dataset.id = id;
    panel.append(a);
  }
  panel.scrollTop = 0;
  end.hidden = !(index === pages.length-1 && FINISH);
  more.hidden = SCROLL || !end.hidden;
  busy = false;
}
function advance() {
  if (busy || index === pages.length-1) return;
  busy = true; setTimeout(() => {index++; render()}, 100);
}
more.onclick = advance;
panel.onscroll = () => {
  if (SCROLL && panel.scrollTop+panel.clientHeight >= panel.scrollHeight-1) advance();
};
render();
})();</script></html>"""


def synthetic_page(pages, *, scroll, finish=True):
    return (
        PAGE.replace("ROWS", json.dumps(pages).replace("<", r"\u003c"))
        .replace("SCROLL", json.dumps(scroll))
        .replace("FINISH", json.dumps(finish))
    )


# Signatures include the actual rows so same-length virtualized pages are detected.
READ = """() => ({
    rows: Array.from(document.querySelectorAll('#list [data-handle]'), e =>
        [e.dataset.handle, e.dataset.id]),
    end: document.querySelector('#end')?.hidden === false
})"""


def read_list(page, expected, *, budget=12, wait_ms=1500, max_members=MAX_MEMBERS):
    seen = {}
    deadline = monotonic() + budget
    reason = "time_limit"

    def remaining_ms():
        return max(1, min(wait_ms, int(1000 * (deadline - monotonic()))))

    while monotonic() < deadline:
        state = page.evaluate(READ)
        for handle, identity in state["rows"]:
            key = identity or handle.lower()
            if key not in seen and len(seen) >= max_members:
                return {
                    "members": list(seen.values()),
                    "complete": False,
                    "reason": "row_limit",
                }
            seen[key] = {"handle": handle, "id": identity}
        if state["end"]:
            reason = "" if len(seen) == expected else "count_mismatch"
            break
        try:
            more = page.get_by_role("button", name="Load more")
            if more.is_visible():
                more.click(timeout=remaining_ms())
            else:
                page.locator("#list").evaluate(
                    "el => { el.scrollTop = el.scrollHeight; }", timeout=remaining_ms()
                )
            page.wait_for_function(
                f"before => JSON.stringify(({READ})()) !== JSON.stringify(before)",
                arg=state,
                timeout=remaining_ms(),
            )
        except BrowserTimeout:
            reason = "stalled" if monotonic() < deadline else "time_limit"
            break
    return {"members": list(seen.values()), "complete": not reason, "reason": reason}


def collect():
    # One synthetic member in each direction changes on the hour. Totals stay
    # constant, so a comparison driven by counts would miss both additions.
    epoch = int(time() // 3600)
    result = {"target": TARGET, "target_id": "demo", "collector_version": VERSION}
    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        try:
            context = browser.new_context(offline=True, service_workers="block")
            context.route("**/*", lambda route: route.abort())
            page = context.new_page()
            for direction in ("followers", "following"):
                first = [(f"{direction}.one", "1"), (f"{direction}.two", "2")]
                member = (f"{direction}.{epoch}", str(epoch))
                pages = [first, [first[-1], member]]
                page.set_content(synthetic_page(pages, scroll=direction == "following"))
                result[direction] = read_list(page, 3)
        finally:
            browser.close()
    return result
