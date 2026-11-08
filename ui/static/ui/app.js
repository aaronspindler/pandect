(() => {
  "use strict";
  const caseNavigation = document.querySelector(".case-navigation");
  if (caseNavigation) {
    const compactNavigation = window.matchMedia("(max-width: 63.999rem)");
    const resizeNavigation = () => { caseNavigation.open = !compactNavigation.matches; };
    resizeNavigation();
    compactNavigation.addEventListener("change", resizeNavigation);
  }
  document.querySelector("[data-error-summary]")?.focus();
  document.querySelectorAll("[data-error-summary] a").forEach((link) => {
    link.addEventListener("click", () => {
      const field = document.getElementById(link.hash.slice(1));
      if (!field) return;
      for (let parent = field.parentElement; parent; parent = parent.parentElement) {
        if (parent.tagName === "DETAILS") parent.open = true;
      }
      field.focus();
    });
  });
  const dirtyForms = new Set();
  document.querySelectorAll("[data-unsaved-form]:not([data-evidence-upload])").forEach((form) => {
    form.addEventListener("input", () => dirtyForms.add(form));
    form.addEventListener("submit", () => dirtyForms.delete(form));
  });
  window.addEventListener("beforeunload", (event) => {
    if (dirtyForms.size) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
})();
