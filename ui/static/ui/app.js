window.PandectPage((signal) => {
  "use strict";
  // One tooltip layer avoids clipping inside scrolling lists and disclosures.
  const tooltip = document.createElement("div");
  tooltip.id = "ui-tooltip";
  tooltip.className = "ui-tooltip";
  tooltip.setAttribute("role", "tooltip");
  tooltip.hidden = true;
  document.body.append(tooltip);
  const tooltipSelector = "[data-tooltip], .leaflet-control-zoom a[title], .leaflet-popup-close-button[aria-label], .leaflet-marker-icon[aria-label]";
  document.querySelectorAll("[data-tooltip]").forEach((control) => control.removeAttribute("title"));
  let tooltipControl = null;
  let tooltipTimer;
  const hideTooltip = () => {
    clearTimeout(tooltipTimer);
    if (tooltipControl) {
      const descriptions = (tooltipControl.getAttribute("aria-describedby") || "").split(/\s+/).filter((id) => id && id !== tooltip.id);
      if (descriptions.length) tooltipControl.setAttribute("aria-describedby", descriptions.join(" "));
      else tooltipControl.removeAttribute("aria-describedby");
    }
    tooltip.hidden = true;
    tooltipControl = null;
  };
  signal.addEventListener("abort", () => { hideTooltip(); tooltip.remove(); }, {once: true});
  const positionTooltip = () => {
    if (!tooltipControl) return;
    const anchor = tooltipControl.getBoundingClientRect();
    if (anchor.bottom <= 0 || anchor.top >= innerHeight || anchor.right <= 0 || anchor.left >= innerWidth) { hideTooltip(); return; }
    const box = tooltip.getBoundingClientRect();
    const rail = tooltipControl.closest(".app-navigation") && matchMedia("(min-width: 64rem)").matches;
    const left = rail ? anchor.right + 8 : anchor.left + (anchor.width - box.width) / 2;
    let top = rail ? anchor.top + (anchor.height - box.height) / 2 : anchor.bottom + 8;
    if (!rail && top + box.height > innerHeight - 8) top = anchor.top - box.height - 8;
    tooltip.style.left = `${Math.max(8, Math.min(left, innerWidth - box.width - 8))}px`;
    tooltip.style.top = `${Math.max(8, Math.min(top, innerHeight - box.height - 8))}px`;
  };
  const showTooltip = (control) => {
    clearTimeout(tooltipTimer);
    if (tooltipControl === control) return;
    hideTooltip();
    const label = control.dataset.tooltip || control.getAttribute("aria-label") || control.title;
    if (!label) return;
    // Retain native title tooltips as the no-JavaScript fallback only.
    control.dataset.tooltip = label;
    control.removeAttribute("title");
    tooltipControl = control;
    tooltip.textContent = label;
    tooltip.hidden = false;
    tooltip.style.left = "0px";
    tooltip.style.top = "0px";
    positionTooltip();
    const descriptions = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
    control.setAttribute("aria-describedby", [...descriptions, tooltip.id].join(" "));
  };
  const scheduleTooltipHide = () => {
    clearTimeout(tooltipTimer);
    tooltipTimer = setTimeout(hideTooltip, 120);
  };
  document.addEventListener("pointerover", (event) => {
    if (event.pointerType === "touch") return;
    const control = event.target.closest(tooltipSelector);
    if (control) showTooltip(control);
  }, {signal});
  document.addEventListener("pointerout", (event) => {
    if (tooltipControl && tooltipControl.contains(event.target) && !tooltipControl.contains(event.relatedTarget) && tooltipControl !== document.activeElement) scheduleTooltipHide();
  }, {signal});
  document.addEventListener("focusin", (event) => {
    const control = event.target.closest(tooltipSelector);
    if (control) showTooltip(control);
    else hideTooltip();
  }, {signal});
  document.addEventListener("focusout", scheduleTooltipHide, {signal});
  tooltip.addEventListener("pointerenter", () => clearTimeout(tooltipTimer));
  tooltip.addEventListener("pointerleave", scheduleTooltipHide);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !tooltip.hidden) {
      hideTooltip();
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, {signal});
  document.addEventListener("click", hideTooltip, {signal});
  document.addEventListener("scroll", (event) => { if (!tooltip.contains(event.target)) positionTooltip(); }, {capture: true, signal});
  window.addEventListener("resize", positionTooltip, {signal});
  const disclosures = Array.from(document.querySelectorAll("[data-disclosure]"));
  const isOpenDisclosure = (details) =>
    details.open && details.querySelector(":scope > summary")?.getClientRects().length > 0;
  const closeDisclosure = (details, restoreFocus = false) => {
    details.open = false;
    if (restoreFocus) details.querySelector(":scope > summary")?.focus();
  };
  disclosures.forEach((details) => {
    details.addEventListener("toggle", () => {
      if (isOpenDisclosure(details)) disclosures.forEach((other) => {
        if (other !== details && isOpenDisclosure(other)) closeDisclosure(other);
      });
    }, {signal});
    details.querySelectorAll("[data-close-disclosure]").forEach((button) => {
      button.hidden = false;
      button.addEventListener("click", () => closeDisclosure(details, true), {signal});
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const open = disclosures.find(isOpenDisclosure);
    if (open) {
      event.preventDefault();
      closeDisclosure(open, true);
    }
  }, {signal});
  document.addEventListener("pointerdown", (event) => {
    disclosures.forEach((details) => {
      if (!details.contains(event.target) && isOpenDisclosure(details)) closeDisclosure(details);
    });
  }, {signal});
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
  document.addEventListener("input", (event) => {
    const form = event.target.closest("[data-unsaved-form]:not([data-evidence-upload])");
    if (form && !form.closest("dialog")) dirtyForms.add(form);
  }, {signal});
  document.addEventListener("submit", (event) => dirtyForms.delete(event.target), {signal});
  document.addEventListener("pandect:check-navigation", (event) => {
    if (!dirtyForms.size) return;
    if (window.confirm("Discard your unsaved changes?")) dirtyForms.clear();
    else event.preventDefault();
  }, {signal});
  window.addEventListener("beforeunload", (event) => {
    if (dirtyForms.size) {
      event.preventDefault();
      event.returnValue = "";
    }
  }, {signal});
});
