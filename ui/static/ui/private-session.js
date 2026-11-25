(() => {
  "use strict";
  const {session, caseRole, requestMethod, casesUrl} = document.currentScript.dataset;
  const root = document.documentElement;
  let checking = false, accessLost = false;

  function clearPrivatePage() {
    if (accessLost) return;
    accessLost = true;
    document.dispatchEvent(new Event("pandect:access-lost", {cancelable: true}));
    document.dispatchEvent(new Event("pandect:before-navigate"));
    for (const media of document.querySelectorAll("audio, video")) media.pause();
    const main = document.createElement("main");
    main.id = "main-content";
    main.className = "main-content";
    const recovery = document.createElement("section");
    recovery.className = "empty-state";
    const heading = document.createElement("h1");
    heading.textContent = "Your session or access changed";
    heading.tabIndex = -1;
    const link = document.createElement("a");
    link.href = casesUrl;
    link.textContent = "All cases";
    link.className = "button button-primary";
    recovery.append(heading, link);
    main.append(recovery);
    document.title = "Session or access changed · Pandect";
    document.body.classList.remove("app-page", "case-page");
    document.body.removeAttribute("data-workspace-case");
    document.body.removeAttribute("data-workspace-root");
    document.body.replaceChildren(main);
    root.style.display = "";
    heading.focus();
  }

  function matches(responseSession) {
    if (accessLost) return false;
    if (responseSession === session) return true;
    clearPrivatePage();
    return false;
  }

  function reload() {
    if (["GET", "HEAD"].includes(requestMethod)) location.reload();
    // A fresh read must not replay the request that produced a private form.
    else location.replace(location.pathname + location.search);
  }

  function checkRole(role) {
    if (role === caseRole) return true;
    clearPrivatePage();
    return false;
  }

  document.addEventListener("visibilitychange", async () => {
    if (document.hidden || checking || accessLost || caseRole) return;
    checking = true;
    try {
      const response = await fetch(location.href, {
        cache: "no-store", redirect: "manual", signal: AbortSignal.timeout(10000),
      });
      if (response.type === "opaqueredirect" || [401, 403, 404].includes(response.status)) {
        clearPrivatePage();
      } else if (response.ok) matches(response.headers.get("X-Pandect-Session"));
    } catch {
      // A transient connection error does not confirm an access change.
    } finally {
      checking = false;
    }
  });

  window.addEventListener("pagehide", event => {
    if (event.persisted) root.style.display = "none";
  });
  window.addEventListener("pageshow", event => {
    if (!event.persisted) return;
    document.dispatchEvent(new Event("pandect:before-navigate"));
    reload();
  });
  window.PandectPrivateSession = {matches, checkRole, clearPrivatePage};
})();
