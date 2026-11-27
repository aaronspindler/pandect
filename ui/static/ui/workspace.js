(() => {
  "use strict";
  const caseId = document.body.dataset.workspaceCase;
  const driver = document.querySelector("[data-workspace-status]");
  if (!caseId || !driver || !window.htmx) return;
  const root = document.body.dataset.workspaceRoot;
  const navLinks = ["[data-workspace-link]", ".app-rail-links a", ".case-context", ".case-tools-directory a",
    ".case-tools-latest a", ".collection-row", ".evidence-row", ".evidence-match-link", ".analysis-finding-link",
    ".image-gallery-link", ".back-link", ".breadcrumb a", ".case-search-option", "[data-collection-kind] .record-link", ".pagination a", "[data-collection-pagination] a"].join(",");
  const currentUrl = () => new URL(location.href);
  let revision = 0;
  let accessLost = false;
  let renderedUrl = location.href;
  let renderedIndex = history.state?.workspaceIndex || 0;
  const needsDocument = doc => doc.querySelector("[data-workspace-document]:not(a), [data-bulk-upload]");
  const mayLeave = reason => document.dispatchEvent(new CustomEvent("pandect:check-navigation", {cancelable: true, detail: {reason}}));
  function coordinates(focus = document.activeElement) {
    return {scroll: window.scrollY, list: document.querySelector(".evidence-records")?.scrollTop || 0, focus: focus?.id || ""};
  }
  function remember(focus) {
    history.replaceState({...history.state, workspace: coordinates(focus), workspaceIndex: renderedIndex}, "");
  }
  function useUrl(url, restore) {
    if (!restore && location.href !== url.href) {
      history.pushState({workspace: {}, workspaceIndex: renderedIndex + 1}, "", url);
    }
    renderedIndex = history.state?.workspaceIndex || 0;
    renderedUrl = location.href;
  }
  function cancelVisit() {
    revision++;
    htmx.trigger(driver, "htmx:abort");
    driver.removeAttribute("aria-busy");
  }
  function beginLocal() {
    cancelVisit();
    const sequence = revision;
    return () => sequence === revision;
  }
  function pushLocal(url, focus) {
    cancelVisit();
    remember(focus);
    useUrl(new URL(url, location.href));
  }
  function replaceLocal(url, state) {
    history.replaceState({...history.state, ...state}, "", url);
    useUrl(currentUrl(), true);
  }
  function headingFocus(scope, preventScroll = true) {
    const heading = scope.querySelector("h1") || scope.querySelector("h2, h3, summary") || scope;
    if (!heading.hasAttribute("tabindex")) heading.tabIndex = -1;
    heading.focus({preventScroll});
  }
  function swap(element, fresh) {
    htmx.swap(element, fresh.outerHTML, {swapStyle: "outerHTML", swapDelay: 0, settleDelay: 0});
  }
  function stylesheetLinks(doc) {
    return [...doc.head.querySelectorAll('link[rel="stylesheet"]')];
  }
  // The browser enforces a document's original CSP for its whole lifetime.
  // Maps, uploads, and specialist controllers explicitly request document visits.
  function acceptPage(page) {
    return page.body.dataset.workspaceCase === caseId && !needsDocument(page) && page.getElementById("main-content");
  }
  function applyPage(page, url, restore, preserve) {
    const oldMain = document.getElementById("main-content");
    const oldRail = document.querySelector(".app-navigation");
    const freshRail = page.querySelector(".app-navigation");
    if (!freshRail) return false;
    const point = preserve ? coordinates() : restore || {};
    document.dispatchEvent(new Event("pandect:before-navigate"));
    const wanted = stylesheetLinks(page);
    const existing = stylesheetLinks(document);
    for (const link of wanted) {
      if (!existing.some(item => item.href === link.href)) document.head.append(link.cloneNode(true));
    }
    for (const link of existing) {
      if (!wanted.some(item => item.href === link.href)) link.remove();
    }
    document.title = page.title;
    document.body.className = page.body.className;
    // Keep the rail in the document and synchronize its role and current link.
    for (const link of oldRail.querySelectorAll("a")) {
      const incoming = [...freshRail.querySelectorAll("a")].find(candidate => candidate.href === link.href);
      if (!incoming) continue;
      if (incoming.hasAttribute("aria-current")) link.setAttribute("aria-current", incoming.getAttribute("aria-current"));
      else link.removeAttribute("aria-current");
      if (incoming.dataset.tooltip) link.dataset.tooltip = incoming.dataset.tooltip;
    }
    useUrl(url, restore);
    swap(oldMain, page.getElementById("main-content"));
    const versions = page.getElementById("case-activity-versions");
    if (versions) document.querySelector(".case-header").prepend(versions);
    document.dispatchEvent(new Event("pandect:navigated"));
    const main = document.getElementById("main-content");
    const list = main.querySelector(".evidence-records");
    if (list && point.list !== undefined) list.scrollTop = point.list;
    const focus = restore?.focus && document.getElementById(restore.focus);
    if (focus) focus.focus({preventScroll: true});
    else if (!preserve) headingFocus(main);
    const anchor = url.hash && document.getElementById(url.hash.slice(1));
    if (anchor && !restore && !preserve) anchor.scrollIntoView();
    else window.scrollTo(0, point.scroll || 0);
    driver.textContent = document.title;
    return true;
  }
  function sectionFor(link) {
    if (link.hasAttribute("data-workspace-filter-reset")) return link.closest("[data-workspace-form-region]");
    if (!link.closest(".pagination")) return null;
    if (link.closest("[data-timeline], [data-conversation-scroll]")) return null;
    // Within Processing, individual open history sections page independently.
    return link.closest("[data-workspace-region], [data-live-region][id]") ||
      link.closest("section[id], details[id], section[aria-labelledby]");
  }
  async function visit(url, {region, restore, preserve = false} = {}) {
    const sequence = ++revision;
    htmx.trigger(driver, "htmx:abort");
    let applied = false;
    const live = region?.closest("[data-live-region]");
    driver.setAttribute("aria-busy", "true");
    try {
      await htmx.ajax("GET", url.pathname + url.search, {
        source: driver, target: region || "#main-content",
        headers: live ? {"X-Pandect-Live": "1"} : {},
        handler: (_, {xhr}) => {
          if (sequence !== revision || document.querySelector("dialog[open]")) return;
          if (xhr.status !== 200 || new URL(xhr.responseURL).pathname !== url.pathname) return;
          if (!window.PandectPrivateSession.matches(xhr.getResponseHeader("X-Pandect-Session"))) return;
          const page = new DOMParser().parseFromString(xhr.responseText, "text/html");
          const role = page.querySelector("script[data-case-role]")?.dataset.caseRole;
          if (role && !window.PandectPrivateSession.checkRole(role)) return;
          if (region) {
            const selector = region.id ? `#${CSS.escape(region.id)}` :
              `section[aria-labelledby="${CSS.escape(region.getAttribute("aria-labelledby"))}"]`;
            const fresh = page.querySelector(selector);
            if (!region.isConnected || !fresh) return;
            if (live ? xhr.getResponseHeader("X-Pandect-Live") !== "1" : !acceptPage(page)) return;
            useUrl(url, restore);
            swap(region, fresh);
            const next = document.querySelector(selector);
            // Other pagination links carry the complete query for their own page.
            for (const pager of page.querySelectorAll("nav.pagination[aria-label]")) {
              const existing = document.querySelector(`nav.pagination[aria-label="${CSS.escape(pager.getAttribute("aria-label"))}"]`);
              if (existing && !next.contains(existing)) existing.replaceChildren(...pager.childNodes);
            }
            headingFocus(next, false);
            driver.textContent = next.querySelector(".pagination span")?.textContent || "Page updated.";
            applied = true;
          } else if (acceptPage(page)) {
            applied = applyPage(page, url, restore, preserve);
          }
        },
      });
    } catch { /* A regular visit still provides navigation and error recovery. */ }
    if (sequence === revision) driver.removeAttribute("aria-busy");
    return applied || sequence !== revision || Boolean(document.querySelector("dialog[open]"));
  }
  document.addEventListener("click", event => {
    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || !window.PandectPage.isPlainClick(event) ||
        link.target || link.hasAttribute("download") || link.matches("[data-quick-form], [data-workspace-document]") || !link.matches(navLinks)) return;
    const url = new URL(link.href);
    if (url.origin !== location.origin || !url.pathname.startsWith(root) || needsDocument(document) || accessLost) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
    if (link.closest("[data-collection-kind], [data-collection-pagination]") && link.closest(".pagination, [data-collection-pagination]")) return;
    event.preventDefault();
    if (!mayLeave("visit")) return;
    remember(link);
    const region = url.pathname === location.pathname ? sectionFor(link) : null;
    visit(url, {region}).then(ok => { if (!ok) location.assign(url); });
  });
  async function browseAnalysisSources(form, submitter, url) {
    const picker = form.querySelector("#analysis-source-browser");
    if (picker.hasAttribute("aria-busy")) return;
    const isCurrent = beginLocal();
    const values = new FormData(form, submitter);
    let edited = false;
    let applied = false;
    const markEdited = () => { edited = true; };
    picker.addEventListener("input", markEdited);
    picker.setAttribute("aria-busy", "true");
    try {
      await htmx.ajax("POST", url.pathname + url.search, {
        source: driver, target: picker, values,
        headers: {"X-Pandect-Analysis-Browse": "1"},
        handler: (_, {xhr}) => {
          if (!isCurrent() || edited || !picker.isConnected || xhr.status !== 200 ||
              new URL(xhr.responseURL).pathname !== url.pathname) return;
          if (!window.PandectPrivateSession.matches(xhr.getResponseHeader("X-Pandect-Session"))) return;
          const page = new DOMParser().parseFromString(xhr.responseText, "text/html");
          const update = page.querySelector("[data-analysis-source-update]");
          if (!update || !window.PandectPrivateSession.checkRole(update.dataset.caseRole)) return;
          const fresh = update.querySelector("#analysis-source-browser");
          const actions = form.querySelector("#analysis-source-submit");
          const freshActions = update.querySelector("#analysis-source-submit");
          if (!fresh || !actions || !freshActions) return;
          const focus = actions.contains(document.activeElement) ? document.activeElement.id : "";
          const focusPicker = picker.contains(document.activeElement);
          const search = ["search", "clear"].includes(submitter.value);
          swap(actions, freshActions);
          swap(picker, fresh);
          if (focus) document.getElementById(focus)?.focus({preventScroll: true});
          else if (focusPicker) {
            document.getElementById(search ? "analysis-query" : "analysis-source-heading")?.focus();
          }
          driver.textContent = fresh.querySelector("#analysis-source-guidance")?.textContent.trim() ||
            fresh.querySelector(".empty-state p")?.textContent.trim() || "Source choices updated.";
          applied = true;
        },
      });
    } catch { /* The same source-browse action works as an ordinary submission. */ }
    finally {
      picker.removeEventListener("input", markEdited);
      picker.removeAttribute("aria-busy");
    }
    if (!applied && !edited && isCurrent() && form.isConnected) {
      form.dataset.browseFallback = "true";
      form.requestSubmit(submitter);
      delete form.dataset.browseFallback;
    }
  }
  document.addEventListener("submit", event => {
    const form = event.target;
    const submitter = event.submitter;
    if (!event.defaultPrevented && form.id === "analysis-selection" && !form.dataset.browseFallback &&
        submitter?.matches('button[name="browse_sources"], button[name="remove_source"]') &&
        !needsDocument(document) && !accessLost && form.querySelector("#analysis-source-browser")) {
      const url = new URL(form.action || location.href);
      if (url.origin === location.origin && url.pathname.startsWith(root)) {
        event.preventDefault();
        void browseAnalysisSources(form, submitter, url);
        return;
      }
    }
  }, {capture: true});
  document.addEventListener("submit", event => {
    const form = event.target;
    if (event.defaultPrevented || !form.matches("#case-search, form[data-collection-form], form[data-workspace-form]") || form.method !== "get" || needsDocument(document) || accessLost) return;
    const url = new URL(form.action || location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith(root)) return;
    event.preventDefault();
    if (!mayLeave("visit")) return;
    url.search = new URLSearchParams(new FormData(form)).toString();
    remember();
    const region = form.closest("[data-workspace-form-region]");
    visit(url, {region}).then(ok => { if (!ok) location.assign(url); });
  });
  window.addEventListener("popstate", () => {
    if (needsDocument(document) || !history.state?.workspace || renderedUrl === location.href) return;
    if (accessLost) { location.reload(); return; }
    const previous = new URL(renderedUrl);
    if (!mayLeave("history")) {
      const delta = renderedIndex - history.state.workspaceIndex;
      if (delta) history.go(delta);
      return;
    }
    if (previous.pathname === location.pathname) {
      cancelVisit();
      if (!document.dispatchEvent(new Event("pandect:restore-local", {cancelable: true}))) {
        useUrl(currentUrl(), history.state.workspace);
        return;
      }
      if (previous.search === location.search) return;
    }
    visit(currentUrl(), {restore: history.state.workspace}).then(ok => { if (!ok) location.reload(); });
  });
  document.addEventListener("pandect:access-lost", () => {
    accessLost = true;
    cancelVisit();
  });
  document.addEventListener("htmx:beforeSwap", event => {
    const {target, xhr} = event.detail;
    if (target?.id !== "writing-message-count") return;
    const session = window.PandectPrivateSession;
    if ([401, 403, 404].includes(xhr.status)) {
      session.clearPrivatePage();
      event.preventDefault();
    } else if (xhr.status === 200 && !session.matches(xhr.getResponseHeader("X-Pandect-Session"))) {
      event.preventDefault();
    }
  });
  document.addEventListener("htmx:afterRequest", event => {
    const {target, xhr, failed} = event.detail;
    if (target?.id === "writing-message-count" && failed && target.isConnected && xhr.status !== 0) {
      target.textContent = "Message count unavailable. You can still start analysis.";
    }
  });
  window.addEventListener("pagehide", cancelVisit);
  if (!needsDocument(document)) remember();
  window.PandectWorkspace = {
    beginLocal,
    pushLocal,
    replaceLocal,
    refresh: () => accessLost || needsDocument(document) ? Promise.resolve(false) : visit(currentUrl(), {preserve: true}),
  };
})();
