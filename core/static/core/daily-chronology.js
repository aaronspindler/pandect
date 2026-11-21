(() => {
  "use strict";

  let timeline = document.querySelector("[data-timeline]");
  if (!timeline) return;

  const controls = document.querySelector("[data-timeline-controls]");
  const picker = document.querySelector(".day-picker");
  const date = picker.elements.namedItem("day");
  const clock = document.querySelector("[data-timeline-clock]");
  const status = document.querySelector("[data-timeline-status]");
  let activeDay = timeline.querySelector("[data-timeline-day]");
  let jump;
  let lastScroll = window.scrollY;
  let scrollFrame;

  function dayURL(day) {
    const url = new URL(location.href);
    url.searchParams.set("day", day.dataset.timelineDay);
    if (day.dataset.timelinePage === "1") url.searchParams.delete("page");
    else url.searchParams.set("page", day.dataset.timelinePage);
    url.hash = "";
    return url;
  }

  function selectDay(day, method = "replaceState") {
    if (activeDay === day) return;
    if (date.value === activeDay.dataset.timelineDay) date.value = day.dataset.timelineDay;
    activeDay = day;
    const url = dayURL(day);
    if (url.href !== location.href) history[method](history.state, "", url);
    if (clock) {
      if (url.searchParams.get("clock") === "utc") url.searchParams.delete("clock");
      else url.searchParams.set("clock", "utc");
      clock.href = url;
    }
  }

  async function loadDay(url, lifecycle, timezone) {
    const response = await fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      signal: AbortSignal.any([lifecycle, AbortSignal.timeout(15000)]),
    });
    if (!response.ok || response.redirected) throw new Error("Day unavailable");
    const page = new DOMParser().parseFromString(await response.text(), "text/html");
    const panel = page.querySelector("[data-timeline]");
    const day = panel?.querySelector("[data-timeline-day]");
    const params = url.searchParams;
    if (
      !day || day.dataset.timelineDay !== params.get("day") ||
      day.dataset.timelinePage !== (params.get("page") || "1") ||
      panel.dataset.timezone !== timezone
    ) throw new Error("Day unavailable");
    return { panel, day };
  }

  function enableScroll(panel) {
    const lifecycle = new AbortController();
    if (!("IntersectionObserver" in window)) return lifecycle;
    const days = panel.querySelector("[data-timeline-days]");
    panel.classList.add("is-scrollable");

    panel.querySelectorAll("[data-timeline-edge]").forEach((edge) => {
      const link = edge.querySelector("a");
      if (!link) return;
      const feedback = edge.querySelector("[role=status]");
      const direction = edge.dataset.timelineEdge;
      const prepend = direction === "newer";
      let loading = false;
      let reading = false;
      link.hidden = true;

      function waitForReading() {
        return prepend && !reading && controls.contains(document.activeElement);
      }

      function observe() {
        if (
          !loading && link.hidden && !edge.hidden && !lifecycle.signal.aborted &&
          !waitForReading()
        ) observer.observe(edge);
      }

      async function load() {
        loading = true;
        feedback.textContent = "Loading dates…";
        try {
          const incoming = await loadDay(new URL(link.href), lifecycle.signal, panel.dataset.timezone);
          if (lifecycle.signal.aborted || waitForReading()) return;
          const boundary = prepend ? days.firstElementChild : days.lastElementChild;
          // The server can change while someone is browsing. Only extend the list.
          if (prepend ? incoming.day.dataset.timelineDay <= boundary.dataset.timelineDay
            : incoming.day.dataset.timelineDay >= boundary.dataset.timelineDay) {
            throw new Error("Day unavailable");
          }
          const nextLink = incoming.panel.querySelector(`[data-timeline-edge="${direction}"] a`);
          const top = boundary.getBoundingClientRect().top;
          feedback.textContent = "";
          if (nextLink) link.setAttribute("href", nextLink.getAttribute("href"));
          else edge.hidden = true;
          if (prepend) {
            days.prepend(incoming.day);
            const shift = boundary.getBoundingClientRect().top - top;
            // A short timeline may need room below it to keep this day in view.
            const missing = window.scrollY + shift + window.innerHeight - document.documentElement.scrollHeight;
            if (missing > 0) {
              const padding = parseFloat(panel.style.paddingBlockEnd) || 0;
              panel.style.paddingBlockEnd = `${padding + Math.ceil(missing)}px`;
            }
            window.scrollBy({ top: shift, behavior: "instant" });
            lastScroll = window.scrollY;
          } else {
            days.append(incoming.day);
          }
        } catch {
          if (!lifecycle.signal.aborted) {
            feedback.textContent = "Could not load more dates.";
            link.hidden = false;
          }
        } finally {
          loading = false;
          if (link.hidden) feedback.textContent = "";
          observe();
        }
      }

      const observer = new IntersectionObserver((entries) => {
        if (loading || !entries.some((entry) => entry.isIntersecting)) return;
        observer.unobserve(edge);
        if (!waitForReading()) load();
      }, { rootMargin: "160px 0px" });
      lifecycle.signal.addEventListener("abort", () => observer.disconnect());
      if (prepend) {
        const options = { passive: true, signal: lifecycle.signal };
        const beginReading = (event) => {
          if (!waitForReading() || controls.contains(event.target)) return;
          reading = true;
          observe();
        };
        const useControls = () => { reading = false; };
        controls.addEventListener("focusout", observe, options);
        controls.addEventListener("focusin", useControls, options);
        date.addEventListener("input", useControls, options);
        document.addEventListener("wheel", beginReading, options);
        document.addEventListener("touchmove", beginReading, options);
      }
      observe();
    });
    return lifecycle;
  }

  let scroll = enableScroll(timeline);

  async function jumpToDate() {
    const selected = date.value;
    if (jump?.day === selected) return;
    jump?.controller.abort();
    jump = null;
    if (!selected || !date.validity.valid ||
      (activeDay.dataset.timelineDay === selected && activeDay.dataset.timelinePage === "1")) {
      status.textContent = "";
      return;
    }
    const request = { day: selected, controller: new AbortController() };
    jump = request;
    const url = new URL(picker.action);
    url.search = new URLSearchParams(new FormData(picker));
    status.classList.remove("sr-only");
    status.textContent = "Opening date…";
    try {
      const incoming = await loadDay(url, request.controller.signal, timeline.dataset.timezone);
      if (jump !== request) return;
      scroll.abort();
      timeline.replaceWith(incoming.panel);
      timeline = incoming.panel;
      selectDay(incoming.day, "pushState");
      lastScroll = window.scrollY;
      scroll = enableScroll(timeline);
      status.classList.add("sr-only");
      status.textContent = `Showing ${incoming.day.querySelector("h2").textContent}.`;
    } catch {
      if (jump === request) status.textContent = "Could not open this date. Use Go to date to try again.";
    } finally {
      if (jump === request) jump = null;
    }
  }

  // Native date fields may delay change until a keyboard edit is committed.
  date.addEventListener("input", jumpToDate);
  date.addEventListener("change", jumpToDate);

  window.addEventListener("scroll", () => {
    if (scrollFrame) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = 0;
      if (window.scrollY === lastScroll || jump) return;
      lastScroll = window.scrollY;
      const days = timeline.querySelector("[data-timeline-days]").children;
      const visible = Array.from(days).find((day) => day.getBoundingClientRect().bottom > 1);
      if (visible) selectDay(visible);
    });
  }, { passive: true });

  // A link may be used from a second visible section, before its heading reaches the top.
  function selectLinkDay(event) {
    const day = event.target.closest("a")?.closest("[data-timeline-day]");
    if (day) selectDay(day);
  }
  document.addEventListener("focusin", selectLinkDay);
  document.addEventListener("click", selectLinkDay);

  window.addEventListener("popstate", () => {
    if (dayURL(activeDay).search !== location.search) location.reload();
  });
})();
