window.PandectPage((signal) => {
  "use strict";

  function ordinaryClick(event) {
    return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
  }

  function enableMessageSelection(workspace, transcript) {
    const panel = workspace.querySelector("[data-message-panel]");
    const initialDay = new URL(location.href).searchParams.get("day");
    let selected = transcript.querySelector("[data-selected-message]");
    let transcriptOffset = null;
    const firstEntry = history.state?.workspaceIndex;
    let lastEntry = firstEntry;
    workspace.classList.add("is-interactive");

    function show(row, anchor = row || selected) {
      if (!anchor) return;
      if (anchor.getClientRects().length) transcriptOffset = anchor.getBoundingClientRect().top;

      // Keep one live copy of each server-rendered message inspector.
      if (row !== selected) {
        if (selected) {
          panel.querySelector(".conversation-message-body").textContent = "";
          selected.querySelector("[data-message-content]").content.append(...panel.childNodes);
          selected.removeAttribute("data-selected-message");
          selected.querySelector("[data-message-select]").removeAttribute("aria-current");
        }
        if (row) {
          panel.replaceChildren(row.querySelector("[data-message-content]").content);
          panel.querySelector(".conversation-message-body").textContent = row.querySelector(".conversation-message-body").textContent;
          row.setAttribute("data-selected-message", "");
          row.querySelector("[data-message-select]").setAttribute("aria-current", "true");
        }
        panel.hidden = !row;
        panel.scrollTop = 0;
        selected = row;
      }

      if (anchor.getClientRects().length) {
        // Splitting the desktop view changes wrapping above the selected row.
        const targetTop = transcriptOffset ?? window.innerHeight / 3;
        window.scrollBy(0, anchor.getBoundingClientRect().top - targetTop);
      } else {
        panel.scrollIntoView({ block: "start" });
      }
      const focus = row ? panel.querySelector("h2") : anchor.querySelector("[data-message-select]");
      if (!row) focus.scrollIntoView({ block: "nearest" });
      focus.focus({ preventScroll: true });
    }

    function navigate(row, url) {
      if (!window.PandectWorkspace) {
        location.assign(url);
        return;
      }
      window.PandectWorkspace.pushLocal(url);
      lastEntry = history.state.workspaceIndex;
      show(row);
    }

    workspace.addEventListener("click", (event) => {
      if (!ordinaryClick(event)) return;
      const close = event.target.closest("[data-message-close]");
      if (close) {
        event.preventDefault();
        navigate(null, close.href);
        return;
      }
      const bubble = event.target.closest(".conversation-bubble");
      if (!bubble) return;
      // The original is plain selectable text. Finishing a drag must not open the inspector.
      if (!event.target.closest("[data-message-select]") && !window.getSelection()?.isCollapsed) return;
      const row = bubble.closest(".conversation-message");
      const link = bubble.querySelector("[data-message-select]");
      if (row.hasAttribute("data-load-panel-on-open")) {
        if (!link.contains(event.target)) link.click();
        return;
      }
      event.preventDefault();
      navigate(row, link.href);
    }, {signal});
    workspace.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !event.defaultPrevented && selected) {
        event.preventDefault();
        navigate(null, panel.querySelector("[data-message-close]").href);
      }
    }, {signal});
    document.addEventListener("pandect:restore-local", event => {
      // Only entries opened within this still-mounted transcript can reuse its rows.
      const index = history.state?.workspaceIndex;
      if (index < firstEntry || index > lastEntry || firstEntry === lastEntry) return;
      const params = new URL(location.href).searchParams;
      const id = params.get("message");
      const row = id ? document.getElementById(`message-${id}`) : null;
      // Refreshing can discard messages loaded by scrolling and their finding pages.
      let anchor = row || selected;
      let available;
      if (id) {
        available = row && !row.hasAttribute("data-load-panel-on-open") && (params.get("analysis_page") || "") === row.dataset.analysisPage;
      } else if (location.hash.startsWith("#message-")) {
        anchor = document.getElementById(location.hash.slice(1));
        available = Boolean(anchor);
      } else if (params.has("day")) {
        available = params.get("day") === initialDay;
      } else {
        available = transcript.querySelector("[data-conversation-items]").dataset.page === (params.get("page") || "1");
      }
      if (available) {
        event.preventDefault();
        show(row, anchor);
      }
    }, {signal});
  }

  function enablePaging(reader, workspace) {
    const items = reader.querySelector("[data-conversation-items]");
    const label = reader.dataset.scrollLabel;
    const pageParam = reader.dataset.pageParam || "page";
    reader.querySelectorAll(":scope > [data-conversation-edge]").forEach((edge) => {
      const link = edge.querySelector("[data-conversation-page]");
      if (!link) return;
      const status = edge.querySelector("[data-conversation-status]");
      const previous = edge.dataset.conversationEdge === "previous";
      let loading = false;
      let failed = false;

      const load = async (activated = false) => {
        if (signal.aborted || loading || failed || link.hidden) return;
        const isCurrent = activated && window.PandectWorkspace?.beginLocal();
        const focus = document.activeElement;
        loading = true;
        observer.unobserve(edge);
        link.setAttribute("aria-disabled", "true");
        status.textContent = `Loading ${label}…`;
        try {
          const response = await fetch(link.href, {
            credentials: "same-origin",
            cache: "no-store",
            redirect: "manual",
            signal: AbortSignal.any([signal, AbortSignal.timeout(15000)]),
          });
          if (response.type === "opaqueredirect" || [401, 403, 404].includes(response.status)) {
            window.PandectPrivateSession.clearPrivatePage();
            return;
          }
          if (!response.ok) throw new Error("Page unavailable");
          if (!window.PandectPrivateSession.matches(response.headers.get("X-Pandect-Session"))) return;
          const page = new DOMParser().parseFromString(await response.text(), "text/html");
          if (signal.aborted) return;
          const incomingReader = reader.id ? page.getElementById(reader.id) : page.querySelector("[data-conversation-scroll]");
          const incoming = incomingReader?.querySelector("[data-conversation-items]");
          if (!incoming || incoming.dataset.page !== new URL(link.href).searchParams.get(pageParam)) {
            throw new Error("Page unavailable");
          }
          // Keep the search controls from the current page when adding context.
          incoming.querySelector("#conversation-search-result")?.remove();
          const next = incomingReader.querySelector(`:scope > [data-conversation-edge="${edge.dataset.conversationEdge}"] [data-conversation-page]`);
          const loadedKeys = new Set(Array.from(items.querySelectorAll("[data-message-key]"), row => row.dataset.messageKey));
          const rows = Array.from(incoming.children).filter(row => row.dataset.messageKey
            ? !loadedKeys.has(row.dataset.messageKey)
            : !row.id || !document.getElementById(row.id));
          const anchor = items.querySelector(":scope > :not([data-conversation-day])");
          const top = anchor?.getBoundingClientRect().top;

          // A page boundary can split one date group. Keep just one heading.
          const oldDates = items.querySelectorAll(":scope > [data-conversation-day]");
          const newDates = incoming.querySelectorAll(":scope > [data-conversation-day]");
          const oldDate = previous ? oldDates[0] : oldDates[oldDates.length - 1];
          const newDate = previous ? newDates[newDates.length - 1] : newDates[0];
          if (oldDate && newDate && oldDate.dataset.conversationDay === newDate.dataset.conversationDay) {
            if (previous) {
              if (oldDate.id) {
                newDate.id = oldDate.id;
                newDate.tabIndex = -1;
              }
              oldDate.remove();
            } else rows.splice(rows.indexOf(newDate), 1);
          }
          const fragment = document.createDocumentFragment();
          const focusLoaded = activated && (!isCurrent || isCurrent()) && document.activeElement === focus;
          rows.forEach((row) => fragment.append(row));
          if (previous) {
            items.prepend(fragment);
            if (incoming.hasAttribute("start")) items.start = incoming.start;
          } else items.append(fragment);
          if (next) link.setAttribute("href", next.getAttribute("href"));
          else link.hidden = true;
          const records = rows.filter((row) => !row.hasAttribute("data-conversation-day"));
          status.textContent = `${records.length} ${label} loaded.`;
          if (!next) status.textContent += previous ? ` Start of ${label}.` : ` End of ${label}.`;
          if (previous && anchor) {
            const shift = anchor.getBoundingClientRect().top - top;
            // On a short last page the inspector can extend below the transcript.
            // Keep just enough trailing room to restore the anchor after prepending.
            const spaceBelow = document.documentElement.scrollHeight - innerHeight - scrollY;
            if (workspace && shift > spaceBelow) {
              const padding = parseFloat(getComputedStyle(workspace).paddingBlockEnd);
              workspace.style.paddingBlockEnd = `${padding + Math.ceil(shift - spaceBelow)}px`;
            }
            window.scrollBy(0, shift);
          }
          if (focusLoaded) records[0]?.focus();
        } catch {
          if (signal.aborted) return;
          failed = true;
          status.textContent = `Could not load ${label}. Use the link to open the page.`;
        } finally {
          loading = false;
          link.removeAttribute("aria-disabled");
          if (!signal.aborted && !failed && !link.hidden) observer.observe(edge);
        }
      };
      const observer = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) load();
      }, { rootMargin: "200px 0px" });
      signal.addEventListener("abort", () => observer.disconnect(), {once: true});
      link.addEventListener("click", (event) => {
        if (failed || !ordinaryClick(event)) return;
        event.preventDefault();
        load(true);
      }, {signal});
      observer.observe(edge);
    });
  }

  document.querySelectorAll("[data-conversation-scroll]").forEach((reader) => {
    const workspace = reader.closest("[data-conversation-workspace]");
    if (workspace) enableMessageSelection(workspace, reader);
    if ("IntersectionObserver" in window) enablePaging(reader, workspace);
  });
});
