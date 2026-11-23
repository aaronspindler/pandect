(() => {
  "use strict";
  const results = document.querySelector("[data-collection-kind]");
  if (!results) return;
  const form = document.querySelector("[data-collection-form]");
  const input = form?.elements.q;
  const status = document.createElement("span");
  status.className = "visually-hidden";
  status.setAttribute("role", "status");
  results.before(status);
  let timer, request, pending = null, generation = 0, accessLost = false;

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

  async function update(fromTyping) {
    // Finishing the previous HTTP request also bounds database work on slow cases.
    if (request) {
      pending = fromTyping;
      return;
    }
    const revision = generation;
    const url = new URL(location.href);
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
      if (revision === generation) {
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
      if (response.type === "opaqueredirect" || [401, 403, 404].includes(response.status)) {
        accessLost = true;
        throw Error("access");
      }
      if (revision !== generation || controller.signal.aborted) return;
      if (!response.ok || response.headers.get("X-Pandect-Live") !== "collection") throw Error("unavailable");
      const fragment = new DOMParser().parseFromString(await response.text(), "text/html");
      const fresh = fragment.querySelector("[data-collection-kind]");
      const pagination = document.querySelector("[data-collection-pagination]");
      const freshPagination = fragment.querySelector("[data-collection-pagination]");
      if (fresh?.dataset.collectionKind !== results.dataset.collectionKind || (pagination && !freshPagination)) {
        throw Error("unavailable");
      }
      if (revision !== generation || controller.signal.aborted) return;
      const versions = JSON.parse(response.headers.get("X-Pandect-Activity-Versions") || "{}");
      results.replaceChildren(...fresh.childNodes);
      if (pagination) pagination.replaceChildren(...freshPagination.childNodes);
      if (fromTyping) {
        results.scrollTop = 0;
        history.replaceState({...history.state, evidenceScroll: 0}, "", url);
        updateContext(url);
      }
      status.className = "visually-hidden";
      if (fresh.hasAttribute("data-collection-count")) {
        const count = fresh.dataset.collectionCount;
        results.dataset.collectionCount = count;
        status.textContent = count + (count === "1" ? " result." : " results.");
      } else {
        status.textContent = "Results refreshed.";
      }
      document.dispatchEvent(new CustomEvent("pandect:collection-refreshed", {
        detail: {kind: results.dataset.collectionKind, versions},
      }));
    } catch {
      const cancelled = controller.signal.aborted && controller.signal.reason !== "timeout";
      if (!accessLost && (revision !== generation || cancelled)) return;
      status.className = "muted";
      status.textContent = accessLost ? "Updates stopped. Your session or access may have changed."
        : fromTyping ? "Could not update results. Submit the search to try again."
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
    if (!accessLost) timer = setTimeout(() => update(true), 300);
  }
  input?.addEventListener("input", (event) => { if (!event.isComposing) schedule(); });
  input?.addEventListener("compositionstart", supersede);
  input?.addEventListener("compositionend", schedule);
  form?.addEventListener("submit", cancel);
  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-refresh-results]");
    if (!link || accessLost || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    // A newly populated collection needs its full search and creation toolbar.
    if (!form && ["people", "vehicles", "cases"].includes(results.dataset.collectionKind)) return;
    event.preventDefault();
    supersede();
    const changedQuery = input && input.value.trim() !== (new URL(location.href).searchParams.get("q") || "");
    update(Boolean(changedQuery));
  });
  window.addEventListener("pagehide", cancel);
})();
