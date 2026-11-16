(() => {
  "use strict";
  const controls = document.querySelector("[data-live-controls]");
  if (!controls) return;
  const message = controls.querySelector("[data-live-message]");
  const regions = () => [...document.querySelectorAll("[data-live-region]")];
  const active = () => regions().some((region) => region.dataset.livePoll === "true");
  let timer, controller, failures = 0, stopped = false;
  const announce = (text) => { if (message.textContent !== text) message.textContent = text; };
  const schedule = () => {
    clearTimeout(timer);
    if (!stopped && !document.hidden && active()) timer = setTimeout(refresh, 5000 * 2 ** failures);
  };
  const disclosureKey = (details) => details.id || details.querySelector(":scope > summary")?.textContent.trim();
  const refresh = async () => {
    clearTimeout(timer);
    if (controller || document.hidden || stopped) return;
    controller = new AbortController();
    const timeout = setTimeout(() => controller?.abort(), 15000);
    try {
      const response = await fetch(window.location.href, {
        headers: { "X-Pandect-Live": "1", Accept: "text/html" },
        credentials: "same-origin", cache: "no-store", redirect: "manual", signal: controller.signal,
      });
      if (response.type === "opaqueredirect" || [401, 403, 404].includes(response.status)) {
        stopped = true;
        announce("Automatic updates stopped. Your session or access may have changed.");
        controls.querySelector("[data-live-access]").hidden = false;
        return;
      }
      if (!response.ok || response.headers.get("X-Pandect-Live") !== "1") throw new Error("unavailable");
      const next = new DOMParser().parseFromString(await response.text(), "text/html");
      const updates = regions().map((region) => [region, next.getElementById(region.id)]);
      if (!updates.length || updates.some(([, fresh]) => !fresh?.hasAttribute("data-live-region"))) throw new Error("unavailable");
      let deferred = false;
      for (const [region, fresh] of updates) {
        // Never replace a focused control or a form whose values are being edited.
        if (region.contains(document.activeElement) || region.querySelector("form[data-live-dirty]")) {
          deferred = true;
          continue;
        }
        const disclosures = new Map([...region.querySelectorAll("details")].map((details) => [disclosureKey(details), details.open]));
        for (const details of fresh.querySelectorAll("details")) {
          const key = disclosureKey(details);
          if (disclosures.has(key)) details.open = disclosures.get(key);
        }
        // The persistent message announces outcomes; progress ticks stay quiet.
        fresh.querySelectorAll('[role="status"]').forEach((status) => status.setAttribute("aria-live", "off"));
        region.replaceChildren(...fresh.childNodes);
        region.dataset.livePoll = fresh.dataset.livePoll;
      }
      failures = 0;
      announce(deferred ? "Updates are waiting while you use this section." : active() ? "Progress updates automatically." : "Up to date.");
    } catch {
      failures += 1;
      if (failures >= 3) stopped = true;
      announce(stopped ? "Automatic updates paused. Use Refresh status to try again." : "Could not update progress. Retrying shortly.");
    } finally {
      clearTimeout(timeout);
      controller = null;
      schedule();
    }
  };
  document.addEventListener("input", (event) => {
    const form = event.target.closest("[data-live-region] form");
    if (form) form.dataset.liveDirty = "true";
  });
  document.addEventListener("change", (event) => {
    const form = event.target.closest("[data-live-region] form");
    if (form) form.dataset.liveDirty = "true";
  });
  document.addEventListener("submit", (event) => {
    if (event.target.matches("[data-prepare-trips]")) return;
    stopped = true;
    clearTimeout(timer);
    controller?.abort();
  });
  controls.querySelector("[data-live-refresh]").addEventListener("click", (event) => {
    if (controls.querySelector("[data-live-access]").hidden === false) return;
    event.preventDefault();
    stopped = false;
    failures = 0;
    refresh();
  });
  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer);
    if (!document.hidden && active()) refresh();
  });
  document.addEventListener("pandect:refresh", refresh);
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      stopped = false;
      schedule();
    }
  });
  window.addEventListener("pagehide", () => {
    stopped = true;
    clearTimeout(timer);
    controller?.abort();
  });
  if (active()) announce("Progress updates automatically.");
  schedule();
})();
