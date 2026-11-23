"""Measure actual browser updates through the disposable Compose smoke proxy."""

import asyncio
import hashlib
import json
import math
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from http.client import HTTPConnection
from threading import Event
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from playwright.async_api import async_playwright

TABS = 30
MIB = 1024 * 1024
DOWNLOAD_BYTES = 96 * MIB
UPLOAD_BYTES = 32 * MIB
CHUNK = bytes(64 * 1024)

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


def worker_peak(compose):
    # Read only the app worker's resident memory high-water mark, excluding the
    # database, test fixture processes, kernel page cache, and the browser.
    command = """
from pathlib import Path
children = Path('/proc/1/task/1/children').read_text().split()
workers = [Path('/proc') / pid for pid in children
           if b'gunicorn' in (Path('/proc') / pid / 'cmdline').read_bytes()]
assert len(workers) == 1, 'Expected one Gunicorn web worker'
print(next(line.split()[1] for line in (workers[0] / 'status').read_text().splitlines()
           if line.startswith('VmHWM:')))
"""
    return (
        int(
            subprocess.check_output(
                [*compose, "exec", "-T", "web", "python", "-c", command]
            )
        )
        * 1024
    )


def reject_before_body(host, path):
    with closing(HTTPConnection(host, timeout=8)) as conn:
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", "multipart/form-data; boundary=probe")
        conn.putheader("Content-Length", str(UPLOAD_BYTES))
        conn.putheader("Accept", "application/json")
        conn.endheaders()
        # Waiting for a body here would time out. No file bytes are sent.
        assert conn.getresponse().status == 401


def upload_file(host, path, cookies, csrf, ready, finish):
    fields = {
        "kind": "document",
        "title": "Transfer under load",
        "submission_id": str(uuid4()),
    }
    prefix = (
        "".join(
            f'--probe\r\nContent-Disposition: form-data; name="{name}"\r\n'
            f"\r\n{value}\r\n"
            for name, value in fields.items()
        ).encode()
        + b'--probe\r\nContent-Disposition: form-data; name="file"; '
        b'filename="transfer.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'
    )
    suffix = b"\r\n--probe--\r\n"

    with closing(HTTPConnection(host, timeout=45)) as conn:
        conn.putrequest("POST", path)
        conn.putheader("Cookie", cookies)
        conn.putheader("Accept", "application/json")
        conn.putheader("X-CSRFToken", csrf)
        conn.putheader("Content-Type", "multipart/form-data; boundary=probe")
        conn.putheader("Content-Length", str(len(prefix) + UPLOAD_BYTES + len(suffix)))
        conn.endheaders(prefix)
        for _ in range(UPLOAD_BYTES // len(CHUNK)):
            conn.send(CHUNK)
            ready.set()
            finish.wait(0.08)
        conn.send(suffix)
        response = conn.getresponse()
        result = json.load(response)
        assert response.status == 201, (
            response.status,
            result.get("code"),
            result.get("errors"),
        )
        return result["id"]


def download_file(host, path, cookies, ready, finish):
    with closing(HTTPConnection(host, timeout=45)) as conn:
        conn.request("GET", path, headers={"Cookie": cookies})
        response = conn.getresponse()
        assert response.status == 200, response.status
        digest = hashlib.sha256()
        size = 0
        while data := response.read(len(CHUNK)):
            digest.update(data)
            size += len(data)
            ready.set()
            finish.wait(0.05)
        assert size == DOWNLOAD_BYTES
        return digest.hexdigest()


@contextmanager
def transferring(compose, origin, upload, download, session, csrf, expected_digest):
    host = urlsplit(origin).netloc
    cookies = f"sessionid={session}; csrftoken={csrf}"
    reject_before_body(host, upload)
    before = worker_peak(compose)
    finish = Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        sending, receiving = Event(), Event()
        uploaded = pool.submit(
            upload_file, host, upload, cookies, csrf, sending, finish
        )
        downloaded = pool.submit(
            download_file, host, download, cookies, receiving, finish
        )
        try:
            for ready, transfer in ((sending, uploaded), (receiving, downloaded)):
                if not ready.wait(15):
                    if transfer.done():
                        transfer.result()
                    raise AssertionError("Transfer did not start")
            yield
            assert not uploaded.done() and not downloaded.done(), (
                "Transfer finished before latency measurement"
            )
        finally:
            finish.set()
        assert downloaded.result() == expected_digest
        uploaded_id = UUID(uploaded.result())
    stored = fixture(
        compose,
        f"""
import json
from document.models import DocumentEvidence
doc = DocumentEvidence.objects.get(pk='{uploaded_id}')
print(json.dumps([doc.byte_size, doc.sha256]))
""",
    )
    digest = hashlib.sha256()
    for _ in range(UPLOAD_BYTES // len(CHUNK)):
        digest.update(CHUNK)
    assert stored == [UPLOAD_BYTES, digest.hexdigest()]
    growth = worker_peak(compose) - before
    assert growth < DOWNLOAD_BYTES // 2, f"Worker peak grew by {growth / MIB:.1f} MiB"
    print(
        f"Verified early upload rejection, {UPLOAD_BYTES // MIB} MiB upload, "
        f"{DOWNLOAD_BYTES // MIB} MiB download, "
        f"worker peak RSS growth {growth / MIB:.1f} MiB.",
        flush=True,
    )


async def check(project, compose, origin):
    if not project.startswith("pandect-smoke-") or urlsplit(origin).hostname not in {
        "127.0.0.1",
        "localhost",
    }:
        raise ValueError("Use the isolated compose-smoke project and loopback proxy.")
    case_id, case_path, upload_path, download_path, digest = fixture(
        compose,
        f"""
import json
from tempfile import TemporaryFile
from django.core.files import File
from document.models import DocumentEvidence
from django.urls import reverse
from cases.models import Case
from accounts.models import User
user = User.objects.get(username='smoke')
case = Case.objects.create(owner=user, name='Workspace stream probe')
uploads = Case.objects.create(owner=user, name='Workspace upload probe')
with TemporaryFile() as content:
    content.truncate({DOWNLOAD_BYTES})
    doc = DocumentEvidence.objects.create(case=uploads, uploaded_by=user,
        title='Download under load', file=File(content, name='download.bin'))
print(json.dumps([str(case.pk),
    reverse('core:evidence:list', args=[case.pk]),
    reverse('core:evidence:create', args=[uploads.pk]),
    reverse('core:evidence:download', args=[uploads.pk, 'document', doc.pk]),
    doc.sha256]))
""",
    )
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        login = await browser.new_context(base_url=origin)
        page = await login.new_page()
        await page.goto("/accounts/login/")
        await page.locator('[name="login"]').fill("smoke")
        await page.locator('[name="password"]').fill("synthetic-smoke-password")
        await page.get_by_role("button", name="Sign in").click()
        await page.wait_for_url("**/cases/")
        session = next(
            c["value"] for c in await login.cookies() if c["name"] == "sessionid"
        )
        await login.close()
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
        assert (await client.get(case_path)).ok
        with transferring(
            compose, origin, upload_path, download_path, session, csrf, digest
        ):
            await measure_updates(compose, case_id, tabs, status_reads)
        await browser.close()


async def measure_updates(compose, case_id, tabs, status_reads):
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
        f"p95 {p95} ms during file transfers, idle polling check passed.",
        flush=True,
    )
    assert 0 <= p95 <= 2000, "Commit-to-display p95 exceeded two seconds."


if __name__ == "__main__":
    project, origin, *compose = sys.argv[1:]
    asyncio.run(check(project, compose, origin))
