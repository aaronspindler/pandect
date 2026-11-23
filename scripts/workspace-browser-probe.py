"""Measure actual browser updates through the disposable Compose smoke proxy."""

import asyncio
import json
import math
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import async_playwright

TABS = 30

# Each tab gets its own browser context (as on separate investigator devices).
# Wait for the stream to open, then timestamp changes to the visible badge.
OBSERVE = """
window.EventSource = class extends EventSource {
  constructor(...args) {
    super(...args);
    this.addEventListener('open', () => {
      document.documentElement.dataset.streamReady = 'true';
    });
  }
};
window.badgeTimes = {};
document.addEventListener('DOMContentLoaded', () => {
  const badge = document.querySelector('#processing-activity-count');
  if (badge) new MutationObserver(() => {
    const state = badge.hidden ? 'finished' : badge.textContent;
    window.badgeTimes[state] = Date.now();
    badge.dataset.probeState = state;
  }).observe(badge, {attributeFilter: ['hidden'], childList: true, subtree: true});
});
"""


def fixture(compose, code):
    # Prevent fixture creation on any independently operated installation.
    guard = (
        "from django.conf import settings\n"
        "assert settings.DATABASES['default']['NAME'] == 'pandect_smoke'\n"
    )
    result = subprocess.run(
        [*compose, "exec", "-T", "web", "python", "manage.py", "shell", "-v", "0"],
        input=guard + code,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout.strip())


async def check(project, compose, origin):
    if not project.startswith("pandect-smoke-") or urlsplit(origin).hostname not in {
        "127.0.0.1",
        "localhost",
    }:
        raise ValueError("Use the isolated compose-smoke project and loopback proxy.")
    session, case_id, case_path, upload_path = fixture(
        compose,
        """
import json
from django.test import Client
from django.urls import reverse
from cases.models import Case
from accounts.models import User
user = User.objects.get(username='smoke')
case = Case.objects.create(owner=user, name='Workspace stream probe')
uploads = Case.objects.create(owner=user, name='Workspace upload probe')
client = Client()
client.force_login(user)
print(json.dumps([client.session.session_key, str(case.pk),
    reverse('core:evidence:list', args=[case.pk]),
    reverse('core:evidence:create', args=[uploads.pk])]))
""",
    )
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        tabs = []
        status_reads = []
        for _ in range(TABS):
            ctx = await browser.new_context(base_url=origin, timezone_id="UTC")
            await ctx.add_cookies(
                [{"name": "sessionid", "value": session, "url": origin}]
            )
            await ctx.add_init_script(OBSERVE)
            page = await ctx.new_page()
            page.on(
                "request",
                lambda req: (
                    status_reads.append(1)
                    if urlsplit(req.url).path.endswith("/activity/")
                    else None
                ),
            )
            await page.goto(case_path)
            await page.locator("html[data-stream-ready]").wait_for(timeout=10000)
            tabs.append(page)

        # Exercise CSRF and real multipart upload while all thirty streams are open.
        client = tabs[0].context.request
        assert (await client.get(upload_path)).ok
        cookies = await tabs[0].context.cookies()
        csrf = next(
            cookie["value"] for cookie in cookies if cookie["name"] == "csrftoken"
        )
        upload = await client.post(
            upload_path,
            headers={"X-CSRFToken": csrf, "Accept": "application/json"},
            multipart={
                "kind": "document",
                "title": "Synthetic concurrent upload",
                "submission_id": str(uuid4()),
                "file": {
                    "name": "probe.txt",
                    "mimeType": "text/plain",
                    "buffer": b"Local synthetic workspace probe",
                },
            },
        )
        assert upload.status == 201, upload.status
        assert (await client.get(case_path)).ok

        samples = []
        for status, label in (
            ("queued", "1 queued"),
            ("running", "1 running"),
            ("complete", "finished"),
        ):
            for page in tabs:
                await page.evaluate("label => delete window.badgeTimes[label]", label)
            # Timestamp just before the autocommitted write, which includes DB time
            # in the measured commit-to-display latency.
            began = await asyncio.to_thread(
                fixture,
                compose,
                f"""
import json, time
from core.models import AnalysisRun
from cases.models import Case
case = Case.objects.get(pk='{case_id}')
began = time.time_ns() // 1_000_000
AnalysisRun.objects.update_or_create(case=case, operation='workspace_probe',
    defaults={{'requested_by_id': case.owner_id,
             'method_version': '1', 'status': '{status}'}})
print(json.dumps(began))
""",
            )
            await asyncio.gather(
                *(
                    page.locator(f'[data-probe-state="{label}"]').wait_for(
                        state="attached", timeout=15000
                    )
                    for page in tabs
                )
            )
            times = await asyncio.gather(
                *(
                    page.evaluate("label => window.badgeTimes[label]", label)
                    for page in tabs
                )
            )
            samples.extend(value - began for value in times)
            if status == "running":
                before = len(status_reads)
                await asyncio.sleep(6)
                assert len(status_reads) == before, "Healthy tabs continued polling."
        p95 = sorted(samples)[math.ceil(len(samples) * 0.95) - 1]
        print(
            f"Workspace probe: {TABS} browser tabs, {len(samples)} updates, "
            f"p95 {p95} ms, concurrent upload and idle polling check passed.",
            flush=True,
        )
        assert 0 <= p95 <= 2000, "Commit-to-display p95 exceeded two seconds."
        await browser.close()


if __name__ == "__main__":
    project, origin, *compose = sys.argv[1:]
    asyncio.run(check(project, compose, origin))
