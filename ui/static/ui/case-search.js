(() => {
  "use strict";
  const form = document.getElementById("case-search");
  if (!form) return;
  const input = form.elements.q;
  const list = document.getElementById("case-search-suggestions");
  const announcement = document.getElementById("case-search-announcement");
  let timer;
  let request;
  let selected = -1;
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-controls", list.id);
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-keyshortcuts", "Control+k Meta+k");
  const shortcut = form.querySelector("kbd");
  shortcut.textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘ K" : "Ctrl K";
  shortcut.hidden = false;

  function dismiss() {
    clearTimeout(timer);
    timer = null;
    request?.abort();
    request = null;
    selected = -1;
    input.removeAttribute("aria-activedescendant");
    input.setAttribute("aria-expanded", "false");
    list.hidden = true;
    list.replaceChildren();
    announcement.textContent = "";
    announcement.classList.add("visually-hidden");
  }

  async function load(query) {
    timer = null;
    const controller = new AbortController();
    request = controller;
    const url = new URL(form.dataset.suggestionsUrl, location.origin);
    url.searchParams.set("q", query);
    try {
      const response = await fetch(url, {
        signal:controller.signal, headers:{Accept:"application/json"}, cache:"no-store"
      });
      if (!response.ok) throw Error("Quick matches unavailable");
      const {results} = await response.json();
      if (controller.signal.aborted || input.value.trim() !== query || document.activeElement !== input) return;
      results.forEach(({url: path, title, label}) => {
        const destination = new URL(path, location.origin);
        if (destination.origin !== location.origin) return;
        const option = document.createElement("a");
        option.className = "case-search-option";
        option.href = destination.href;
        option.id = "case-search-option-" + list.children.length;
        option.tabIndex = -1;
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        const name = document.createElement("span");
        name.textContent = title;
        const type = document.createElement("small");
        type.textContent = label;
        option.append(name, type);
        list.append(option);
      });
      const count = list.children.length;
      list.hidden = !count;
      input.setAttribute("aria-expanded", String(Boolean(count)));
      announcement.textContent = count
        ? count + " quick " + (count === 1 ? "match" : "matches") + ". Use arrow keys to choose, or submit for all results."
        : "No quick matches. Submit for full results.";
    } catch {
      if (!controller.signal.aborted) {
        announcement.classList.remove("visually-hidden");
        announcement.textContent = "Quick matches unavailable. Submit for full results.";
      }
    } finally {
      if (request === controller) request = null;
    }
  }

  function schedule() {
    dismiss();
    const query = input.value.trim();
    if (query.length >= 2) timer = setTimeout(() => load(query), 180);
  }
  input.addEventListener("input", event => { if (!event.isComposing) schedule(); });
  input.addEventListener("compositionend", schedule);
  input.addEventListener("keydown", event => {
    if (event.isComposing) return;
    if (event.key === "Escape") {
      if (!list.hidden || timer || request || !announcement.classList.contains("visually-hidden")) {
        event.preventDefault();
        dismiss();
      }
      return;
    }
    if (list.hidden) return;
    if (event.key === "Enter" && selected >= 0) {
      event.preventDefault();
      list.children[selected].click();
      return;
    }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    if (selected !== -1) list.children[selected].setAttribute("aria-selected", "false");
    const last = list.children.length - 1;
    selected = event.key === "ArrowDown" ? Math.min(selected + 1, last) : (selected <= 0 ? last : selected - 1);
    const option = list.children[selected];
    option.setAttribute("aria-selected", "true");
    input.setAttribute("aria-activedescendant", option.id);
    option.scrollIntoView({block:"nearest"});
  });
  // Keep DOM focus on the combobox until the selected link navigates.
  list.addEventListener("mousedown", event => { if (event.button === 0) event.preventDefault(); });
  input.addEventListener("blur", dismiss);
  form.addEventListener("submit", dismiss);
  document.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "k") {
      event.preventDefault();
      input.focus();
      input.select();
    }
  });
  window.addEventListener("pagehide", dismiss);
})();
