(() => {
  const picker = document.getElementById("ai-model-picker");
  if (!picker || typeof picker.showModal !== "function") return;
  const title = picker.querySelector("h2");
  const query = picker.querySelector("input[type=search]");
  const matches = picker.querySelector("select");
  const status = picker.querySelector("[role=status]");
  const apply = picker.querySelector("[data-use-model]");
  let activeSelect;
  let opener;

  function filterOptions() {
    const term = query.value.trim().toLocaleLowerCase();
    const visible = activeSelect.cloneNode(true);
    for (const option of Array.from(visible.options)) {
      const group = option.closest("optgroup")?.label || "";
      const searchable = [option.text, option.value, group].join(" ").toLocaleLowerCase();
      if (!searchable.includes(term)) option.remove();
    }
    for (const group of visible.querySelectorAll("optgroup")) {
      if (!group.querySelector("option")) group.remove();
    }
    matches.replaceChildren(...visible.children);
    matches.value = activeSelect.value;
    if (matches.selectedIndex < 0) {
      const first = Array.from(matches.options).find(option => !option.matches(":disabled"));
      if (first) matches.value = first.value;
    }
    updateAction();
    status.textContent = matches.length ? `${matches.length} matching model choices` : "No matching models.";
  }

  function updateAction() {
    apply.disabled = !matches.selectedOptions[0] || matches.selectedOptions[0].matches(":disabled");
  }

  function updateEffort(model, effort, modelChanged = false) {
    const allowed = model?.dataset.efforts?.split(" ").filter(Boolean) || [];
    if (modelChanged && !allowed.includes(effort.value)) effort.value = "";
    for (const option of effort.options) {
      // Keep a submitted value visible beside its validation error.
      const unavailable = option.value && option.value !== effort.value && !allowed.includes(option.value);
      option.hidden = option.disabled = Boolean(unavailable);
    }
    const defaultEffort = model?.dataset.defaultEffort;
    const defaultLabel = defaultEffort && Array.from(effort.options).find(option => option.value === defaultEffort)?.text;
    effort.options[0].text = defaultLabel ? `Default (${defaultLabel})` : "Default";
    const unavailable = !allowed.length && !effort.value && !effort.hasAttribute("aria-invalid");
    effort.disabled = unavailable;
    effort.closest(".ai-effort-control").hidden = unavailable;
  }

  for (const row of document.querySelectorAll(".ai-model-row")) {
    const select = row.querySelector(".ai-model-control select");
    if (!select || select.disabled) continue;
    const label = row.querySelector("label");
    const button = row.querySelector(".ai-model-trigger");
    const value = button.querySelector("[data-model-label]");
    const effort = row.querySelector("[data-reasoning-effort]");
    const update = (modelChanged = false) => {
      const model = select.selectedOptions[0];
      value.textContent = model?.text || "Choose model";
      button.setAttribute("aria-label", `${label.textContent}: ${value.textContent}`);
      if (effort) updateEffort(model, effort, modelChanged);
    };
    button.id = select.id;
    select.removeAttribute("id");
    if (select.hasAttribute("aria-describedby")) button.setAttribute("aria-describedby", select.getAttribute("aria-describedby"));
    if (select.hasAttribute("aria-invalid")) button.setAttribute("aria-invalid", select.getAttribute("aria-invalid"));
    select.hidden = true;
    button.hidden = false;
    update();
    select.addEventListener("change", () => update(true));
    button.addEventListener("click", () => {
      activeSelect = select;
      opener = button;
      title.textContent = `Model for ${label.textContent}`;
      query.value = "";
      filterOptions();
      picker.showModal();
      query.focus();
    });
  }

  query.addEventListener("input", filterOptions);
  matches.addEventListener("change", updateAction);
  apply.addEventListener("click", () => {
    if (activeSelect.value !== matches.value) {
      activeSelect.value = matches.value;
      activeSelect.dispatchEvent(new Event("input", {bubbles: true}));
      activeSelect.dispatchEvent(new Event("change", {bubbles: true}));
    }
    picker.close();
  });
  picker.querySelector("[data-close-picker]").addEventListener("click", () => picker.close());
  picker.addEventListener("close", () => opener?.focus());
})();
