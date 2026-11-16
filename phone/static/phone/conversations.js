(() => {
  "use strict";
  if (!("IntersectionObserver" in window)) return;

  const reader = document.querySelector("[data-conversation-scroll]");
  if (!reader) return;
  const items = reader.querySelector("[data-conversation-items]");
  const label = reader.dataset.scrollLabel;

  reader.querySelectorAll("[data-conversation-edge]").forEach((edge) => {
    const link = edge.querySelector("[data-conversation-page]");
    if (!link) return;
    const status = edge.querySelector("[data-conversation-status]");
    const previous = edge.dataset.conversationEdge === "previous";
    let loading = false;
    let failed = false;

    const load = async (activated = false) => {
      if (loading || failed || link.hidden) return;
      loading = true;
      observer.unobserve(edge);
      link.setAttribute("aria-disabled", "true");
      status.textContent = `Loading ${label}…`;
      try {
        const response = await fetch(link.href, {
          credentials: "same-origin",
          cache: "no-store",
          signal: AbortSignal.timeout(15000),
        });
        if (!response.ok || response.redirected) throw new Error("Page unavailable");
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        const incoming = page.querySelector("[data-conversation-items]");
        if (!incoming || incoming.dataset.page !== new URL(link.href).searchParams.get("page")) {
          throw new Error("Page unavailable");
        }
        const next = page.querySelector(`[data-conversation-edge="${edge.dataset.conversationEdge}"] [data-conversation-page]`);
        const rows = Array.from(incoming.children).filter((row) => !row.id || !document.getElementById(row.id));
        const anchor = items.querySelector("[id]");
        const top = anchor?.getBoundingClientRect().top;

        // A page boundary can split one date group. Keep just one heading.
        const oldDates = items.querySelectorAll("[data-conversation-day]");
        const newDates = incoming.querySelectorAll("[data-conversation-day]");
        const oldDate = previous ? oldDates[0] : oldDates[oldDates.length - 1];
        const newDate = previous ? newDates[newDates.length - 1] : newDates[0];
        if (oldDate && newDate && oldDate.dataset.conversationDay === newDate.dataset.conversationDay) {
          if (previous) oldDate.remove();
          else rows.splice(rows.indexOf(newDate), 1);
        }
        const fragment = document.createDocumentFragment();
        rows.forEach((row) => fragment.append(row));
        if (previous) items.prepend(fragment);
        else items.append(fragment);
        if (next) link.setAttribute("href", next.getAttribute("href"));
        else link.hidden = true;
        if (previous && anchor) window.scrollBy(0, anchor.getBoundingClientRect().top - top);
        status.textContent = `${rows.filter((row) => row.id).length} ${label} loaded.`;
        if (!next) status.textContent += previous ? ` Start of ${label}.` : ` End of ${label}.`;
        if (activated) rows.find((row) => row.id)?.focus();
      } catch {
        failed = true;
        status.textContent = `Could not load ${label}. Use the link to open the page.`;
      } finally {
        loading = false;
        link.removeAttribute("aria-disabled");
        if (!failed && !link.hidden) observer.observe(edge);
      }
    };
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) load();
    }, { rootMargin: "200px 0px" });
    link.addEventListener("click", (event) => {
      if (failed || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      load(true);
    });
    observer.observe(edge);
  });
})();
