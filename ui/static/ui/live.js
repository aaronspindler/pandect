(() => {
  "use strict";
  const activity = document.querySelector("[data-case-activity-url]");
  if (!activity) return;
  const badge = activity.querySelector("#processing-activity-count");
  const controls = document.querySelector("[data-live-controls]");
  const feedback = controls || document.querySelector("[data-activity-feedback]");
  const message = feedback.querySelector("[data-live-message], [data-activity-message]");
  const retry = feedback.querySelector("[data-live-refresh], [data-activity-retry]");
  const access = feedback.querySelector("[data-live-access], [data-activity-access]");
  const regions = () => [...document.querySelectorAll("[data-live-region]")];
  const waiting = new Map();
  const localWork = () => regions().some(region => (waiting.get(region) || region).dataset.livePoll === "true");
  const discoversWork = Boolean(document.querySelector("#processing-activity, #evidence-findings"));
  const initial = document.getElementById("case-activity-versions");
  let versions = initial ? JSON.parse(initial.textContent) : null;
  let displayed = versions;
  let caseBusy = localWork();
  let timer, controller, failures = 0, stopped = false, forceNext = false;
  let fragmentDue = false;

  // A collection remains stable until its reader explicitly requests fresh results.
  const collection = document.querySelector("[data-collection-kind]");
  let notice, noticeStatus;
  if (collection) {
    notice = document.createElement("a");
    notice.className = "record-link";
    notice.textContent = "Refresh results";
    notice.dataset.refreshResults = "";
    notice.hidden = true;
    noticeStatus = document.createElement("span");
    noticeStatus.className = "visually-hidden";
    noticeStatus.setAttribute("role", "status");
    collection.before(noticeStatus, notice);
  }
  function showCollectionChange() {
    if (!notice || !displayed || !versions) return;
    const kind = collection.dataset.collectionKind;
    const keys = kind === "case-tools" ? ["evidence", "findings"] : [kind];
    notice.href = location.href;
    const upToDate = keys.every(key => versions[key] <= displayed[key]);
    if (notice.hidden === upToDate) return;
    if (upToDate && document.activeElement === notice) {
      if (!collection.hasAttribute("tabindex")) collection.tabIndex = -1;
      collection.focus({preventScroll: true});
    }
    notice.hidden = upToDate;
    noticeStatus.textContent = upToDate ? "" : "Updated results are available.";
  }
  document.addEventListener("pandect:collection-refreshed", event => {
    if (event.detail?.versions) {
      // The collection response can be newer than the most recent heartbeat.
      displayed = event.detail.versions;
      showCollectionChange();
    }
  });

  function announce(text) {
    if (message.textContent !== text) message.textContent = text;
  }
  function schedule() {
    clearTimeout(timer);
    if (!stopped && !document.hidden) {
      const delay = caseBusy || localWork() ? 5000 : 30000;
      timer = setTimeout(refresh, forceNext ? 0 : delay * 2 ** failures);
    }
  }
  function checkAccess(response) {
    if (response.type === "opaqueredirect" || [401, 403, 404].includes(response.status)) {
      stopped = true;
      waiting.clear();
      badge.hidden = true;
      if (notice) notice.hidden = true;
      feedback.hidden = false;
      access.hidden = false;
      if (!controls) retry.hidden = true;
      announce("Automatic updates stopped. Your session or access may have changed.");
      return false;
    }
    if (!response.ok) throw Error("Update unavailable");
    return true;
  }
  function updateBadge(snapshot) {
    const parts = [];
    if (snapshot.queued) parts.push(snapshot.queued + " queued");
    if (snapshot.running) parts.push(snapshot.running + " running");
    badge.textContent = parts.join(" · ") || (snapshot.active ? "Active" : "");
    badge.hidden = !snapshot.active;
  }
  const disclosureKey = details => details.id || details.querySelector(":scope > summary")?.textContent.trim();
  function applyWaiting() {
    for (const [region, fresh] of waiting) {
      if (region.contains(document.activeElement) || region.querySelector("form[data-live-dirty]")) continue;
      const disclosures = new Map([...region.querySelectorAll("details")].map(item => [disclosureKey(item), item.open]));
      for (const item of fresh.querySelectorAll("details")) {
        const key = disclosureKey(item);
        if (disclosures.has(key)) item.open = disclosures.get(key);
      }
      fresh.querySelectorAll('[role="status"]').forEach(item => item.setAttribute("aria-live", "off"));
      region.replaceChildren(...fresh.childNodes);
      region.dataset.livePoll = fresh.dataset.livePoll;
      waiting.delete(region);
    }
    if (controls) announce(waiting.size ? "Updates are waiting while you use this section." :
      localWork() ? "Progress updates automatically." : "Up to date.");
  }
  async function refresh() {
    clearTimeout(timer);
    if (controller || document.hidden || stopped) return;
    const forced = forceNext;
    forceNext = false;
    controller = new AbortController();
    const timeout = setTimeout(() => controller?.abort(), 15000);
    const options = {credentials: "same-origin", cache: "no-store", redirect: "manual", signal: controller.signal};
    try {
      const response = await fetch(activity.dataset.caseActivityUrl, {...options, headers: {Accept: "application/json"}});
      if (!checkAccess(response)) return;
      const snapshot = await response.json();
      const changed = !versions || versions.work !== snapshot.versions.work;
      caseBusy = snapshot.active;
      updateBadge(snapshot);
      // Use the page URL including current filters and pagination.
      const progressChanged = changed && (discoversWork || localWork());
      if ((controls || discoversWork) && (forced || fragmentDue || progressChanged)) {
        fragmentDue = true;
        const requestedUrl = location.href;
        const fragment = await fetch(requestedUrl, {...options, headers: {"X-Pandect-Live": "1", Accept: "text/html"}});
        if (!checkAccess(fragment)) return;
        if (fragment.headers.get("X-Pandect-Live") !== "1") throw Error("Update unavailable");
        const next = new DOMParser().parseFromString(await fragment.text(), "text/html");
        if (requestedUrl !== location.href) {
          forceNext = true;
          return;
        }
        for (const region of regions()) {
          const fresh = next.getElementById(region.id);
          if (!fresh?.hasAttribute("data-live-region")) throw Error("Missing section");
          waiting.set(region, fresh);
        }
        applyWaiting();
        fragmentDue = false;
      }
      versions = snapshot.versions;
      displayed ??= versions;
      showCollectionChange();
      if (!controls) feedback.hidden = true;
      failures = 0;
    } catch {
      if (stopped) return;
      failures++;
      if (failures >= 3) stopped = true;
      if (controls || stopped) feedback.hidden = false;
      announce(stopped ? (controls ? "Automatic updates paused. Use Refresh status to try again." : "Automatic updates paused.") :
        "Could not update progress. Retrying shortly.");
    } finally {
      clearTimeout(timeout);
      controller = null;
      schedule();
    }
  }
  function forceRefresh() {
    if (stopped) return;
    forceNext = true;
    refresh();
  }
  function markEdited(event) {
    const form = event.target.closest("[data-live-region] form");
    if (form) form.dataset.liveDirty = "true";
  }
  document.addEventListener("input", markEdited);
  document.addEventListener("change", markEdited);
  document.addEventListener("focusout", () => {
    if (waiting.size) setTimeout(() => { if (waiting.size && !stopped) applyWaiting(); }, 0);
  });
  retry.addEventListener("click", event => {
    if (!access.hidden) return;
    event.preventDefault();
    stopped = false;
    failures = 0;
    forceRefresh();
  });
  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer);
    if (!document.hidden) refresh();
  });
  document.addEventListener("pandect:refresh", forceRefresh);
  window.addEventListener("pageshow", event => {
    if (event.persisted && access.hidden) {
      stopped = false;
      refresh();
    }
  });
  window.addEventListener("pagehide", () => {
    stopped = true;
    clearTimeout(timer);
    controller?.abort();
  });
  if (controls && localWork()) announce("Progress updates automatically.");
  refresh();
})();
