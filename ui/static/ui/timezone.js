(() => {
  "use strict";
  const script = document.currentScript;
  if (script.dataset.automatic !== "true" || script.dataset.method !== "GET") return;
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!zone) return;
    const cookie = `pandect_timezone=${zone}`;
    const hasCookie = () => document.cookie.split("; ").includes(cookie);
    if (hasCookie()) return;
    document.cookie = `${cookie}; Path=/; Max-Age=31536000; SameSite=Lax${location.protocol === "https:" ? "; Secure" : ""}`;
    // Run before forms appear; keep POST error pages in their submitted timezone.
    if (hasCookie()) location.reload();
  } catch {
    // The account setting also works when browser detection is unavailable.
  }
})();
