(() => {
  "use strict";
  const announce = (message) => {
    const region = document.querySelector("[data-global-status]");
    if (region) region.textContent = message;
  };
  document.querySelector("[data-error-summary]")?.focus();
  const dirtyForms = new Set();
  document.querySelectorAll("[data-unsaved-form]").forEach((form) => {
    form.addEventListener("input", () => dirtyForms.add(form));
    form.addEventListener("submit", () => dirtyForms.delete(form));
  });
  let queueUnfinished = false;
  window.addEventListener("beforeunload", (event) => {
    if (dirtyForms.size || queueUnfinished) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    if (!navigator.clipboard) { button.hidden = true; return; }
    button.addEventListener("click", async () => {
      const content = document.getElementById(button.dataset.copyTarget);
      try {
        await navigator.clipboard.writeText(content.textContent.trim());
        announce("Checksum copied.");
      } catch {
        announce("Copy is unavailable. Select the checksum text to copy it.");
      }
    });
  });
  const records = document.querySelector(".evidence-records");
  if (records) {
    const selected = records.querySelector('[aria-current="true"]');
    // Only restore scrolling for the current navigation entry; keep evidence out
    // of persistent browser storage. Canonical links/Back remain the baseline.
    const state = history.state || {};
    if (typeof state.evidenceScroll === "number") records.scrollTop = state.evidenceScroll;
    else if (selected && selected.offsetTop > records.clientHeight) selected.scrollIntoView({block:"nearest"});
    records.addEventListener("click", (event) => {
      if (event.target.closest(".evidence-row")) {
        history.replaceState({...history.state, evidenceScroll: records.scrollTop}, "");
      }
    });
  }
  const form = document.querySelector("[data-evidence-upload]");
  if (!form || !window.FormData || !window.XMLHttpRequest || !crypto.randomUUID) return;
  const fileInput = form.querySelector('input[type="file"]');
  const section = document.getElementById("upload-queue");
  if (!fileInput || !section) return;
  const container = section.querySelector("[data-upload-entries]");
  const overall = section.querySelector("[data-upload-status]");
  const stopButton = section.querySelector("[data-stop-uploads]");
  const submit = form.querySelector('button[type="submit"]');
  const limit = Number(form.dataset.maxFileBytes);
  const maxFiles = Number(form.dataset.maxBatchFiles);
  const fields = Array.from(form.querySelectorAll(".field"));
  const fileField = fileInput.closest(".field");
  const csrf = form.querySelector('[name="csrfmiddlewaretoken"]').value;
  let entries = [], running = false, stopRequested = false, blocked = false;
  fileInput.multiple = true;
  fileInput.setAttribute("aria-describedby", `${fileInput.id}_helptext`);
  const fileHelp = document.getElementById(`${fileInput.id}_helptext`);
  if (fileHelp) fileHelp.textContent += ` Select up to ${maxFiles} files, or drop them here. Each file is uploaded separately.`;
  form.prepend(fileField);
  form.insertBefore(section, form.querySelector(".actions"));

  const element = (tag, text, attributes = {}) => {
    const node = document.createElement(tag);
    if (text !== null) node.textContent = text;
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  };
  const inputField = (parent, entry, name, label, type="text", value="") => {
    const wrapper = element("div", null);
    const id = `queue-${entry.id}-${name}`;
    wrapper.append(element("label", label, {for:id}));
    const input = element(type === "textarea" ? "textarea" : "input", null, {id});
    input.dataset.fieldName = name;
    if (type !== "textarea") input.type = type;
    input.value = value;
    if (name === "title") {input.maxLength = 255; input.required = true;}
    if (name === "source") input.maxLength = 255;
    input.addEventListener("input", () => {
      entry.values[name] = input.value;
      dirtyForms.delete(form);
      queueUnfinished = true;
    });
    wrapper.append(input);
    parent.append(wrapper);
    entry.inputs.push(input);
    return input;
  };
  const setStatus = (entry, state, message) => {
    entry.state = state;
    entry.status.textContent = message;
    entry.node.dataset.state = state;
    entry.retry.hidden = !["failed", "unknown"].includes(state) || blocked;
    entry.remove.hidden = ["uploading", "saving", "added", "unknown"].includes(state) || running;
    entry.inputs.forEach((input) => input.disabled = Boolean(entry.frozen) || state === "added" || blocked);
    entry.progress.hidden = !["uploading", "saving"].includes(state);
    queueUnfinished = entries.some((item) => !["added", "removed"].includes(item.state));
  };
  const renderEntry = (file, defaults) => {
    const kind = file.type.startsWith("image/") ? "image" : file.type.startsWith("audio/") ? "audio" : form.dataset.kind;
    const entry = {id:crypto.randomUUID(), file, state:"ready", frozen:null, inputs:[], values:{...defaults, kind, title:defaults.title || file.name}};
    entry.node = element("article", null, {class:"upload-entry"});
    const header = element("div", null, {class:"upload-entry-header"});
    header.append(element("strong", file.name));
    entry.remove = element("button", "Remove from queue", {type:"button", class:"button button-secondary"});
    entry.remove.addEventListener("click", () => {
      if (entry.state === "unknown") {announce("Retry this interrupted submission unchanged before removing it from the queue."); return;}
      entry.state = "removed"; entry.node.remove();
      entries = entries.filter((item) => item !== entry);
      queueUnfinished = entries.some((item) => item.state !== "added");
      submit.disabled = !entries.some((item) => ["ready", "failed", "unknown"].includes(item.state));
      announce(`${file.name} removed from the queue.`);
    });
    header.append(entry.remove); entry.node.append(header);
    const basic = element("div", null, {class:"upload-entry-fields"});
    inputField(basic, entry, "title", "Title", "text", entry.values.title);
    const category = element("div", null);
    const selectId = `queue-${entry.id}-kind`;
    category.append(element("label", "Category", {for:selectId}));
    const select = element("select", null, {id:selectId});
    select.dataset.fieldName = "kind";
    [["document","Document"],["image","Image"],["audio","Audio"]].forEach(([value,label]) => select.append(element("option", label, {value})));
    select.value = kind;
    select.addEventListener("change", () => {entry.values.kind = select.value;});
    entry.inputs.push(select); category.append(select); basic.append(category); entry.node.append(basic);
    const details = element("details", null);
    details.append(element("summary", "Description and source details"));
    inputField(details, entry, "description", "Investigator description", "textarea", defaults.description);
    inputField(details, entry, "source", "Source", "text", defaults.source);
    inputField(details, entry, "source_description", "Source description", "textarea", defaults.source_description);
    inputField(details, entry, "source_url", "Source URL", "url", defaults.source_url);
    inputField(details, entry, "collected_at", "Source collected at (UTC)", "datetime-local", defaults.collected_at);
    entry.node.append(details);
    entry.status = element("p", null, {class:"upload-status", id:`status-${entry.id}`});
    entry.progress = element("progress", null, {max:"100", value:"0", "aria-label":`Upload progress for ${file.name}`});
    entry.retry = element("button", "Retry this file", {class:"button button-secondary", type:"button"});
    entry.retry.addEventListener("click", () => {
      if (running || blocked) return;
      setStatus(entry, "ready", "Ready to retry.");
      run([entry]);
    });
    entry.node.append(entry.status, entry.progress, entry.retry);
    container.append(entry.node);
    setStatus(entry, "ready", `${(file.size / 1024 / 1024).toFixed(2)} MiB · Ready`);
    if (!file.size || file.size > limit) {
      setStatus(entry, "failed", !file.size ? "This file is empty. Remove it and select a nonempty file." : "This file exceeds the configured limit. Remove it and select a smaller file.");
      entry.retry.hidden = true;
      entry.invalidFile = true;
    }
    return entry;
  };
  const loadFiles = (files) => {
    if (running) return;
    if (entries.some((entry) => entry.state === "unknown")) {
      overall.textContent = "Resolve the interrupted upload using Retry before replacing the queue.";
      return;
    }
    const selected = Array.from(files);
    if (selected.length > maxFiles) {
      section.hidden = false;
      overall.textContent = `Choose no more than ${maxFiles} files per batch. Your current queue is unchanged.`;
      submit.disabled = !entries.some((entry) => !entry.invalidFile && ["ready", "failed", "unknown"].includes(entry.state));
      return;
    }
    if (entries.some((entry) => ["ready", "failed"].includes(entry.state)) && !window.confirm("Replace the pending selection? Unsaved descriptions in this queue will be discarded. Previously added evidence stays saved.")) return;
    const defaults = Object.fromEntries(new FormData(form).entries());
    delete defaults.file; delete defaults.csrfmiddlewaretoken; delete defaults.submission_id;
    defaults.title = selected.length === 1 ? (defaults.title || "") : "";
    container.replaceChildren();
    entries = selected.map((file) => renderEntry(file, defaults));
    section.hidden = !entries.length;
    fields.filter((field) => field !== fileField).forEach((field) => {field.hidden = Boolean(entries.length);});
    form.noValidate = Boolean(entries.length);
    submit.textContent = entries.length > 1 ? "Upload files" : "Upload file";
    submit.disabled = !entries.some((entry) => !entry.invalidFile);
    overall.textContent = entries.length ? `${entries.length} file${entries.length === 1 ? "" : "s"} ready for review. Suggested categories can be changed.` : "";
    dirtyForms.delete(form);
    queueUnfinished = entries.length > 0;
    blocked = false;
  };
  fileInput.addEventListener("change", () => loadFiles(fileInput.files));
  form.addEventListener("dragover", (event) => {if (!running && event.dataTransfer.types.includes("Files")) {event.preventDefault(); form.classList.add("drop-active");}});
  form.addEventListener("dragleave", () => form.classList.remove("drop-active"));
  form.addEventListener("drop", (event) => {if (!running && event.dataTransfer.files.length) {event.preventDefault(); form.classList.remove("drop-active"); loadFiles(event.dataTransfer.files);}});
  const send = (entry) => new Promise((resolve) => {
    entry.inputs.forEach((input) => {input.removeAttribute("aria-invalid"); input.removeAttribute("aria-describedby");});
    if (!entry.frozen) entry.frozen = {...entry.values};
    const data = new FormData();
    Object.entries(entry.frozen).forEach(([key,value]) => {if (key !== "body" && value !== undefined) data.set(key, value);});
    data.set("csrfmiddlewaretoken", csrf); data.set("submission_id", entry.id); data.set("file", entry.file, entry.file.name);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", form.dataset.uploadUrl);
    xhr.timeout = 180000;
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("X-CSRFToken", csrf);
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) {
        entry.progress.value = event.loaded / event.total * 100;
        if (event.loaded === event.total) {setStatus(entry, "saving", "Saving original…"); entry.progress.removeAttribute("value");}
      }
    });
    xhr.addEventListener("load", () => {
      let result;
      try {result = JSON.parse(xhr.responseText);} catch {result = null;}
      if ((xhr.status === 200 || xhr.status === 201) && result?.detail_url) {
        setStatus(entry, "added", "Added to this case.");
        const link = element("a", "View evidence", {href:result.detail_url, class:"record-link"});
        entry.node.append(link); announce(`${entry.file.name} added.`);
      } else if ([401,403,404].includes(xhr.status)) {
        blocked = true;
        setStatus(entry, "blocked", result?.message || "Access is unavailable. Sign in again or ask the case owner for access before continuing.");
      } else if (xhr.status === 413 || (xhr.status === 400 && result)) {
        entry.frozen = null;
        entry.id = crypto.randomUUID();
        const errors = result?.errors || {};
        const fieldErrors = Object.values(errors).flat().map((error) => typeof error === "string" ? error : error.message).filter(Boolean).join(" ");
        entry.inputs.forEach((input) => {
          const name = input.dataset.fieldName;
          if (errors[name]) {input.setAttribute("aria-invalid", "true"); input.setAttribute("aria-describedby", entry.status.id);}
        });
        const message = xhr.status === 413 ? "This file exceeds the server's upload limit. Remove it and select a smaller file, or ask the operator to review the configured limit." : fieldErrors || result?.message || "Review this file and its details, then retry.";
        setStatus(entry, "failed", message);
        announce(`${entry.file.name}: ${message}`);
      } else {
        setStatus(entry, "unknown", "The save could not be confirmed. Retry unchanged to check the result without creating a duplicate.");
      }
      resolve();
    });
    ["error", "timeout", "abort"].forEach((name) => xhr.addEventListener(name, () => {
      setStatus(entry, "unknown", "Confirmation was interrupted. Retry unchanged to resolve this submission safely.");
      resolve();
    }));
    setStatus(entry, "uploading", "Uploading original…");
    entry.progress.value = 0; announce(`Uploading ${entry.file.name}.`); xhr.send(data);
  });
  const run = async (pending) => {
    if (running || blocked) return;
    running = true; stopRequested = false;
    submit.disabled = true; fileInput.disabled = true; stopButton.disabled = false;
    entries.forEach((entry) => {entry.remove.hidden = true; entry.retry.disabled = true;});
    for (const entry of pending) {
      if (stopRequested || blocked) break;
      if (entry.invalidFile || !["ready", "failed", "unknown"].includes(entry.state)) continue;
      if (!entry.frozen && !entry.values.title.trim()) {setStatus(entry, "failed", "Enter a title before uploading."); continue;}
      await send(entry);
    }
    running = false; fileInput.disabled = blocked; stopButton.disabled = true;
    entries.forEach((entry) => {entry.retry.disabled = blocked; setStatus(entry, entry.state, entry.status.textContent);});
    submit.disabled = blocked || !entries.some((entry) => !entry.invalidFile && ["ready", "failed", "unknown"].includes(entry.state));
    const count = entries.filter((entry) => entry.state === "added").length;
    overall.textContent = blocked ? "Uploads stopped because case access is unavailable. No remaining queue entries will be sent. Check your case access before continuing." : `${count} of ${entries.length} files added.${stopRequested ? " Pending uploads are paused." : " Review individual results below."}`;
    announce(overall.textContent); dirtyForms.delete(form);
  };
  stopButton.disabled = true;
  stopButton.addEventListener("click", () => {stopRequested = true; stopButton.disabled = true; overall.textContent = "The current file will finish; pending files will stay queued.";});
  form.addEventListener("submit", (event) => {
    if (!entries.length) return;
    event.preventDefault(); dirtyForms.delete(form);
    run(entries.slice());
  });
})();
