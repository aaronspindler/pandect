"use strict";

// The normal POST form remains available without JavaScript.
for (const form of document.querySelectorAll("[data-prepare-trips][data-auto-prepare]")) {
  const status = form.querySelector("[data-prepare-status]");
  const button = form.querySelector("button");
  const prepare = async () => {
    if (form.dataset.busy) return;
    form.dataset.busy = "1";
    button.disabled = true;
    try {
      const response = await fetch(form.action, {
        method: "POST", credentials: "same-origin",
        headers: { Accept: "application/json" }, body: new FormData(form),
      });
      if (!response.ok) throw new Error("unavailable");
      status.textContent = "Trip preparation requested. Refresh to see completed trips, or follow progress in Processing.";
    } catch {
      status.textContent = "Trip preparation could not be confirmed. Try Prepare trips again.";
    } finally {
      delete form.dataset.busy;
      button.disabled = false;
    }
  };
  form.addEventListener("submit", (event) => { event.preventDefault(); prepare(); });
  prepare();
}
