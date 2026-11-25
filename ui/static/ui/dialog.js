(() => {
  "use strict";
  const dialog = document.getElementById("quick-form-dialog");
  if (!dialog?.showModal) return;
  const title = dialog.querySelector("h2");
  const feedback = dialog.querySelector("[data-dialog-feedback]");
  const fields = dialog.querySelector("[data-dialog-fields]");
  let opener, request, dirty = false, saving = false, accessLost = false;

  function close(force = false) {
    if (!force && (saving || (dirty && !window.confirm("Discard your unsaved changes?")))) return false;
    request?.abort();
    request = null;
    dirty = saving = false;
    dialog.close();
    fields.replaceChildren();
    feedback.replaceChildren();
    opener?.focus({preventScroll: true});
    return true;
  }

  function recovery(message, label, href) {
    const link = document.createElement("a");
    link.className = "record-link";
    link.href = href;
    link.textContent = label;
    feedback.replaceChildren(message + " ", link);
    feedback.tabIndex = -1;
    feedback.focus();
  }

  async function refreshSaved(result, trigger, origin) {
    if (Number.isInteger(result.note_count)) {
      const notes = trigger.parentElement?.querySelector("[data-notes-for]");
      if (notes) {
        notes.textContent = "Notes · " + result.note_count;
        notes.setAttribute("aria-label", `Notes for ${notes.dataset.notesFor} (${result.note_count})`);
      }
      return true;
    }
    if (trigger.dataset.savedLink) {
      // The first record reveals a search form that needs its normal initialization.
      if (!document.querySelector("[data-collection-form]")) return window.PandectWorkspace?.refresh();
      document.dispatchEvent(new Event("pandect:refresh-collection"));
      return true;
    }
    const response = await fetch(origin, {
      headers: {"X-Pandect-Form": "record"}, credentials: "same-origin",
      cache: "no-store", redirect: "manual", signal: AbortSignal.timeout(15000),
    });
    if (accessLost) return null;
    if (location.href !== origin || !trigger.isConnected || dialog.open) return false;
    if (response.type === "opaqueredirect" || [401, 403, 404].includes(response.status)) {
      window.PandectPrivateSession.clearPrivatePage();
      return null;
    }
    if (!response.ok || response.headers.get("X-Pandect-Form") !== "record") return false;
    if (!window.PandectPrivateSession.matches(response.headers.get("X-Pandect-Session"))) return null;
    const fragment = new DOMParser().parseFromString(await response.text(), "text/html");
    const parts = [...fragment.querySelectorAll("[data-record-part][id]")];
    if (accessLost) return null;
    if (location.href !== origin || !trigger.isConnected || dialog.open || !parts.length ||
        !parts.every(part => document.getElementById(part.id)?.hasAttribute("data-record-part"))) return false;
    for (const part of parts) document.getElementById(part.id).replaceWith(part);
    document.title = fragment.title;
    const evidenceTitle = document.getElementById("evidence-record-title");
    if (evidenceTitle) {
      for (const media of document.querySelectorAll("[data-record-label]")) {
        media.setAttribute(media.matches("img") ? "alt" : "aria-label",
          media.dataset.recordLabel + evidenceTitle.dataset.recordTitle);
      }
      document.dispatchEvent(new Event("pandect:refresh-collection"));
    }
    return true;
  }

  async function saved(result, trigger) {
    const origin = location.href;
    const href = trigger.href;
    close(true);
    let refreshed = false;
    try {
      refreshed = await refreshSaved(result, trigger, origin);
    } catch {
      // The mutation succeeded; keep its canonical record link for recovery.
    }
    if (location.href !== origin || dialog.open || refreshed === null) return;
    const target = [...document.querySelectorAll("a[data-quick-form]")].find(link => link.href === href)
      || document.querySelector("main h1");
    if (!target) return;
    if (!target.matches("a")) target.tabIndex = -1;
    if (document.activeElement === trigger || document.activeElement === document.body) {
      target.focus({preventScroll: true});
    }
    document.querySelector(".quick-save-feedback")?.remove();
    const notice = document.createElement("span");
    notice.className = "quick-save-feedback";
    notice.setAttribute("role", "status");
    target.after(notice);
    notice.append(result.message);
    if (trigger.dataset.savedLink || !refreshed) {
      const link = document.createElement("a");
      link.textContent = trigger.dataset.savedLink || "View saved changes";
      link.href = result.url;
      link.className = "record-link";
      notice.append(link);
    }
  }

  async function load(form) {
    const controller = new AbortController();
    request = controller;
    const timeout = setTimeout(() => controller.abort("timeout"), 15000);
    const trigger = opener;
    saving = Boolean(form);
    const payload = form ? new FormData(form) : null;
    feedback.textContent = form ? "Saving…" : "Loading…";
    if (form) {
      feedback.tabIndex = -1;
      feedback.focus({preventScroll: true});
      form.querySelector('[type="submit"]').disabled = true;
      form.inert = true;
    }
    try {
      const response = await fetch(trigger.href, {
        method: form ? "POST" : "GET", body: payload,
        headers: {"X-Pandect-Form": "dialog"}, credentials: "same-origin",
        cache: "no-store", redirect: "manual", signal: controller.signal,
      });
      if (request !== controller) return;
      if (response.type === "opaqueredirect" || [401, 403, 404].includes(response.status)) {
        window.PandectPrivateSession.clearPrivatePage();
        return;
      }
      if (response.headers.get("X-Pandect-Form") &&
          !window.PandectPrivateSession.matches(response.headers.get("X-Pandect-Session"))) return;
      if (form && response.ok && response.headers.get("X-Pandect-Form") === "saved") {
        const result = await response.json();
        if (request !== controller) return;
        if (new URL(result.url, location.href).origin !== location.origin) throw Error("Invalid destination");
        await saved(result, trigger);
        return;
      }
      if (![200, 400, 409].includes(response.status) || response.headers.get("X-Pandect-Form") !== "dialog") {
        throw Error("Form unavailable");
      }
      const fragment = new DOMParser().parseFromString(await response.text(), "text/html");
      if (request !== controller) return;
      const incoming = fragment.querySelector('form[method="post"]');
      if (!incoming) {
        fields.replaceChildren();
        dirty = false;
        recovery("This form is no longer available.", "Open record", trigger.href);
        return;
      }
      fields.replaceChildren(...fragment.body.childNodes);
      feedback.textContent = "";
      const focus = fields.querySelector("[data-error-summary]") ||
        fields.querySelector('input:not([type="hidden"]), textarea, select') || title;
      focus.focus();
    } catch {
      if (request !== controller) return;
      if (form) {
        // A lost response may follow a committed creation or note. Never replay it.
        recovery("The save could not be confirmed. Check the record before trying again.",
          "Check record", location.href);
      } else {
        recovery("The form could not be loaded.", "Open full page", trigger.href);
      }
    } finally {
      clearTimeout(timeout);
      if (request === controller) {
        if (form) form.inert = false;
        saving = false;
        request = null;
      }
    }
  }

  document.addEventListener("click", event => {
    const trigger = event.target.closest("a[data-quick-form]");
    if (!trigger || accessLost || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey ||
        event.shiftKey || event.altKey || trigger.target || trigger.origin !== location.origin || dialog.open) return;
    event.preventDefault();
    window.PandectWorkspace?.beginLocal();
    opener = trigger;
    document.querySelector(".quick-save-feedback")?.remove();
    title.textContent = trigger.dataset.quickForm || trigger.textContent.trim();
    dialog.showModal();
    title.focus();
    load();
  });
  dialog.addEventListener("click", event => {
    if (event.target.closest("[data-dialog-cancel]")) {
      event.preventDefault();
      close();
    }
    const link = event.target.closest("[data-error-summary] a");
    if (link?.hash) {
      const field = fields.querySelector("#" + CSS.escape(link.hash.slice(1)));
      if (field) {
        event.preventDefault();
        for (let parent = field.parentElement; parent !== dialog; parent = parent.parentElement) {
          if (parent.tagName === "DETAILS") parent.open = true;
        }
        field.focus();
      }
    }
  });
  dialog.addEventListener("input", () => { dirty = true; });
  dialog.addEventListener("keydown", event => {
    if (event.key === "Escape") event.stopPropagation();
    if (event.key !== "Tab" || event.altKey || event.ctrlKey || event.metaKey) return;
    const controls = [...dialog.querySelectorAll("a[href], button, input, textarea, select, summary, [tabindex]")]
      .filter(el => !el.closest("[inert]") && !el.matches(':disabled, [tabindex="-1"]') && el.getClientRects().length);
    const index = controls.indexOf(document.activeElement);
    const next = index < 0 ? (event.shiftKey ? controls.length - 1 : 0)
      : (index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length;
    // Date/time widgets use Tab internally to move between their editable segments.
    if (event.target.matches('input[type="datetime-local"]')) {
      requestAnimationFrame(() => {
        if (dialog.open && (!dialog.contains(document.activeElement) || document.activeElement === dialog)) controls[next]?.focus();
      });
      return;
    }
    event.preventDefault();
    controls[next]?.focus();
  });
  dialog.addEventListener("cancel", event => { event.preventDefault(); close(); });
  dialog.addEventListener("submit", event => {
    event.preventDefault();
    if (!saving && !event.target.querySelector('[type="submit"]').disabled) load(event.target);
  });
  document.addEventListener("pandect:access-lost", () => {
    accessLost = true;
    if (!dialog.open) return;
    request?.abort();
    request = null;
    dirty = saving = false;
    fields.replaceChildren();
  });
  document.addEventListener("pandect:check-navigation", event => {
    if (!dialog.open) return;
    if (!close() || event.detail?.reason === "history") event.preventDefault();
  });
  window.addEventListener("beforeunload", event => {
    if (dirty || saving) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  window.addEventListener("pagehide", () => { if (dialog.open) close(true); });
})();
