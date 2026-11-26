(() => {
  "use strict";
  let page, source;
  let nextStreamAttempt = 0;

  function closeStream() {
    source?.close();
    source = null;
  }
  function openStream(activity) {
    const url = activity.dataset.caseEventsUrl;
    if (!url || !window.EventSource || document.hidden) return;
    if (source?.url === new URL(url, location.href).href && source.readyState !== EventSource.CLOSED) return;
    closeStream();
    source = new EventSource(url);
    // A fresh snapshot after subscription covers reconnects and the interval
    // between rendering the page and starting to listen for committed changes.
    source.onopen = () => {
      nextStreamAttempt = 0;
      page?.invalidate();
    };
    source.addEventListener("change", () => page?.invalidate());
    source.addEventListener("access-lost", () => {
      closeStream();
      window.PandectPrivateSession.clearPrivatePage();
    });
    source.onerror = () => {
      // EventSource retries an interrupted stream. A failed HTTP handshake
      // can instead close it permanently; the fallback will try again later.
      nextStreamAttempt = Date.now() + 60000;
      page?.fallback();
    };
  }
  function mountPage(activity, signal) {
    const listeners = {signal};
    const badge = activity.querySelector("#processing-activity-count");
    const controls = document.querySelector("[data-live-controls]");
    const feedback = controls || document.querySelector("[data-activity-feedback]");
    const message = feedback.querySelector("[data-live-message], [data-activity-message]");
    const retry = feedback.querySelector("[data-live-refresh], [data-activity-retry]");
    const regions = () => [...document.querySelectorAll("[data-live-region]")];
    const waiting = new Map();
    const savedControls = document.querySelector("[data-live-saved-controls]");
    const changedSaved = new Set();
    let refreshSaved = false;
    const localWork = () => regions().some(region => (waiting.get(region) || region).dataset.livePoll === "true");
    const chatId = document.querySelector("[data-live-chat]")?.dataset.liveChat;
    let chatProgress;
    const discoversWork = Boolean(document.querySelector("#processing-activity, #evidence-findings"));
    const initial = document.getElementById("case-activity-versions");
    let versions = initial ? JSON.parse(initial.textContent) : null;
    let displayed = versions;
    let caseBusy = localWork();
    let timer, controller, failures = 0, stopped = false, forceNext = false;
    let fragmentDue = false;
    let snapshotDue = false;

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
      const keys = kind === "case-tools" ? ["evidence", "findings"] :
        kind === "important" ? ["important", "evidence", "findings"] : [kind];
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
    }, listeners);

    function announce(text, quiet = false) {
      message.classList.toggle("visually-hidden", quiet);
      if (message.textContent !== text) message.textContent = text;
    }
    function schedule() {
      clearTimeout(timer);
      if (!stopped && !document.hidden) {
        const pending = localWork();
        const delay = pending && chatId ? 1000 : (caseBusy || pending ? 5000 : 30000);
        if (forceNext || snapshotDue || failures || !source || source.readyState !== EventSource.OPEN) {
          timer = setTimeout(refresh, forceNext ? 0 : snapshotDue && !failures ? 100 : delay * 2 ** failures);
        }
      }
    }
    function checkAccess(response) {
      if (stopped) return false;
      if (response.type === "opaqueredirect" || [401, 403, 404].includes(response.status)) {
        window.PandectPrivateSession.clearPrivatePage();
        return false;
      }
      if (!response.ok) throw Error("Update unavailable");
      return window.PandectPrivateSession.matches(response.headers.get("X-Pandect-Session"));
    }
    function updateBadge(snapshot) {
      const parts = [];
      if (snapshot.queued) parts.push(snapshot.queued + " queued");
      if (snapshot.running) parts.push(snapshot.running + " running");
      badge.textContent = parts.join(" · ") || (snapshot.active ? "Active" : "");
      badge.hidden = !snapshot.active;
    }
    const disclosureKey = details => details.id || details.querySelector(":scope > summary")?.textContent.trim();
    function savedRowsChanged(region, fresh) {
      // Compare the displayed rows and their destinations. Open disclosures,
      // unsent field values and freshly masked CSRF tokens belong to the reader.
      if (region.textContent !== fresh.textContent) return true;
      const links = region.querySelectorAll("a[href]");
      const next = fresh.querySelectorAll("a[href]");
      return links.length !== next.length || [...links].some((link, index) =>
        link.getAttribute("href") !== next[index].getAttribute("href"));
    }
    function applyWaiting() {
      let outcome;
      for (const [region, fresh] of waiting) {
        if (!region.isConnected) { waiting.delete(region); continue; }
        if (region.querySelector("form[data-live-dirty]")) continue;
        const active = document.activeElement;
        let nextFocus;
        if (region.contains(active)) {
          const disclosure = active.closest("details[data-live-keep-focus]");
          if (!disclosure) continue;
          const next = fresh.querySelector(`#${CSS.escape(disclosure.id)}`);
          nextFocus = next ? [...next.querySelectorAll("a[href]")].find(link =>
            link.getAttribute("href") === active.getAttribute("href")) || next.querySelector(":scope > summary") : region;
        }
        const disclosures = new Map([...region.querySelectorAll("details")].map(item => [disclosureKey(item), item.open]));
        for (const item of fresh.querySelectorAll("details")) {
          const key = disclosureKey(item);
          if (disclosures.has(key)) item.open = disclosures.get(key);
        }
        fresh.querySelectorAll('[role="status"]').forEach(item => item.setAttribute("aria-live", "off"));
        if (region.dataset.livePoll === "true" && fresh.dataset.liveOutcome) {
          outcome = fresh.dataset.liveOutcome;
        }
        region.replaceChildren(...fresh.childNodes);
        nextFocus?.focus({preventScroll: true});
        if (fresh.hasAttribute("data-live-poll")) region.dataset.livePoll = fresh.dataset.livePoll;
        if (fresh.hasAttribute("data-live-saved")) {
          region.dataset.liveSaved = fresh.dataset.liveSaved;
          changedSaved.delete(region.id);
        }
        waiting.delete(region);
      }
      if (savedControls) {
        savedControls.querySelector("a").href = location.href;
        if (!changedSaved.size && savedControls.contains(document.activeElement)) {
          const results = savedControls.nextElementSibling;
          results.tabIndex = -1;
          results.focus({preventScroll: true});
        }
        savedControls.hidden = !changedSaved.size;
      }
      if (controls) announce(outcome || (waiting.size ? "Updates are waiting while you use this section." :
        localWork() ? "Progress updates automatically." :
          changedSaved.size ? "Updated results are available." : "Up to date."), Boolean(outcome));
    }
    async function refresh() {
      clearTimeout(timer);
      if (controller || document.hidden || stopped) return;
      const forced = forceNext;
      const loadSaved = refreshSaved;
      refreshSaved = false;
      forceNext = false;
      snapshotDue = false;
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 15000);
      const options = {credentials: "same-origin", cache: "no-store", redirect: "manual", signal: controller.signal};
      try {
        const statusUrl = new URL(activity.dataset.caseActivityUrl, location.href);
        const pendingChat = chatId && localWork();
        if (pendingChat) statusUrl.searchParams.set("chat", chatId);
        const response = await fetch(statusUrl, {...options, headers: {Accept: "application/json"}});
        if (!checkAccess(response)) return;
        const snapshot = await response.json();
        if (stopped || !window.PandectPrivateSession.checkRole(snapshot.role)) return;
        const changed = !versions || versions.work !== snapshot.versions.work ||
          (document.getElementById("evidence-saved-findings") && versions.findings !== snapshot.versions.findings);
        caseBusy = snapshot.active;
        updateBadge(snapshot);
        // Use the page URL including current filters and pagination.
        const currentChat = pendingChat ? JSON.stringify([
          snapshot.chat_progress, snapshot.chat_activity, snapshot.versions.evidence, snapshot.versions.findings,
        ]) : undefined;
        const progressChanged = pendingChat ? chatProgress !== currentChat : changed && (discoversWork || localWork());
        if ((controls || discoversWork) && (forced || fragmentDue || progressChanged)) {
          fragmentDue = true;
          const requestedUrl = location.href;
          const fragment = await fetch(requestedUrl, {...options, headers: {"X-Pandect-Live": "1", Accept: "text/html"}});
          if (!checkAccess(fragment)) return;
          if (fragment.headers.get("X-Pandect-Live") !== "1") throw Error("Update unavailable");
          const html = await fragment.text();
          if (stopped) return;
          const next = new DOMParser().parseFromString(html, "text/html");
          if (requestedUrl !== location.href) {
            forceNext = true;
            if (loadSaved) refreshSaved = true;
            return;
          }
          for (const region of regions()) {
            const fresh = next.getElementById(region.id);
            if (!fresh?.hasAttribute("data-live-region")) throw Error("Missing section");
            if (region.hasAttribute("data-live-saved")) {
              if (!savedRowsChanged(region, fresh)) {
                changedSaved.delete(region.id);
                waiting.delete(region);
                continue;
              }
              if (region.dataset.liveSaved === "true") {
                changedSaved.add(region.id);
                if (!loadSaved) {
                  waiting.delete(region);
                  continue;
                }
              }
            }
            waiting.set(region, fresh);
          }
          applyWaiting();
          fragmentDue = false;
        }
        chatProgress = currentChat;
        versions = snapshot.versions;
        displayed ??= versions;
        showCollectionChange();
        if (!controls) feedback.hidden = true;
        failures = 0;
        if (source?.readyState === window.EventSource?.CLOSED && nextStreamAttempt && Date.now() >= nextStreamAttempt) {
          openStream(activity);
        }
      } catch {
        if (stopped) return;
        if (loadSaved) refreshSaved = true;
        failures++;
        if (failures >= 3) {
          stopped = true;
          closeStream();
        }
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
    savedControls?.addEventListener("click", event => {
      if (stopped || !event.target.closest("[data-live-refresh-saved]") || event.button || event.metaKey ||
          event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      refreshSaved = true;
      forceRefresh();
    }, listeners);
    function markEdited(event) {
      const form = event.target.closest("[data-live-region] form");
      if (form) form.dataset.liveDirty = "true";
    }
    document.addEventListener("input", markEdited, listeners);
    document.addEventListener("change", markEdited, listeners);
    document.addEventListener("pandect:access-lost", closeStream, listeners);
    document.addEventListener("focusout", () => {
      if (waiting.size) setTimeout(() => { if (waiting.size && !stopped) applyWaiting(); }, 0);
    }, listeners);
    retry.addEventListener("click", event => {
      event.preventDefault();
      stopped = false;
      failures = 0;
      openStream(activity);
      forceRefresh();
    }, listeners);
    document.addEventListener("visibilitychange", () => {
      clearTimeout(timer);
      if (document.hidden) closeStream();
      else if (!stopped) {
        openStream(activity);
        refresh();
      }
    }, listeners);
    document.addEventListener("pandect:refresh", forceRefresh, listeners);
    window.addEventListener("pagehide", () => {
      stopped = true;
      closeStream();
      clearTimeout(timer);
      controller?.abort();
    }, listeners);
    if (controls && localWork()) announce("Progress updates automatically.");
    refresh();
    return {
      invalidate() {
        if (snapshotDue || stopped) return;
        snapshotDue = true;
        if (!controller) schedule();
      },
      fallback() {
        if (!controller) schedule();
      },
      destroy() {
        stopped = true;
        clearTimeout(timer);
        controller?.abort();
        waiting.clear();
        notice?.remove();
        noticeStatus?.remove();
      },
    };
  }
  window.PandectPage(signal => {
    const activity = document.querySelector("[data-case-activity-url]");
    if (activity) {
      page = mountPage(activity, signal);
      signal.addEventListener("abort", () => {
        page.destroy();
        page = null;
      }, {once: true});
      openStream(activity);
    } else {
      closeStream();
    }
  });
})();
