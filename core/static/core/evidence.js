(() => {
  "use strict";
  const announce = (message) => {
    const region = document.querySelector("[data-evidence-status]");
    if (region) region.textContent = message;
  };
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
  const revealInfoAnchor = () => {
    const target = document.getElementById(location.hash.slice(1));
    const details = target?.closest(".evidence-info");
    if (details) {
      details.open = true;
      target.scrollIntoView({block: "nearest"});
    }
  };
  revealInfoAnchor();
  window.addEventListener("hashchange", revealInfoAnchor);
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
  if (!form) return;
  let formDirty = false;
  let entries = [];
  form.addEventListener("input", (event) => {
    if (!event.target.closest(".upload-entry")) formDirty = true;
  });
  form.addEventListener("submit", () => { formDirty = false; });
  window.addEventListener("beforeunload", (event) => {
    if (formDirty || entries.some((entry) => entry.state !== "added")) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  if (!window.FormData || !window.XMLHttpRequest || !window.crypto?.randomUUID) return;
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
  const additionalDetails = form.querySelector("[data-additional-evidence-details]");
  const csrf = form.querySelector('[name="csrfmiddlewaretoken"]').value;
  const bulk = form.hasAttribute("data-bulk-upload");
  const sharedDetails = form.querySelector("[data-shared-details]");
  const sharedInputs = Array.from(sharedDetails?.querySelectorAll("input, textarea") || []);
  const newBatch = form.querySelector("[data-new-batch]");
  const uploadActions = form.querySelector("[data-upload-actions]");
  let running = false, stopRequested = false, blocked = false;
  fileInput.multiple = true;
  fileInput.setAttribute("aria-describedby", `${fileInput.id}_helptext`);
  const fileHelp = document.getElementById(`${fileInput.id}_helptext`);
  if (!bulk) {
    if (fileHelp) fileHelp.textContent += ` Select up to ${maxFiles} files, or drop them here. Each file is uploaded separately.`;
    form.prepend(fileField);
    form.insertBefore(section, form.querySelector(".actions"));
  }

  const refreshControls = () => {
    const unknown = entries.some((entry) => entry.state === "unknown");
    const unfinished = entries.some((entry) => entry.state !== "added");
    submit.disabled = running || blocked || !entries.some((entry) => !entry.invalidFile && ["ready", "failed", "unknown"].includes(entry.state));
    fileInput.disabled = running || blocked || unknown;
    stopButton.hidden = !running;
    if (uploadActions) uploadActions.hidden = !entries.length;
    if (sharedDetails) sharedDetails.disabled = running || blocked;
    if (newBatch) newBatch.hidden = running || blocked || unfinished || !entries.length;
    const label = form.querySelector("[data-file-label]");
    if (label) label.textContent = entries.length ? "Add more files" : "Select files";
    submit.textContent = entries.length === 1 || (!bulk && !entries.length) ? "Upload file" : "Upload files";
    if (!bulk) {
      const hasFiles = entries.length > 0;
      fields.filter((field) => field !== fileField).forEach((field) => {field.hidden = hasFiles;});
      if (additionalDetails) additionalDetails.hidden = hasFiles;
      form.noValidate = hasFiles;
    }
  };
  const inheritShared = (entry) => {
    if (blocked || entry.frozen || !["ready", "failed", "duplicate"].includes(entry.state)) return;
    sharedInputs.forEach((shared) => {
      if (entry.overrides.has(shared.name)) return;
      entry.inputs.find((input) => input.dataset.fieldName === shared.name).value = shared.value;
    });
  };
  sharedInputs.forEach((input) => input.addEventListener("input", () => {
    entries.forEach(inheritShared);
    refreshControls();
  }));

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
    if (name === "source_url") input.maxLength = 2048;
    if (name === "source_description") input.maxLength = 20000;
    input.addEventListener("input", () => entry.overrides.add(name));
    wrapper.append(input);
    parent.append(wrapper);
    entry.inputs.push(input);
    return input;
  };
  const setStatus = (entry, state, message) => {
    entry.state = state;
    entry.status.textContent = message;
    entry.node.dataset.state = state;
    entry.retry.hidden = entry.invalidFile || !["failed", "unknown", "duplicate"].includes(state) || blocked;
    entry.retry.textContent = state === "duplicate" ? "Add duplicate anyway" : "Retry this file";
    entry.matches.hidden = state !== "duplicate";
    entry.remove.hidden = ["uploading", "saving", "added", "unknown"].includes(state) || running;
    entry.inputs.forEach((input) => input.disabled = Boolean(entry.frozen) || state === "added" || blocked);
    if (entry.resetShared) entry.resetShared.disabled = Boolean(entry.frozen) || state === "added" || blocked || running;
    entry.progress.hidden = !["uploading", "saving"].includes(state);
  };
  const renderEntry = (file, defaults) => {
    const kind = file.type.startsWith("image/") ? "image" : file.type.startsWith("audio/") ? "audio" : bulk ? "document" : form.dataset.kind;
    const entry = {id:crypto.randomUUID(), file, state:"ready", frozen:null, inputs:[], overrides:new Set(), defaults:{...defaults}};
    entry.node = element("article", null, {class:"upload-entry"});
    const header = element("div", null, {class:"upload-entry-header"});
    header.append(element("strong", file.name));
    entry.remove = element("button", "Remove from queue", {type:"button", class:"button button-secondary"});
    entry.remove.addEventListener("click", () => {
      if (entry.state === "unknown") {announce("Retry this interrupted submission unchanged before removing it from the queue."); return;}
      entry.state = "removed"; entry.node.remove();
      entries = entries.filter((item) => item !== entry);
      refreshControls();
      if (!entries.length) {
        section.hidden = true;
        fileInput.focus();
      }
      overall.textContent = `${entries.filter((item) => item.state === "added").length} of ${entries.length} files added. ${maxFiles - entries.length} places available in this batch.`;
      announce(`${file.name} removed from the queue.`);
    });
    header.append(entry.remove); entry.node.append(header);
    const basic = element("div", null, {class:"upload-entry-fields"});
    entry.title = inputField(basic, entry, "title", "Title", "text", defaults.title || file.name);
    const category = element("div", null);
    const selectId = `queue-${entry.id}-kind`;
    category.append(element("label", "Category", {for:selectId}));
    const select = element("select", null, {id:selectId});
    select.dataset.fieldName = "kind";
    [["document","Document"],["image","Image"],["audio","Audio"]].forEach(([value,label]) => select.append(element("option", label, {value})));
    select.value = kind;
    entry.inputs.push(select); category.append(select); basic.append(category); entry.node.append(basic);
    const details = element("details", null);
    details.append(element("summary", "Source details"));
    inputField(details, entry, "source", "Source", "text", defaults.source);
    inputField(details, entry, "source_description", "Source description", "textarea", defaults.source_description);
    inputField(details, entry, "source_url", "Source URL", "url", defaults.source_url);
    inputField(details, entry, "collected_at", "Source collected at (UTC)", "datetime-local", defaults.collected_at);
    if (bulk) {
      details.append(element("p", "Source fields follow shared details until you edit them here. Clearing a field also overrides its shared value.", {class:"muted"}));
      entry.resetShared = element("button", "Use shared details", {type:"button", class:"button button-secondary"});
      entry.resetShared.addEventListener("click", () => {
        entry.overrides.clear();
        inheritShared(entry);
        announce(`Shared source details restored for ${file.name}.`);
      });
      details.append(entry.resetShared);
    }
    entry.node.append(details);
    entry.status = element("p", null, {class:"upload-status", id:`status-${entry.id}`});
    entry.matches = element("div", null, {class:"notice", "aria-label":"Matching evidence"});
    entry.progress = element("progress", null, {max:"100", value:"0", "aria-label":`Upload progress for ${file.name}`});
    entry.retry = element("button", "Retry this file", {class:"button button-secondary", type:"button"});
    entry.retry.addEventListener("click", () => {
      if (running || blocked) return;
      setStatus(entry, "ready", "Ready to retry.");
      run([entry]);
    });
    entry.node.append(entry.status, entry.matches, entry.progress, entry.retry);
    container.append(entry.node);
    setStatus(entry, "ready", `${(file.size / 1024 / 1024).toFixed(2)} MiB · Ready`);
    if (!file.size || file.size > limit) {
      setStatus(entry, "failed", !file.size ? "This file is empty. Remove it and select a nonempty file." : "This file exceeds the configured limit. Remove it and select a smaller file.");
      entry.invalidFile = true;
      entry.retry.hidden = true;
    }
    return entry;
  };
  const loadFiles = (files) => {
    if (running || blocked) return;
    if (entries.some((entry) => entry.state === "unknown")) {
      overall.textContent = "Resolve the interrupted upload using Retry before selecting more files.";
      return;
    }
    const selected = Array.from(files);
    if (!selected.length) return;
    if (selected.length + (bulk ? entries.length : 0) > maxFiles) {
      section.hidden = false;
      const available = bulk ? maxFiles - entries.length : maxFiles;
      overall.textContent = `This batch has room for ${available} more file${available === 1 ? "" : "s"} (${maxFiles} per batch). Your current queue is unchanged.`;
      refreshControls();
      return;
    }
    if (!bulk && entries.some((entry) => ["ready", "failed", "duplicate"].includes(entry.state)) && !window.confirm("Replace the pending selection? Unsaved changes in this queue will be discarded. Previously added evidence stays saved.")) return;
    const defaults = Object.fromEntries(new FormData(form).entries());
    delete defaults.file; delete defaults.csrfmiddlewaretoken; delete defaults.submission_id;
    defaults.title = !bulk && selected.length === 1 ? (defaults.title || "") : "";
    if (!bulk) {container.replaceChildren(); entries = [];}
    entries.push(...selected.map((file) => renderEntry(file, defaults)));
    section.hidden = !entries.length;
    overall.textContent = `${entries.length} file${entries.length === 1 ? "" : "s"} in this batch; ${entries.filter((entry) => entry.state === "added").length} added. Review titles and suggested categories before uploading.`;
    formDirty = false;
    blocked = false;
    refreshControls();
  };
  fileInput.addEventListener("change", () => {loadFiles(fileInput.files); fileInput.value = "";});
  form.addEventListener("dragover", (event) => {if (event.dataTransfer.types.includes("Files")) {event.preventDefault(); if (!fileInput.disabled) form.classList.add("drop-active");}});
  form.addEventListener("dragleave", () => form.classList.remove("drop-active"));
  form.addEventListener("drop", (event) => {if (event.dataTransfer.files.length) {event.preventDefault(); form.classList.remove("drop-active"); loadFiles(event.dataTransfer.files);}});
  const send = (entry) => new Promise((resolve) => {
    entry.inputs.forEach((input) => {input.removeAttribute("aria-invalid"); input.removeAttribute("aria-describedby");});
    if (!entry.frozen) {
      // Read the editable fields once, then reuse the exact payload on uncertain retries.
      entry.frozen = {...entry.defaults};
      for (const input of entry.inputs) entry.frozen[input.dataset.fieldName] = input.value;
    }
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
      } else if (xhr.status === 409 && result?.code === "duplicate_evidence") {
        entry.frozen = null;
        entry.defaults.duplicate_confirmation = result.confirmation;
        entry.matches.replaceChildren(element("p", "Matching evidence already exists. Review matches in a new tab, remove this file from the queue, or add a separate copy for its source."));
        const list = element("ul", null);
        result.matches.forEach((match) => {
          const row = element("li", null);
          row.append(element("a", `${match.title} · ${match.label}${match.is_archived ? " · Archived" : ""} (opens in a new tab)`, {href:match.url, target:"_blank", rel:"noopener", class:"record-link"}));
          list.append(row);
        });
        entry.matches.append(list);
        if (result.has_more) entry.matches.append(element("p", "Showing the first 10 matches."));
        setStatus(entry, "duplicate", "Exact duplicate found in this case. This file has not been added.");
        announce(`${entry.file.name}: exact duplicate found. Review matching evidence before adding another copy.`);
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
    refreshControls(); stopButton.disabled = false;
    if (bulk) entries.forEach((entry) => {if (entry.resetShared) entry.resetShared.disabled = true;});
    entries.forEach((entry) => {entry.remove.hidden = true; entry.retry.disabled = true;});
    for (const entry of pending) {
      if (stopRequested || blocked) break;
      if (entry.invalidFile || !["ready", "failed", "unknown"].includes(entry.state)) continue;
      if (!entry.frozen && !entry.title.value.trim()) {setStatus(entry, "failed", "Enter a title before uploading."); continue;}
      await send(entry);
    }
    running = false; stopButton.disabled = true;
    entries.forEach((entry) => {entry.retry.disabled = blocked; setStatus(entry, entry.state, entry.status.textContent);});
    refreshControls();
    const count = entries.filter((entry) => entry.state === "added").length;
    const duplicates = entries.filter((entry) => entry.state === "duplicate").length;
    overall.textContent = blocked ? "Uploads stopped because case access is unavailable. No remaining queue entries will be sent. Check your case access before continuing." : `${count} of ${entries.length} files added.${duplicates ? ` ${duplicates} exact duplicate${duplicates === 1 ? " needs" : "s need"} review.` : ""}${stopRequested ? " Pending uploads are paused." : " Review individual results below."}`;
    announce(overall.textContent);
    formDirty = false;
  };
  stopButton.disabled = true;
  stopButton.addEventListener("click", () => {stopRequested = true; stopButton.disabled = true; overall.textContent = "The current file will finish; pending files will stay queued.";});
  form.addEventListener("submit", (event) => {
    if (!entries.length && !bulk) return;
    event.preventDefault();
    run(entries.slice());
  });
  if (bulk) {
    form.noValidate = true;
    form.hidden = false;
    document.querySelector("[data-bulk-fallback]").hidden = true;
    refreshControls();
  }
})();
