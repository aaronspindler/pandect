window.PandectPage((signal) => {
  "use strict";
  const results = document.querySelector("[data-collection-kind]");
  if (!results) return;
  const summary = document.querySelector("[data-collection-summary]");
  const form = document.querySelector("[data-collection-form]");
  const input = form?.elements.q;
  const hidesEmptySearch = form?.hasAttribute("data-collection-hide-empty");
  const status = document.createElement("span");
  status.className = "visually-hidden";
  status.setAttribute("role", "status");
  results.before(status);
  let timer, request, pending = null, generation = 0;
  let accessLost = false;

  function canFillEmpty() {
    const active = document.activeElement;
    return !accessLost && results.hasAttribute("data-collection-empty") && !results.contains(active) &&
      !active?.matches("[data-refresh-results]") && document.getSelection()?.isCollapsed &&
      !document.querySelector('dialog[open], [data-workspace-status][aria-busy="true"]');
  }

  function supersede() {
    clearTimeout(timer);
    pending = null;
    generation++;
  }

  function cancel() {
    supersede();
    request?.abort();
  }

  // Links returning from record actions use the same collection query as the URL.
  function updateContext(url) {
    for (const element of document.querySelectorAll("[data-collection-context]")) {
      const attribute = element.tagName === "FORM" ? "action" : "href";
      const destination = new URL(element.getAttribute(attribute), location.href);
      const returnKey = element.dataset.collectionContext;
      const params = returnKey
        ? new URLSearchParams(destination.searchParams.get(returnKey))
        : destination.searchParams;
      for (const key of ["q", "page"]) {
        if (url.searchParams.has(key)) params.set(key, url.searchParams.get(key));
        else params.delete(key);
      }
      if (returnKey) destination.searchParams.set(returnKey, params.toString());
      element.setAttribute(attribute, destination.pathname + destination.search + destination.hash);
    }
    const clear = form?.querySelector("[data-collection-clear]");
    if (clear) clear.hidden = !url.searchParams.get("q") && clear.dataset.otherFilters !== "true";
  }

  async function update(options = {}) {
    const {fromTyping, destination, current, trigger} = options;
    const stale = () => signal.aborted || !results.isConnected || (current && !current());
    if (stale()) return;
    // Finishing the previous HTTP request also bounds database work on slow cases.
    if (request) {
      pending = options;
      return;
    }
    const revision = generation;
    const url = destination || new URL(location.href);
    if (fromTyping) {
      const query = input.value.trim();
      if (query === (url.searchParams.get("q") || "")) {
        status.className = "visually-hidden";
        status.textContent = "";
        return;
      }
      if (query) url.searchParams.set("q", query);
      else url.searchParams.delete("q");
      url.searchParams.delete("page");
    }
    const controller = new AbortController();
    request = controller;
    const timeout = setTimeout(() => controller.abort("timeout"), 15000);
    const loading = setTimeout(() => {
      if (revision === generation && !stale()) {
        status.className = "muted";
        status.textContent = fromTyping ? "Searching…" : "Refreshing results…";
      }
    }, 300);
    results.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(url, {
        headers: {"X-Pandect-Live": "collection", Accept: "text/html"},
        signal: controller.signal, credentials: "same-origin", cache: "no-store", redirect: "manual",
      });
      if (revision !== generation || stale() || controller.signal.aborted) return;
      if (window.PandectPrivateSession.clearIfAccessDenied(response)) return;
      if (!response.ok || response.headers.get("X-Pandect-Live") !== "collection") throw Error("unavailable");
      if (!window.PandectPrivateSession.matches(response.headers.get("X-Pandect-Session"))) return;
      const fragment = new DOMParser().parseFromString(await response.text(), "text/html");
      const fresh = fragment.querySelector("[data-collection-kind]");
      const freshSummary = fragment.querySelector("[data-collection-summary]");
      const pagination = document.querySelector("[data-collection-pagination]");
      const freshPagination = fragment.querySelector("[data-collection-pagination]");
      if (fresh?.dataset.collectionKind !== results.dataset.collectionKind ||
          (summary && !freshSummary) || (pagination && !freshPagination)) {
        throw Error("unavailable");
      }
      if (revision !== generation || stale() || controller.signal.aborted) return;
      const versions = JSON.parse(response.headers.get("X-Pandect-Activity-Versions") || "{}");
      if (destination) window.PandectWorkspace.pushLocal(url, trigger);
      else if (fromTyping) {
        if (window.PandectWorkspace) window.PandectWorkspace.replaceLocal(url, {evidenceScroll: 0});
        else history.replaceState({...history.state, evidenceScroll: 0}, "", url);
      }
      results.replaceChildren(...fresh.childNodes);
      if (hidesEmptySearch) {
        const empty = fresh.hasAttribute("data-collection-empty");
        results.toggleAttribute("data-collection-empty", empty);
        form.hidden = empty && !form.contains(document.activeElement);
      }
      if (summary) summary.replaceChildren(...freshSummary.childNodes);
      if (pagination) pagination.replaceChildren(...freshPagination.childNodes);
      if (fromTyping || destination) {
        results.scrollTop = 0;
        if (destination) {
          if (input) input.value = url.searchParams.get("q") || "";
          results.tabIndex = -1;
          results.focus();
        }
        updateContext(url);
      }
      status.className = "visually-hidden";
      if (fresh.hasAttribute("data-collection-count")) {
        const count = fresh.dataset.collectionCount;
        results.dataset.collectionCount = count;
        const notice = results.querySelector("[data-search-notice]");
        status.textContent = notice?.textContent || count + (count === "1" ? " result." : " results.");
      } else {
        status.textContent = "Results refreshed.";
      }
      document.dispatchEvent(new CustomEvent("pandect:collection-refreshed", {
        detail: {kind: results.dataset.collectionKind, versions},
      }));
    } catch {
      const cancelled = controller.signal.aborted && controller.signal.reason !== "timeout";
      if (accessLost || revision !== generation || stale() || cancelled) return;
      if (destination) { location.assign(destination); return; }
      status.className = "muted";
      status.textContent = fromTyping ? "Could not update results. Submit the search to try again."
        : "Could not update results. Refresh the page to try again.";
    } finally {
      clearTimeout(timeout);
      clearTimeout(loading);
      request = null;
      results.removeAttribute("aria-busy");
      if (revision !== generation && !accessLost) {
        status.textContent = "";
        status.className = "visually-hidden";
      }
      if (pending !== null && !accessLost) {
        const next = pending;
        pending = null;
        update(next);
      }
    }
  }

  function schedule() {
    supersede();
    if (accessLost) return;
    const current = window.PandectWorkspace?.beginLocal();
    timer = setTimeout(() => update({fromTyping: true, current}), 300);
  }
  input?.addEventListener("input", (event) => { if (!event.isComposing) schedule(); });
  input?.addEventListener("compositionstart", () => {
    supersede();
    if (!accessLost) window.PandectWorkspace?.beginLocal();
  });
  input?.addEventListener("compositionend", schedule);
  form?.addEventListener("submit", cancel);
  document.addEventListener("pandect:refresh-collection", () => {
    if (!accessLost) { supersede(); update(); }
  }, {signal});
  document.addEventListener("pandect:fill-empty-collection", () => {
    if (!request && canFillEmpty()) update({current: canFillEmpty});
  }, {signal});
  document.addEventListener("pandect:access-lost", () => {
    accessLost = true;
    cancel();
    status.className = "visually-hidden";
    status.textContent = "";
    results.removeAttribute("aria-busy");
  }, {signal});
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || !window.PandectPage.isPlainClick(event)) return;
    const pageLink = event.target.closest(".pagination a, [data-collection-pagination] a");
    if (!accessLost && window.PandectWorkspace && pageLink && !pageLink.target &&
        (results.contains(pageLink) || pageLink.closest("[data-collection-pagination]"))) {
      const url = new URL(pageLink.href);
      if (url.origin !== location.origin || url.pathname !== location.pathname) return;
      event.preventDefault();
      supersede();
      update({destination: url, trigger: pageLink, current: window.PandectWorkspace.beginLocal()});
      return;
    }
    const link = event.target.closest("[data-refresh-results]");
    if (!link || accessLost) return;
    event.preventDefault();
    supersede();
    const changedQuery = input && input.value.trim() !== (new URL(location.href).searchParams.get("q") || "");
    update({fromTyping: Boolean(changedQuery), current: window.PandectWorkspace?.beginLocal()});
  }, {signal});
  window.addEventListener("pagehide", cancel, {signal});
  signal.addEventListener("abort", cancel, {once: true});
});
