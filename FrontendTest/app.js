/* FrontendTest shared client. One script for every page: it hydrates whatever
 * live-data hooks the current page exposes (by id), then keeps them in sync over
 * the locked /ws/events socket. Pages opt in purely by including the matching
 * container ids -- a page without a hook simply skips that renderer.
 *
 * Live-data hooks:
 *   #protocol-cards  #inventory-rows  #log-rows
 *   #step-tracker    #step-current    #timer-list   #live-transcript
 *
 * Outer event types (locked): transcript_update, command_result, timer_update, error.
 * command_result dispatches on payload.kind -- never a new outer type.
 */
(function () {
  "use strict";

  const API = "";
  const $ = (id) => document.getElementById(id);
  const INVENTORY_UNITS = [
    "mL",
    "uL",
    "L",
    "g",
    "mg",
    "ug",
    "bottles",
    "plates",
    "aliquots",
    "doses",
    "reactions",
  ];

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtClock(total) {
    const s = Math.max(0, total | 0);
    const m = Math.floor(s / 60);
    return `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }

  function fmtTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? String(iso) : d.toLocaleString();
  }

  // --- REST fetchers ---------------------------------------------------------
  async function getJSON(path) {
    const r = await fetch(API + path);
    if (!r.ok) throw new Error(`${path} -> ${r.status}`);
    return r.json();
  }

  async function fetchProtocols() {
    return (await getJSON("/api/protocols")).protocols || [];
  }
  async function fetchRecent() {
    return (await getJSON("/api/protocols/recent")).recent || [];
  }
  async function fetchFullProtocol(id) {
    return (await getJSON("/api/protocols/" + encodeURIComponent(id))).protocol;
  }
  async function patchProtocol(id, body) {
    const r = await fetch(API + "/api/protocols/" + encodeURIComponent(id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return r.json();
  }
  async function fetchInventory() {
    return (await getJSON("/api/inventory")).items || [];
  }
  async function fetchLog() {
    return (await getJSON("/api/log")).log || [];
  }
  async function fetchState() {
    return getJSON("/api/state");
  }
  async function fetchScale(body) {
    const res = await fetch("/api/scale", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Could not scale reagents");
    }
    return data;
  }

  async function fetchScaleWithPriority(body) {
    const res = await fetch("/api/scale/with-priority", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Could not compute deductions");
    return data;
  }

  async function consumeReagents(deductions) {
    const res = await fetch("/api/inventory/consume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deductions })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Could not consume reagents");
    return data;
  }

  async function loadProtocol(id) {
    await fetch(`${API}/api/protocols/${encodeURIComponent(id)}/load`, {
      method: "POST",
    });
    // Loading lands on the Guide; flag the prep modal to pop up once it hydrates.
    flagPrepOnLoad();
    window.location.href = "guide.html";
  }

  async function importProtocol(text, name) {
    const r = await fetch(API + "/api/protocols/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, name: name || null }),
    });
    return r.json();
  }

  async function importProtocolFromFile(file, name) {
    const fd = new FormData();
    fd.append("file", file);
    if (name) fd.append("name", name);
    // No Content-Type header: the browser sets the multipart boundary itself.
    const r = await fetch(API + "/api/protocols/import/file", {
      method: "POST",
      body: fd,
    });
    return r.json();
  }

  async function deleteProtocol(id) {
    const r = await fetch(`${API}/api/protocols/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
    if (!r.ok) throw new Error(`delete failed (${r.status})`);
    await refreshProtocols();
  }

  async function refreshProtocols() {
    if ($("protocol-cards")) renderProtocolCards(await fetchProtocols());
  }

  async function refreshRecent() {
    if ($("recent-protocols")) renderRecentProtocols(await fetchRecent());
  }

  function setImportResult(message, tone) {
    const result = $("import-result");
    if (result) {
      result.textContent = message;
      result.className = `text-sm mb-4 min-h-[1.25rem] ${tone}`;
    }
  }

  // Indeterminate progress: the import is an LLM call with no knowable duration,
  // so we show an animated bar (honest motion) rather than a fabricated percent.
  // A final setImportResult() call uses textContent, which replaces this markup.
  function setImportLoading(message) {
    const result = $("import-result");
    if (!result) return;
    result.className = "mb-4 min-h-[1.25rem]";
    result.innerHTML =
      `<div class="flex items-center gap-3 text-sm text-on-surface-variant">` +
      `<div class="relative h-1 flex-1 overflow-hidden rounded-full bg-surface-variant">` +
      `<div class="import-progress-bar absolute h-full w-2/5 rounded-full bg-primary"></div>` +
      `</div><span>${escapeHtml(message)}</span></div>`;
  }

  function resetImportForm() {
    ["import-text", "import-name", "import-file"].forEach((id) => {
      const el = $(id);
      if (el) el.value = "";
    });
    const label = $("import-file-label");
    if (label) label.textContent = "Drop a PDF here or click to choose";
  }

  async function handleProtocolImport() {
    const name = ($("import-name") || {}).value || "";
    const text = ($("import-text") || {}).value || "";
    const fileInput = $("import-file");
    const file = fileInput && fileInput.files && fileInput.files[0];
    if (!file && !text.trim()) {
      setImportResult("Paste at least one step or drop a PDF.", "text-tertiary");
      return;
    }
    // Disable the submit button for the in-flight request so a second click
    // can't fire a duplicate upload (imports are slow LLM/PDF calls).
    setImportSubmitDisabled(true);
    setImportLoading(file ? "Reading PDF…" : "Importing…");
    try {
      const data = file
        ? await importProtocolFromFile(file, name)
        : await importProtocol(text, name);
      if (data.ok) {
        setImportResult(
          `Imported "${data.protocol.name}". ${data.load_hint || ""}`,
          "text-secondary"
        );
        await refreshProtocols();
        resetImportForm();
        setTimeout(closeImportModal, 1200);
      } else {
        // {ok:false} carries `error`; a 4xx (wrong type / too large) carries `detail`.
        setImportResult(data.error || data.detail || "Import failed.", "text-tertiary");
      }
    } catch (e) {
      setImportResult("Import failed: " + e.message, "text-tertiary");
    } finally {
      setImportSubmitDisabled(false);
    }
  }

  function setImportSubmitDisabled(disabled) {
    const submit = $("import-submit");
    if (submit) submit.disabled = disabled;
  }

  function openImportModal() {
    const m = $("import-modal");
    if (m) m.classList.remove("hidden");
  }
  function closeImportModal() {
    const m = $("import-modal");
    if (m) m.classList.add("hidden");
  }

  function wireImportModal() {
    const open = $("import-protocol");
    if (!open) return;
    open.addEventListener("click", openImportModal);
    ["import-cancel", "import-cancel-2"].forEach((id) => {
      const b = $(id);
      if (b) b.addEventListener("click", closeImportModal);
    });
    const submit = $("import-submit");
    if (submit) submit.addEventListener("click", handleProtocolImport);
    const modal = $("import-modal");
    if (modal)
      modal.addEventListener("click", (e) => {
        if (e.target === modal) closeImportModal();
      });
    wireImportDropzone();
  }

  function wireImportDropzone() {
    const fileInput = $("import-file");
    const dropzone = $("import-dropzone");
    const label = $("import-file-label");
    if (!fileInput || !dropzone) return;
    const showName = (file) => {
      if (label) label.textContent = file ? file.name : "Drop a PDF here or click to choose";
    };
    fileInput.addEventListener("change", () =>
      showName(fileInput.files && fileInput.files[0])
    );
    ["dragover", "dragenter"].forEach((ev) =>
      dropzone.addEventListener(ev, (e) => {
        e.preventDefault();
        dropzone.classList.add("border-primary", "text-on-surface");
      })
    );
    ["dragleave", "drop"].forEach((ev) =>
      dropzone.addEventListener(ev, () =>
        dropzone.classList.remove("border-primary", "text-on-surface")
      )
    );
    dropzone.addEventListener("drop", (e) => {
      e.preventDefault();
      const dropped = e.dataTransfer && e.dataTransfer.files;
      if (dropped && dropped.length) {
        fileInput.files = dropped; // surface the dropped file to the form + submit
        showName(dropped[0]);
      }
    });
  }

  // --- edit protocol (structured: name + description + steps) ----------------
  // The card's Edit button opens this; it fetches the FULL protocol (steps incl.
  // params), lets the user edit text + duration + order, and PATCHes it back.
  function setEditResult(message, tone) {
    const el = $("edit-protocol-result");
    if (el) el.className = `text-sm mb-3 min-h-[1.25rem] ${tone || ""}`;
    if (el) el.textContent = message;
  }

  // A step's duration_s shows as the nicest whole unit: 600 -> "10 min",
  // 90 -> "90 sec", null -> empty.
  function durationToField(s) {
    if (!s) return { value: "", unit: "min" };
    return s % 60 === 0 ? { value: String(s / 60), unit: "min" } : { value: String(s), unit: "sec" };
  }
  function fieldToDuration(value, unit) {
    const v = parseFloat(value);
    if (!value || isNaN(v) || v <= 0) return null;
    return unit === "min" ? Math.round(v * 60) : Math.round(v);
  }

  function renumberSteps() {
    const list = $("edit-steps-list");
    if (!list) return;
    [...list.children].forEach((row, i) => {
      const n = row.querySelector(".edit-step-num");
      if (n) n.textContent = String(i + 1);
    });
  }

  // Drag-reorder: first row whose vertical midpoint is below the cursor, or null
  // to drop at the end (the MDN reorder idiom). The dragged row is excluded.
  function rowAfterCursor(list, y) {
    const rows = [...list.querySelectorAll(".edit-step-row:not(.dragging)")];
    return rows.reduce(
      (closest, row) => {
        const box = row.getBoundingClientRect();
        const offset = y - box.top - box.height / 2;
        return offset < 0 && offset > closest.offset ? { offset, element: row } : closest;
      },
      { offset: Number.NEGATIVE_INFINITY, element: null }
    ).element;
  }

  // Each row stashes its source step's params + timer_label on a JS property
  // (row._step), NOT in the DOM, so reorder/delete carry them with the row.
  function addStepRow(step) {
    const list = $("edit-steps-list");
    if (!list) return;
    step = step || { text: "", duration_s: null, timer_label: null, parameters: {} };
    const { value, unit } = durationToField(step.duration_s);
    const iconBtn =
      "w-8 h-8 rounded-lg flex items-center justify-center text-on-surface-variant hover:bg-surface-variant hover:text-on-surface transition-colors active:scale-95 shrink-0";
    const row = document.createElement("div");
    row.className = "edit-step-row flex items-center gap-2";
    row.innerHTML = `
      <span class="edit-step-drag material-symbols-outlined text-on-surface-variant text-[20px] shrink-0 cursor-grab select-none active:cursor-grabbing" draggable="true" title="Drag to reorder" aria-label="Drag to reorder step">drag_indicator</span>
      <span class="edit-step-num text-on-surface-variant text-sm w-5 text-right shrink-0"></span>
      <input class="edit-step-text flex-1 bg-surface-container-lowest border border-outline-variant rounded-lg px-3 py-2 text-on-surface text-base" type="text" placeholder="Step description" />
      <input class="edit-step-dur w-16 bg-surface-container-lowest border border-outline-variant rounded-lg px-2 py-2 text-on-surface text-sm" inputmode="decimal" placeholder="0" />
      <select class="edit-step-unit bg-surface-container-lowest border border-outline-variant rounded-lg px-2 py-2 text-on-surface text-sm">
        <option value="min">min</option><option value="sec">sec</option>
      </select>
      <button type="button" class="edit-step-remove ${iconBtn} hover:bg-error/10 hover:text-error" title="Remove step" aria-label="Remove step"><span class="material-symbols-outlined text-[18px]">close</span></button>`;
    row.querySelector(".edit-step-text").value = step.text || "";
    row.querySelector(".edit-step-dur").value = value;
    row.querySelector(".edit-step-unit").value = unit;
    row._step = { timer_label: step.timer_label || null, parameters: step.parameters || {} };
    list.appendChild(row);
    renumberSteps();
  }

  async function openEditProtocolModal(id) {
    const modal = $("edit-protocol-modal");
    if (!modal) return;
    modal.dataset.protocolId = id;
    modal.classList.remove("hidden");
    setEditResult("Loading…", "text-on-surface-variant");
    try {
      const proto = await fetchFullProtocol(id);
      $("edit-protocol-name").value = proto.name || "";
      $("edit-protocol-description").value = proto.description || "";
      const list = $("edit-steps-list");
      list.innerHTML = "";
      (proto.steps || []).forEach((s) => addStepRow(s));
      if (!list.children.length) addStepRow();
      setEditResult("");
    } catch (e) {
      setEditResult("Could not load protocol: " + e.message, "text-tertiary");
    }
  }

  function closeEditProtocolModal() {
    const m = $("edit-protocol-modal");
    if (m) m.classList.add("hidden");
  }

  function collectEditSteps() {
    const list = $("edit-steps-list");
    if (!list) return [];
    return [...list.children].map((row) => {
      const text = (row.querySelector(".edit-step-text").value || "").trim();
      const duration_s = fieldToDuration(
        row.querySelector(".edit-step-dur").value,
        row.querySelector(".edit-step-unit").value
      );
      const src = row._step || {};
      return {
        text,
        duration_s,
        // mirror the backend rule: a label only rides a timed step
        timer_label: duration_s ? src.timer_label || null : null,
        parameters: src.parameters || {},
      };
    });
  }

  async function handleProtocolEdit() {
    const modal = $("edit-protocol-modal");
    const id = modal && modal.dataset.protocolId;
    if (!id) return;
    const name = ($("edit-protocol-name").value || "").trim();
    const description = ($("edit-protocol-description").value || "").trim();
    const steps = collectEditSteps();
    if (!name) return setEditResult("Name can't be empty.", "text-tertiary");
    if (!steps.length || steps.some((s) => !s.text))
      return setEditResult("Every step needs text (remove blank rows).", "text-tertiary");
    setEditResult("Saving…", "text-on-surface-variant");
    try {
      const data = await patchProtocol(id, { name, description, steps });
      if (data.ok) {
        setEditResult("Saved.", "text-secondary");
        await refreshProtocols();
        await refreshRecent();
        setTimeout(closeEditProtocolModal, 800);
      } else {
        setEditResult(data.error || data.detail || "Save failed.", "text-tertiary");
      }
    } catch (e) {
      setEditResult("Save failed: " + e.message, "text-tertiary");
    }
  }

  function wireEditProtocolModal() {
    const modal = $("edit-protocol-modal");
    if (!modal) return;
    ["edit-protocol-cancel", "edit-protocol-cancel-2"].forEach((id) => {
      const b = $(id);
      if (b) b.addEventListener("click", closeEditProtocolModal);
    });
    const submit = $("edit-protocol-submit");
    if (submit) submit.addEventListener("click", handleProtocolEdit);
    const add = $("edit-step-add");
    if (add) add.addEventListener("click", () => addStepRow());
    modal.addEventListener("click", (e) => {
      if (e.target === modal) closeEditProtocolModal();
    });
    // Delegate per-row remove + drag-reorder so they survive re-renders.
    const list = $("edit-steps-list");
    if (list) {
      list.addEventListener("click", (ev) => {
        const row = ev.target.closest(".edit-step-row");
        if (row && ev.target.closest(".edit-step-remove")) {
          row.remove();
          renumberSteps();
        }
      });
      // Reorder by dragging the left grip. Drag events bubble, so delegate once.
      list.addEventListener("dragstart", (ev) => {
        const handle = ev.target.closest(".edit-step-drag");
        if (!handle) return;
        const row = handle.closest(".edit-step-row");
        row.classList.add("dragging", "opacity-50");
        if (ev.dataTransfer) {
          ev.dataTransfer.effectAllowed = "move";
          // Drag the whole row as the ghost, not just the tiny grip glyph.
          ev.dataTransfer.setDragImage(row, 16, row.offsetHeight / 2);
        }
      });
      list.addEventListener("dragover", (ev) => {
        const dragging = list.querySelector(".dragging");
        if (!dragging) return;
        ev.preventDefault(); // mark this a valid drop target
        const after = rowAfterCursor(list, ev.clientY);
        if (after == null) list.appendChild(dragging);
        else list.insertBefore(dragging, after);
      });
      list.addEventListener("dragend", (ev) => {
        const row = ev.target.closest(".edit-step-row");
        if (row) row.classList.remove("dragging", "opacity-50");
        renumberSteps();
      });
    }
  }

  // --- inventory add / edit / delete ----------------------------------------
  let inventoryCache = []; // last-rendered items, for edit prefill (keyed by id)
  let editingId = null; // null = add mode; number = id of the item being edited

  async function refreshInventory() {
    if ($("inventory-rows")) renderInventory(await fetchInventory());
  }

  async function addInventoryItem(payload) {
    const r = await fetch(API + "/api/inventory", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `add failed (${r.status})`);
    return data;
  }

  async function updateInventoryItem(id, payload) {
    const r = await fetch(`${API}/api/inventory/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `update failed (${r.status})`);
    return data;
  }

  async function deleteInventoryItem(id) {
    const r = await fetch(`${API}/api/inventory/${id}`, { method: "DELETE" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `delete failed (${r.status})`);
    return data;
  }

  // Dismiss one timer (the card's delete button). The backend removes it and
  // broadcasts timer_removed, which drops the card via onTimerRemoved — so we do
  // NOT mutate the local `timers` map here (single source of truth = the WS bus).
  async function stopTimer(id) {
    const r = await fetch(`${API}/api/timers/${encodeURIComponent(id)}/stop`, { method: "POST" });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      throw new Error(data.detail || `stop failed (${r.status})`);
    }
  }

  function populateInventoryUnits() {
    const options = $("additem-unit-options");
    if (!options || options.dataset.populated) return;
    options.innerHTML = INVENTORY_UNITS.map(
      (unit) => `<option value="${escapeHtml(unit)}"></option>`
    ).join("");
    options.dataset.populated = "1";
  }

  function formatInventoryAmount(item) {
    const { amount, unit } = item || {};
    const cleanAmount = String(amount || "").trim();
    const cleanUnit = String(unit || "").trim();
    if (cleanAmount && cleanUnit) return `${cleanAmount} ${cleanUnit}`;
    if (cleanAmount) return cleanAmount;
    return String((item && item.quantity_approx) || "").trim() || "—";
  }

  // <input type=date> only accepts YYYY-MM-DD; ignore anything else (e.g. "N/A").
  const asDateInputValue = (v) => (/^\d{4}-\d{2}-\d{2}$/.test(v || "") ? v : "");

  function setModalMode(mode) {
    const title = $("additem-title");
    const submit = $("additem-submit");
    if (mode === "edit") {
      if (title) title.textContent = "Edit Inventory Item";
      if (submit) submit.textContent = "Save";
    } else {
      if (title) title.textContent = "Add Inventory Item";
      if (submit) submit.textContent = "Add Item";
    }
  }

  function clearAddItemForm() {
    populateInventoryUnits();
    ["additem-name", "additem-amount", "additem-unit", "additem-location", "additem-date"].forEach(
      (id) => {
        const el = $(id);
        if (el) el.value = "";
      }
    );
    const res = $("additem-result");
    if (res) {
      res.textContent = "";
      res.className = "text-sm mb-4 min-h-[1.25rem]";
    }
  }

  function openAddItemModal() {
    const m = $("additem-modal");
    if (!m) return;
    editingId = null;
    setModalMode("add");
    clearAddItemForm();
    // Default "Date Created" to today for convenience.
    const dc = $("additem-date");
    if (dc) dc.value = new Date().toISOString().slice(0, 10);
    m.classList.remove("hidden");
    const name = $("additem-name");
    if (name) name.focus();
  }

  function openEditItemModal(id) {
    const m = $("additem-modal");
    const it = inventoryCache.find((x) => x.id === id);
    if (!m || !it) return;
    editingId = id;
    setModalMode("edit");
    clearAddItemForm();
    const set = (id, val) => {
      const el = $(id);
      if (el) el.value = val || "";
    };
    set("additem-name", it.name);
    set("additem-amount", it.amount);
    set("additem-unit", it.unit);
    set("additem-location", it.location);
    set("additem-date", asDateInputValue(it.date));
    m.classList.remove("hidden");
    const name = $("additem-name");
    if (name) name.focus();
  }

  function closeAddItemModal() {
    const m = $("additem-modal");
    if (m) m.classList.add("hidden");
  }

  // Lightweight transient toast (top-center). Message set via textContent.
  function showToast(message) {
    let hostEl = $("toast-host");
    if (!hostEl) {
      hostEl = document.createElement("div");
      hostEl.id = "toast-host";
      hostEl.className =
        "fixed top-6 left-1/2 -translate-x-1/2 z-[80] flex flex-col items-center gap-2 pointer-events-none";
      document.body.appendChild(hostEl);
    }
    const toast = document.createElement("div");
    toast.className =
      "pointer-events-auto bg-surface-container-high border border-outline-variant text-on-surface px-4 py-3 rounded-xl shadow-2xl flex items-center gap-2 text-sm transition-all duration-300 opacity-0 -translate-y-2";
    const icon = document.createElement("span");
    icon.className = "material-symbols-outlined text-secondary";
    icon.textContent = "check_circle";
    const label = document.createElement("span");
    label.textContent = message;
    toast.append(icon, label);
    hostEl.appendChild(toast);
    requestAnimationFrame(() =>
      toast.classList.remove("opacity-0", "-translate-y-2")
    );
    setTimeout(() => {
      toast.classList.add("opacity-0", "-translate-y-2");
      setTimeout(() => toast.remove(), 350);
    }, 2600);
  }

  async function handleSubmitItem() {
    const result = $("additem-result");
    const setMsg = (msg, cls) => {
      if (result) {
        result.textContent = msg;
        result.className = "text-sm mb-4 min-h-[1.25rem] " + cls;
      }
    };
    const name = (($("additem-name") || {}).value || "").trim();
    const amount = (($("additem-amount") || {}).value || "").trim();
    const unit = (($("additem-unit") || {}).value || "").trim();
    const location = (($("additem-location") || {}).value || "").trim();
    const date = (($("additem-date") || {}).value || "").trim();
    if (!name) {
      setMsg("Reagent name is required.", "text-tertiary");
      return;
    }
    setMsg("Saving...", "text-on-surface-variant");
    const isEdit = editingId !== null;
    try {
      if (isEdit) {
        await updateInventoryItem(editingId, { name, amount, unit, location, date });
      } else {
        await addInventoryItem({ name, amount, unit, location, date });
      }
      await refreshInventory();
      clearAddItemForm();
      // Close immediately on success and stay closed; a toast confirms it.
      closeAddItemModal();
      showToast(`"${name}" was ${isEdit ? "updated" : "added"}`);
    } catch (e) {
      setMsg(`Could not ${isEdit ? "update" : "add"} item: ` + e.message, "text-tertiary");
    }
  }

  async function handleDeleteItem(id) {
    const it = inventoryCache.find((x) => x.id === id);
    const name = it ? it.name : "this item";
    if (!window.confirm(`Delete "${name}" from inventory?`)) return;
    try {
      await deleteInventoryItem(id);
      await refreshInventory();
      showToast(`"${name}" was deleted`);
    } catch (e) {
      showToast("Could not delete: " + e.message);
    }
  }

  function wireAddItemModal() {
    const open = $("add-item");
    if (!open) return;
    populateInventoryUnits();
    open.addEventListener("click", openAddItemModal);
    ["additem-cancel", "additem-cancel-2"].forEach((id) => {
      const b = $(id);
      if (b) b.addEventListener("click", closeAddItemModal);
    });
    const submit = $("additem-submit");
    if (submit) submit.addEventListener("click", handleSubmitItem);
    const modal = $("additem-modal");
    if (modal)
      modal.addEventListener("click", (e) => {
        if (e.target === modal) closeAddItemModal();
      });
    // Edit/delete are delegated on the (persistent) rows container, since its
    // innerHTML is replaced on every render.
    const rowsHost = $("inventory-rows");
    if (rowsHost)
      rowsHost.addEventListener("click", (e) => {
        const editBtn = e.target.closest(".inv-edit");
        const delBtn = e.target.closest(".inv-delete");
        if (editBtn) openEditItemModal(parseInt(editBtn.dataset.id, 10));
        else if (delBtn) handleDeleteItem(parseInt(delBtn.dataset.id, 10));
      });
  }

  // --- manual log entry modal (notebook "Manual Entry") ---------------------
  function openLogModal() {
    const m = $("log-modal");
    if (!m) return;
    const res = $("log-result");
    if (res) res.textContent = "";
    m.classList.remove("hidden");
    populateLogProtocols(); // fill "Related protocol" from the live library
    const t = $("log-text");
    if (t) t.focus();
  }
  function closeLogModal() {
    const m = $("log-modal");
    if (m) m.classList.add("hidden");
  }
  // Fill the manual-entry "Related protocol" dropdown from the loaded protocols.
  // The chosen name rides along as the entry's category (its badge); "— None —"
  // sends no category. A failed fetch leaves just the None option, never an error.
  async function populateLogProtocols() {
    const sel = $("log-protocol");
    if (!sel) return;
    const prev = sel.value;
    let protocols = [];
    try {
      protocols = await fetchProtocols();
    } catch (_) {
      protocols = [];
    }
    sel.innerHTML =
      `<option value="">— None —</option>` +
      protocols
        .map((p) => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`)
        .join("");
    if (prev) sel.value = prev; // keep a pending pick across re-opens
  }
  async function submitLogForm(e) {
    if (e) e.preventDefault();
    const textEl = $("log-text");
    const protocolEl = $("log-protocol");
    const result = $("log-result");
    const text = ((textEl && textEl.value) || "").trim();
    const protocol = ((protocolEl && protocolEl.value) || "").trim();
    if (!text) {
      if (result) result.textContent = "Enter an observation to log.";
      return;
    }
    if (result) result.textContent = "Saving...";
    try {
      // The related protocol is stored as the entry category (its feed badge).
      await postLog(text, null, protocol || null);
      if (textEl) textEl.value = "";
      if (protocolEl) protocolEl.value = "";
      await refreshNotebookFeed();
      if (result) result.textContent = "";
      closeLogModal();
    } catch (err) {
      if (result) result.textContent = "Could not save: " + err.message;
    }
  }
  function wireLogModal() {
    const open = $("log-add");
    if (!open) return;
    open.addEventListener("click", openLogModal);
    const cancel = $("log-cancel");
    if (cancel) cancel.addEventListener("click", closeLogModal);
    const form = $("log-form");
    if (form) form.addEventListener("submit", submitLogForm);
    const modal = $("log-modal");
    if (modal)
      modal.addEventListener("click", (e) => {
        if (e.target === modal) closeLogModal();
      });
  }

  async function postLog(text, sample_id, category) {
    const r = await fetch(API + "/api/log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, sample_id: sample_id || null, category: category || null }),
    });
    return r.json();
  }

  // --- export the active notebook (client-side, no backend call) -------------
  // Serializes the in-memory logCache — the active notebook's entries, in the same
  // order the Activity Stream shows (sortLog) — to a downloadable Markdown/CSV file
  // or a print-to-PDF window. The typed/voice spine is untouched; this is read-only
  // over data already hydrated, so it can't break the demo.
  function notebookTitle() {
    const el = $("notebook-title");
    return (((el && el.textContent) || "").trim()) || "Notebook";
  }
  function notebookExportBase() {
    const safe = notebookTitle().replace(/[^\w-]+/g, "_").replace(/^_+|_+$/g, "");
    const date = new Date().toISOString().slice(0, 10);
    return `${safe || "Notebook"}_${date}`;
  }
  // Provenance/context chips for one entry, shared by the Markdown and print views.
  function exportEntryMeta(e) {
    const meta = [e.entry_type === "manual" ? "Manual" : "Automatic"];
    if (e.category) meta.push(e.category);
    if (e.step_ref != null) meta.push("Step " + e.step_ref);
    if (e.sample_id) meta.push("Sample " + e.sample_id);
    if (e.edited) meta.push("edited");
    if (e.flag && e.flag.status)
      meta.push(e.flag.status === "mismatch" ? "reproducibility mismatch" : "reproducibility ok");
    return meta;
  }
  function exportNotebookMarkdown(rows) {
    const n = rows.length;
    const lines = [
      `# ${notebookTitle()}`,
      "",
      `_Exported ${fmtTime(new Date().toISOString())} • ${n} ${n === 1 ? "entry" : "entries"}_`,
      "",
    ];
    for (const e of rows) {
      lines.push(`### ${fmtTime(e.timestamp)}`);
      lines.push(`*${exportEntryMeta(e).join(" • ")}*`);
      lines.push("");
      lines.push((e.text || "").trim());
      lines.push("");
    }
    return lines.join("\n");
  }
  function csvCell(v) {
    const s = v == null ? "" : String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }
  function exportNotebookCSV(rows) {
    const cols = ["timestamp", "entry_type", "category", "step_ref", "sample_id", "flag", "edited", "text"];
    const out = [cols.join(",")];
    for (const e of rows) {
      out.push(
        [
          e.timestamp,
          e.entry_type,
          e.category,
          e.step_ref,
          e.sample_id,
          e.flag && e.flag.status ? e.flag.status : "",
          e.edited ? "yes" : "no",
          e.text,
        ]
          .map(csvCell)
          .join(","),
      );
    }
    return out.join("\r\n");
  }
  // Open a clean, self-contained print window and trigger the browser print dialog
  // (the user picks "Save as PDF"). No reportlab/server round-trip needed.
  function printNotebook(rows) {
    const title = notebookTitle();
    const n = rows.length;
    const articles = rows
      .map(
        (e) =>
          `<article><h3>${escapeHtml(fmtTime(e.timestamp))}</h3>` +
          `<p class="meta">${escapeHtml(exportEntryMeta(e).join(" • "))}</p>` +
          `<p class="text">${escapeHtml((e.text || "").trim())}</p></article>`,
      )
      .join("");
    const sub = `Exported ${fmtTime(new Date().toISOString())} • ${n} ${n === 1 ? "entry" : "entries"}`;
    const doc =
      `<!doctype html><html><head><meta charset="utf-8"><title>${escapeHtml(title)}</title>` +
      `<style>body{font:14px/1.55 system-ui,-apple-system,sans-serif;margin:2.5rem;color:#111}` +
      `h1{margin:0 0 .25rem;font-size:1.6rem}.sub{color:#666;margin:0 0 1.5rem}` +
      `article{border-top:1px solid #ddd;padding:.85rem 0;break-inside:avoid}` +
      `h3{margin:0 0 .2rem;font-size:1rem}.meta{color:#666;font-size:.78rem;margin:0 0 .4rem}` +
      `.text{margin:0;white-space:pre-wrap}@media print{body{margin:1rem}}</style></head>` +
      `<body><h1>${escapeHtml(title)}</h1><p class="sub">${escapeHtml(sub)}</p>${articles}` +
      `<script>window.onload=function(){window.focus();window.print();}<\/script></body></html>`;
    const w = window.open("", "_blank");
    if (!w) {
      alert("Pop-up blocked — allow pop-ups to export the notebook as PDF.");
      return;
    }
    w.document.open();
    w.document.write(doc);
    w.document.close();
  }
  function downloadBlob(filename, mime, text) {
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
  function exportNotebook(format) {
    const rows = sortLog(logCache, logSortMode);
    const base = notebookExportBase();
    if (format === "csv") {
      downloadBlob(base + ".csv", "text/csv;charset=utf-8", exportNotebookCSV(rows));
    } else if (format === "pdf") {
      printNotebook(rows);
    } else {
      downloadBlob(base + ".md", "text/markdown;charset=utf-8", exportNotebookMarkdown(rows));
    }
  }
  function wireNotebookExport() {
    const btn = $("log-export");
    const menu = $("log-export-menu");
    if (!btn || !menu) return; // page without the export control opts out
    const close = () => {
      menu.classList.add("hidden");
      btn.setAttribute("aria-expanded", "false");
    };
    btn.addEventListener("click", (e) => {
      e.stopPropagation(); // don't let the outside-click handler immediately re-close
      const opening = menu.classList.contains("hidden");
      menu.classList.toggle("hidden", !opening);
      btn.setAttribute("aria-expanded", String(opening));
    });
    menu.querySelectorAll("[data-export]").forEach((item) =>
      item.addEventListener("click", () => {
        exportNotebook(item.getAttribute("data-export"));
        close();
      }),
    );
    document.addEventListener("click", (e) => {
      if (!menu.contains(e.target) && e.target !== btn) close();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") close();
    });
  }

  // --- edit an existing log entry (per-row pencil -> pre-filled modal) -------
  // Any entry is editable; the backend re-tags it manual + edited. Reuses the
  // same modal pattern as "Manual Entry".
  function openLogEditModal(id) {
    const m = $("log-edit-modal");
    if (!m) return;
    const entry = logCache.find((e) => e.id === id);
    if (!entry) return;
    const idEl = $("log-edit-id");
    const textEl = $("log-edit-text");
    const res = $("log-edit-result");
    if (idEl) idEl.value = String(id);
    if (textEl) textEl.value = entry.text || "";
    if (res) res.textContent = "";
    m.classList.remove("hidden");
    if (textEl) textEl.focus();
  }
  function closeLogEditModal() {
    const m = $("log-edit-modal");
    if (m) m.classList.add("hidden");
  }
  async function submitLogEdit(e) {
    if (e) e.preventDefault();
    const idEl = $("log-edit-id");
    const textEl = $("log-edit-text");
    const result = $("log-edit-result");
    const id = parseInt((idEl && idEl.value) || "", 10);
    const text = ((textEl && textEl.value) || "").trim();
    if (!Number.isFinite(id)) return;
    if (!text) {
      if (result) result.textContent = "Entry text can't be empty.";
      return;
    }
    if (result) result.textContent = "Saving...";
    try {
      await patchLog(id, text);
      await refreshNotebookFeed();
      if (result) result.textContent = "";
      closeLogEditModal();
    } catch (err) {
      if (result) result.textContent = "Could not save: " + err.message;
    }
  }
  function wireLogEditModal() {
    const rows = $("log-rows");
    if (rows && !rows.dataset.editWired) {
      rows.dataset.editWired = "1";
      // Event-delegate the per-row pencil so it survives every re-render.
      rows.addEventListener("click", (ev) => {
        const btn = ev.target.closest("[data-edit-id]");
        if (btn) openLogEditModal(parseInt(btn.dataset.editId, 10));
      });
    }
    const cancel = $("log-edit-cancel");
    if (cancel) cancel.addEventListener("click", closeLogEditModal);
    const form = $("log-edit-form");
    if (form) form.addEventListener("submit", submitLogEdit);
    const modal = $("log-edit-modal");
    if (modal)
      modal.addEventListener("click", (ev) => {
        if (ev.target === modal) closeLogEditModal();
      });
  }

  // Activity-Stream sort dropdown (Notebook page). Closure state (logCache /
  // logSortMode / renderLog) lives here, so wire in app.js rather than inline.
  // dataset.wired keeps the re-run on demo-reset from double-binding.
  function wireLogSort() {
    const sel = $("log-sort");
    if (!sel || sel.dataset.wired) return;
    sel.dataset.wired = "1";
    sel.value = logSortMode; // reflect current mode on (re)hydrate
    sel.addEventListener("change", () => {
      logSortMode = sel.value;
      renderLog(logCache);
    });
  }

  async function patchLog(id, text) {
    const r = await fetch(API + "/api/log/" + id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }

  // --- renderers (each is a no-op when its hook is absent) -------------------
  const STATUS_BADGE = {
    READY: "text-secondary bg-secondary-container/20",
    LOW_REAGENTS: "text-tertiary bg-tertiary-container/20",
    ARCHIVED: "text-on-surface-variant bg-surface-variant",
  };

  function renderProtocolCards(protocols) {
    const host = $("protocol-cards");
    if (!host) return;
    if (!protocols.length) {
      host.innerHTML = `<p class="text-on-surface-variant">No protocols loaded.</p>`;
      return;
    }
    host.innerHTML = protocols
      .map((p) => {
        const badge = STATUS_BADGE[p.status] || STATUS_BADGE.READY;
        const pill = (label) =>
          `<span class="text-[12px] bg-surface-variant px-2 py-1 rounded border border-outline-variant">${escapeHtml(
            label
          )}</span>`;
        // Prefer name + amount ("lysis buffer · 200 uL"); fall back to plain
        // reagent-name pills when a protocol has no extractable volumes.
        const reagents =
          p.ingredients && p.ingredients.length
            ? p.ingredients.map((ing) => pill(`${ing.reagent} · ${ing.display}`)).join("")
            : (p.reagents || []).map((r) => pill(r)).join("");
        const archived = p.status === "ARCHIVED";
        const loadBtn = archived
          ? `<button class="flex-1 border border-outline text-on-surface py-3 rounded-lg font-bold flex items-center justify-center gap-2" disabled><span class="material-symbols-outlined">history</span>Archived</button>`
          : `<button class="protocol-load flex-1 bg-primary text-on-primary py-3 rounded-lg font-bold hover:bg-primary/90 transition-all active:opacity-80 flex items-center justify-center gap-2" data-protocol-id="${escapeHtml(
              p.id
            )}"><span class="material-symbols-outlined">play_arrow</span>Load Protocol</button>`;
        // Edit sits to the RIGHT of Load and opens the structured editor.
        const editBtn = `<button class="protocol-edit px-4 rounded-lg border border-outline text-on-surface-variant hover:bg-surface-variant hover:text-on-surface transition-colors active:scale-95 flex items-center justify-center" data-protocol-id="${escapeHtml(
          p.id
        )}" title="Edit protocol" aria-label="Edit protocol"><span class="material-symbols-outlined">edit</span></button>`;
        const button = `<div class="flex gap-2">${loadBtn}${editBtn}</div>`;
        return `<div class="protocol-card bg-surface-container-low rounded-xl p-6 flex flex-col">
  <div class="flex justify-between items-start mb-4">
    <div class="w-12 h-12 rounded-lg bg-surface-variant flex items-center justify-center">
      <span class="material-symbols-outlined text-primary" style="font-variation-settings:'FILL' 1;">science</span>
    </div>
    <div class="flex items-center gap-2">
      <span class="font-data-label text-xs px-2 py-1 rounded ${badge}">${escapeHtml(
          p.status.replace("_", " ")
        )}</span>
      <button class="protocol-delete w-8 h-8 rounded-lg flex items-center justify-center text-on-surface-variant hover:bg-error/10 hover:text-error transition-colors active:scale-95" data-protocol-id="${escapeHtml(
          p.id
        )}" data-protocol-name="${escapeHtml(p.name)}" title="Remove protocol" aria-label="Remove protocol"><span class="material-symbols-outlined text-[20px]">delete</span></button>
    </div>
  </div>
  <h3 class="font-headline-md text-headline-md mb-2">${escapeHtml(p.name)}</h3>
  <p class="text-on-surface-variant text-sm mb-6 flex-1">${escapeHtml(p.description || "")}</p>
  <div class="grid grid-cols-2 gap-4 mb-6">
    <div class="bg-surface-container-high p-3 rounded-lg">
      <p class="text-[10px] text-on-surface-variant uppercase font-bold mb-1">Duration</p>
      <div class="flex items-center gap-2"><span class="material-symbols-outlined text-sm">schedule</span><span class="font-data-value text-data-value">${escapeHtml(
        p.duration_label || "-"
      )}</span></div>
    </div>
    <div class="bg-surface-container-high p-3 rounded-lg">
      <p class="text-[10px] text-on-surface-variant uppercase font-bold mb-1">Steps</p>
      <div class="flex items-center gap-2"><span class="material-symbols-outlined text-sm">format_list_numbered</span><span class="font-data-value text-data-value">${p.step_count}</span></div>
    </div>
  </div>
  <div class="mb-6"><p class="text-[10px] text-on-surface-variant uppercase font-bold mb-2">Required Reagents</p><div class="flex flex-wrap gap-2">${reagents}</div></div>
  ${button}
</div>`;
      })
      .join("");
    host.querySelectorAll(".protocol-load").forEach((btn) => {
      btn.addEventListener("click", () => loadProtocol(btn.dataset.protocolId));
    });
    host.querySelectorAll(".protocol-delete").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const name = btn.dataset.protocolName || "this protocol";
        if (!confirm(`Remove "${name}"? This deletes its file and cannot be undone.`)) return;
        try {
          await deleteProtocol(btn.dataset.protocolId);
        } catch (e) {
          alert("Could not remove protocol: " + e.message);
        }
      });
    });
    host.querySelectorAll(".protocol-edit").forEach((btn) => {
      btn.addEventListener("click", () => openEditProtocolModal(btn.dataset.protocolId));
    });
  }

  // Dashboard "Recently Used Protocols": the 3 most recent, slimmed to just
  // time/name/description. The whole card loads the protocol (opens the Guide);
  // full detail lives on the protocols page. Cold-start entries have no
  // last_used_at, so the time slot reads "Not used yet".
  function renderRecentProtocols(recent) {
    const host = $("recent-protocols");
    if (!host) return;
    if (!recent.length) {
      host.innerHTML = `<p class="text-on-surface-variant text-sm col-span-3">No protocols available.</p>`;
      return;
    }
    host.innerHTML = recent
      .map((p) => {
        const when = p.last_used_at ? fmtTime(p.last_used_at) : "Not used yet";
        // Highlight the currently-loaded protocol the same way the active-notebook
        // card is marked: solid primary border + an "Active" badge.
        const borderCls = p.active
          ? "recent-card--active border-primary"
          : "border-outline-variant hover:border-primary";
        const badge = p.active
          ? `<span class="font-data-label text-xs px-2 py-1 rounded bg-primary/10 text-primary">Active</span>`
          : "";
        return `<button type="button" class="recent-card bg-surface-container-low rounded-xl p-5 flex flex-col text-left w-full border ${borderCls} transition-colors active:scale-[0.99] cursor-pointer" data-protocol-id="${escapeHtml(
          p.id
        )}" title="Load ${escapeHtml(p.name)}">
  <div class="flex items-center justify-between gap-2 mb-3">
    <span class="flex items-center gap-2 text-on-surface-variant text-xs"><span class="material-symbols-outlined text-sm">schedule</span><span>${escapeHtml(when)}</span></span>
    ${badge}
  </div>
  <h3 class="font-headline-md text-headline-md mb-2">${escapeHtml(p.name)}</h3>
  <p class="text-on-surface-variant text-sm flex-1">${escapeHtml(p.description || "")}</p>
</button>`;
      })
      .join("");
    host.querySelectorAll(".recent-card").forEach((btn) => {
      btn.addEventListener("click", () => loadProtocol(btn.dataset.protocolId));
    });
  }

  function renderInventory(items) {
    const host = $("inventory-rows");
    if (!host) return;
    inventoryCache = items;
    const header = `<div class="grid grid-cols-12 gap-4 px-6 py-3 bg-surface-container-low text-on-surface-variant text-xs font-bold uppercase tracking-wider border-b border-outline-variant sticky top-0 z-10">
      <div class="col-span-4">Reagent Name</div><div class="col-span-2">Amount</div><div class="col-span-3">Location</div><div class="col-span-2">Date Created</div><div class="col-span-1 text-right">Actions</div></div>`;
    const rows = items
      .map((it, i) => {
        const amtRaw = (it.amount == null ? "" : String(it.amount)).trim();
        const unit = (it.unit == null ? "" : String(it.unit)).trim();
        const amtNum = parseFloat(amtRaw);
        const isZero = amtRaw !== "" && !isNaN(amtNum) && amtNum === 0;
        const depletedCls = isZero ? "inv-depleted" : "";
        const amtCls = isZero ? "text-error font-bold" : "text-on-surface";
        // Prefer the backend's humanized amount ("1000 mL" -> "1 L"); fall back to
        // raw amount + unit, then to quantity_approx for unstructured rows.
        const display = (it.amount_display == null ? "" : String(it.amount_display)).trim();
        const amtText = escapeHtml(
          amtRaw === "" ? formatInventoryAmount(it) : display || (unit ? `${amtRaw} ${unit}` : amtRaw)
        );
        return `<div class="inventory-row grid grid-cols-12 gap-4 px-6 py-5 border-b border-outline-variant items-center transition-colors group ${depletedCls}">
      <div class="col-span-4 flex items-center gap-3">
        <div class="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center text-primary"><span class="material-symbols-outlined text-lg">science</span></div>
        <div><p class="font-bold text-on-surface">${escapeHtml(it.name)}</p></div>
      </div>
      <div class="col-span-2 whitespace-nowrap"><span class="text-sm font-data-label ${amtCls}">${amtText}</span></div>
      <div class="col-span-3"><p class="text-on-surface text-sm">${escapeHtml(it.location)}</p></div>
      <div class="col-span-2"><p class="font-data-label text-on-surface-variant text-sm">${escapeHtml(
        it.date || "—"
      )}</p></div>
      <div class="col-span-1 flex items-center justify-end gap-1 opacity-60 group-hover:opacity-100 transition-opacity">
        <button type="button" class="inv-edit text-on-surface-variant hover:text-primary" data-id="${escapeHtml(String(it.id))}" title="Edit"><span class="material-symbols-outlined text-base">edit</span></button>
        <button type="button" class="inv-delete text-on-surface-variant hover:text-error" data-id="${escapeHtml(String(it.id))}" title="Delete"><span class="material-symbols-outlined text-base">delete</span></button>
      </div>
    </div>`;
      })
      .join("");
    host.innerHTML = header + (rows || `<div class="p-12 text-center opacity-40">Inventory is empty.</div>`);
  }

  function prepVerdictClass(verdict) {
    if (verdict === "in_stock") return "text-secondary";
    if (verdict === "insufficient" || verdict === "critical" || verdict === "missing") return "text-error";
    return "text-tertiary";
  }

  function prepVerdictLabel(row) {
    if (row.verdict === "in_stock") return "In stock";
    if (row.verdict === "unknown_unit") return "Check units";
    if (row.verdict === "insufficient") {
      return `Short ${row.shortage_ul} uL`;
    }
    if (row.verdict === "missing") return "Missing";
    return row.verdict;
  }

  // ---- Priority ordering state for the prep modal -------------------------
  // Maps reagent name -> ordered array of {id, name} for the bottles shown.
  // Rebuilt whenever renderPrepTable is called; mutated by drag-reorder.
  let _prepPriorityState = {};

  function _getPriorityOrder() {
    const order = {};
    for (const [reagent, bottles] of Object.entries(_prepPriorityState)) {
      const ids = bottles.map((b) => b.id).filter((id) => id != null);
      if (ids.length > 1) order[reagent] = ids; // only send when user has a real choice
    }
    return order;
  }

  // Wire drag-and-drop reordering on a priority list element.
  function _wirePriorityDrag(listEl, reagentName) {
    let dragging = null;
    listEl.querySelectorAll("[draggable]").forEach((row) => {
      row.addEventListener("dragstart", (e) => {
        dragging = row;
        e.dataTransfer.effectAllowed = "move";
        row.classList.add("opacity-40");
      });
      row.addEventListener("dragend", () => {
        if (dragging) dragging.classList.remove("opacity-40");
        dragging = null;
        listEl.querySelectorAll("[draggable]").forEach((r) => r.classList.remove("bg-surface-container"));
        // Sync _prepPriorityState from DOM order
        const ids = [];
        const names = [];
        listEl.querySelectorAll("[data-bottle-id]").forEach((r) => {
          ids.push(Number(r.dataset.bottleId));
          names.push(r.dataset.bottleName);
        });
        _prepPriorityState[reagentName] = ids.map((id, i) => ({ id, name: names[i] }));
      });
      row.addEventListener("dragover", (e) => {
        e.preventDefault();
        if (!dragging || dragging === row) return;
        row.classList.add("bg-surface-container");
        const rect = row.getBoundingClientRect();
        const mid = rect.top + rect.height / 2;
        if (e.clientY < mid) listEl.insertBefore(dragging, row);
        else listEl.insertBefore(dragging, row.nextSibling);
      });
      row.addEventListener("dragleave", () => {
        row.classList.remove("bg-surface-container");
      });
      row.addEventListener("drop", (e) => {
        e.preventDefault();
        row.classList.remove("bg-surface-container");
      });
    });
  }

  // Last sample count used in the prep modal (for auto-deduct on protocol end).
  let _lastPrepSamples = 1;
  // Guard: auto-deduct fires once per protocol run, not on every WS render.
  let _deductTriggeredForRun = false;

  function renderPrepTable(data) {
    const mount = $("prep-table");
    if (!mount) return;
    const rows = data.reagents || [];
    if (!rows.length) {
      mount.innerHTML = `<div class="text-on-surface-variant">No scalable reagent volumes found in this protocol.</div>`;
      _prepPriorityState = {};
      return;
    }

    // Build priority state directly from source_details (ids are already resolved).
    const existing = _prepPriorityState;
    _prepPriorityState = {};
    for (const row of rows) {
      const details = row.source_details || [];
      if (!details.length) continue;
      // Preserve any drag-reorder the user made in a previous render.
      const prev = existing[row.reagent];
      if (prev && prev.length === details.length) {
        const detailById = Object.fromEntries(details.map((d) => [d.id, d]));
        const allMatch = prev.every((b) => detailById[b.id]);
        if (allMatch) {
          _prepPriorityState[row.reagent] = prev;
          continue;
        }
      }
      _prepPriorityState[row.reagent] = details.map((d) => ({ id: d.id, name: d.name }));
    }

    const tableHtml = `
      <div class="overflow-x-auto">
        <table class="prep-table w-full text-left border-collapse">
          <thead>
            <tr class="text-xs uppercase text-on-surface-variant border-b border-outline-variant">
              <th class="py-2 pr-3">Reagent</th>
              <th class="py-2 pr-3">Per sample</th>
              <th class="py-2 pr-3">Samples</th>
              <th class="py-2 pr-3">Total needed</th>
              <th class="py-2 pr-3">Availability</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map((row) => {
              const details = row.source_details || [];
              const multiBottle = details.length > 1;
              // Use the user's current priority order if available.
              const orderedDetails = (() => {
                const state = _prepPriorityState[row.reagent];
                if (!state || !multiBottle) return details;
                const byId = Object.fromEntries(details.map((d) => [d.id, d]));
                return state.map((b) => byId[b.id]).filter(Boolean);
              })();
              const priorityRows = multiBottle ? orderedDetails.map((d, i) => `
                <div draggable="true" data-bottle-id="${escapeHtml(String(d.id))}" data-bottle-name="${escapeHtml(d.name)}"
                     class="flex items-center gap-2 px-2 py-1.5 rounded cursor-grab text-xs border border-outline-variant/40 mb-1 transition-colors bg-surface-container-lowest">
                  <span class="material-symbols-outlined text-sm text-on-surface-variant select-none shrink-0">drag_indicator</span>
                  <span class="font-mono text-[10px] text-primary font-bold w-4 shrink-0">${i + 1}</span>
                  <div class="flex flex-col min-w-0 flex-1">
                    <span class="font-medium text-on-surface truncate">${escapeHtml(d.name)}</span>
                    <span class="text-on-surface-variant text-[10px]">${escapeHtml(d.amount_display)}${d.location ? ` · ${escapeHtml(d.location)}` : ""}</span>
                  </div>
                </div>`).join("") : "";
              const singleDisplay = details.length === 1 && details[0]
                ? `<div class="text-xs text-on-surface-variant mt-0.5">${escapeHtml(details[0].amount_display)}${details[0].location ? ` · ${escapeHtml(details[0].location)}` : ""}</div>`
                : "";
              return `
              <tr class="border-b border-outline-variant/60">
                <td class="py-3 pr-3 text-on-surface font-medium">${escapeHtml(row.reagent)}</td>
                <td class="py-3 pr-3 font-data-label">${escapeHtml(row.per_sample_ul)} uL</td>
                <td class="py-3 pr-3 font-data-label">${escapeHtml(row.n_samples)}</td>
                <td class="py-3 pr-3 font-data-label text-on-surface">${escapeHtml(row.total_display)}</td>
                <td class="py-3 pr-3">
                  <div class="${prepVerdictClass(row.verdict)} font-bold">${escapeHtml(prepVerdictLabel(row))}</div>
                  <div class="text-xs text-on-surface-variant">${escapeHtml(row.match_name || "No inventory match")}</div>
                  ${singleDisplay}
                  ${multiBottle ? `
                  <div class="mt-2">
                    <div class="text-[10px] uppercase text-on-surface-variant mb-1 tracking-wide">Use order — drag to reorder</div>
                    <div class="priority-list" data-reagent="${escapeHtml(row.reagent)}">${priorityRows}</div>
                  </div>` : ""}
                </td>
              </tr>`;
            }).join("")}
          </tbody>
        </table>
      </div>
      <div class="mt-4 pt-4 border-t border-outline-variant flex items-center justify-between gap-3">
        <p class="text-xs text-on-surface-variant">Deduct used amounts from inventory after the run.</p>
        <button id="prep-deduct" type="button"
          class="flex items-center gap-1.5 bg-secondary text-on-secondary px-4 py-2 rounded-lg font-bold text-sm hover:bg-secondary/90 transition-all">
          <span class="material-symbols-outlined text-base">remove_circle</span>
          Deduct from Inventory
        </button>
      </div>
    `;

    mount.innerHTML = tableHtml;

    // Wire drag-and-drop on each multi-bottle priority list.
    mount.querySelectorAll(".priority-list").forEach((listEl) => {
      const reagentName = listEl.dataset.reagent;
      _wirePriorityDrag(listEl, reagentName);
    });

    // Wire the manual deduct button.
    const deductBtn = $("prep-deduct");
    if (deductBtn) {
      deductBtn.addEventListener("click", () => handleDeductReagents());
    }
  }

  async function handleDeductReagents() {
    const deductBtn = $("prep-deduct");
    if (deductBtn) { deductBtn.disabled = true; deductBtn.innerHTML = '<span class="material-symbols-outlined text-base">hourglass_empty</span> Computing…'; }
    try {
      const samples = _lastPrepSamples || Number($("prep-samples")?.value || 1);
      const plan = await fetchScaleWithPriority({
        sample_count: samples,
        overage_percent: 0,
        priority_order: _getPriorityOrder(),
      });

      const deductions = plan.deductions || [];
      if (!deductions.length) {
        alert("No inventory items to deduct (all reagents missing or have non-volume units).");
        return;
      }

      const lines = deductions.map(
        (d) => `• ${d.name}: −${d.deduct_ul} uL  →  ${d.new_amount > 0 ? d.new_amount + " " + d.new_unit + " remaining" : "EMPTY — will be removed"}`
      );
      const ok = confirm(
        `Deduct reagents from inventory?\n\n${lines.join("\n")}\n\nThis cannot be undone.`
      );
      if (!ok) return;

      await consumeReagents(deductions);
      await handlePrepCompute();
    } catch (err) {
      alert(`Deduction failed: ${err.message || err}`);
    } finally {
      if (deductBtn) {
        deductBtn.disabled = false;
        deductBtn.innerHTML = '<span class="material-symbols-outlined text-base">remove_circle</span> Deduct from Inventory';
      }
    }
  }

  // Called automatically when a protocol finishes. Skips silently if there is
  // nothing to deduct. The _deductTriggeredForRun guard prevents re-firing on
  // subsequent WS renders of the same finished state.
  async function autoDeductOnComplete() {
    if (_deductTriggeredForRun) return;
    _deductTriggeredForRun = true;
    try {
      const samples = _lastPrepSamples || 1;
      const plan = await fetchScaleWithPriority({
        sample_count: samples,
        overage_percent: 0,
        priority_order: _getPriorityOrder(),
      });
      const deductions = plan.deductions || [];
      if (!deductions.length) return;

      const lines = deductions.map(
        (d) => `• ${d.name}: −${d.deduct_ul} uL  →  ${d.new_amount > 0 ? d.new_amount + " " + d.new_unit + " remaining" : "EMPTY — will be removed"}`
      );
      const ok = confirm(
        `Protocol complete! Deduct reagents from inventory?\n\n${lines.join("\n")}\n\nThis cannot be undone. Click Cancel to skip.`
      );
      if (!ok) return;
      await consumeReagents(deductions);
    } catch (err) {
      // Non-blocking: auto-deduct failure doesn't interrupt the protocol flow.
      console.warn("Auto-deduct failed:", err);
    }
  }

  async function handlePrepCompute() {
    const table = $("prep-table");
    if (!table) return;
    const samples = Number($("prep-samples")?.value || 1);
    _lastPrepSamples = samples;
    table.textContent = "Calculating reagent prep...";
    try {
      const data = await fetchScaleWithPriority({
        sample_count: samples,
        overage_percent: 0,
        priority_order: _getPriorityOrder(),
      });
      renderPrepTable(data);
    } catch (err) {
      table.innerHTML = `<div class="text-error">${escapeHtml(err.message || String(err))}</div>`;
    }
  }

  // --- reagent prep modal ---------------------------------------------------
  // The prep table is no longer an always-on dashboard panel; it surfaces as a
  // modal that pops up when a protocol is loaded (and is re-openable from the
  // Guide breadcrumb). A full page reload happens on load, so a sessionStorage
  // flag carries the "open me" intent across the navigation.
  const PREP_ON_LOAD_KEY = "prep-modal-on-load";

  function flagPrepOnLoad() {
    try {
      sessionStorage.setItem(PREP_ON_LOAD_KEY, "1");
    } catch (e) {
      /* sessionStorage unavailable (private mode); modal just won't auto-open */
    }
  }
  function consumePrepOnLoad() {
    try {
      if (sessionStorage.getItem(PREP_ON_LOAD_KEY) === "1") {
        sessionStorage.removeItem(PREP_ON_LOAD_KEY);
        return true;
      }
    } catch (e) {
      /* ignore */
    }
    return false;
  }

  function prepModalOpen() {
    const modal = $("prep-modal");
    return !!modal && !modal.classList.contains("hidden");
  }
  // Mirror the prep gate (run-not-started) + the operator's determined sample
  // count to the backend so "start protocol"/"done" can require a real count
  // before the run begins. Returns the promise so callers can sequence on it.
  function postPrepState(patch) {
    return fetch(API + "/api/prep/state", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }).catch(() => {});
  }
  // ``gate`` raises the start-gate (used when the popup opens on a fresh load);
  // re-opening to peek (breadcrumb / a voice sample change) does not re-gate.
  function openPrepModal(name, gate) {
    const modal = $("prep-modal");
    if (!modal) return;
    const title = $("prep-protocol-name");
    const resolved = name || $("protocol-name")?.textContent || "Protocol";
    if (title) title.textContent = resolved;
    modal.classList.remove("hidden");
    if (gate) postPrepState({ open: true });  // run not started until confirmed
    handlePrepCompute();                       // preview only; not a "determination"
  }
  function closePrepModal() {
    // Hide the popup only. The backend gate is lifted by the run actually
    // starting (handled server-side), so dismissing never bypasses the count.
    const modal = $("prep-modal");
    if (modal) modal.classList.add("hidden");
  }
  // The operator deliberately set a sample count (typed it / clicked Compute):
  // record it so the run is allowed to start, then re-scale the table.
  function determineSamples() {
    const n = Number($("prep-samples")?.value || 0);
    const posted = n >= 1 ? postPrepState({ sample_count: n }) : Promise.resolve();
    handlePrepCompute();
    return posted;
  }
  function wirePrepModal() {
    const compute = $("prep-compute");
    if (compute) compute.addEventListener("click", determineSamples);
    const input = $("prep-samples");
    if (input) input.addEventListener("change", determineSamples);
    const openBtn = $("prep-open");
    if (openBtn) openBtn.addEventListener("click", () => openPrepModal());
    // "Done" begins the run through the SAME spine as voice: record the shown
    // count, then confirm the prep (which starts the run) instead of just hiding.
    const done = $("prep-done");
    if (done)
      done.addEventListener("click", async () => {
        await determineSamples();
        ingestCommand("looks good").catch(() => {});
      });
    // X / clicking the backdrop just hides the popup; the run stays gated.
    const close = $("prep-close");
    if (close) close.addEventListener("click", closePrepModal);
    const modal = $("prep-modal");
    if (modal)
      modal.addEventListener("click", (e) => {
        if (e.target === modal) closePrepModal();
      });
  }

  // Plan 3: a log entry may carry an optional reproducibility `flag` (volume_ul).
  // VISIBLE badges use display symbols; the line text stays ASCII for screen readers.
  function renderLogFlag(flag) {
    if (!flag) return "";
    if (flag.status === "ok") {
      return `<div class="log-ok"><span aria-hidden="true">\u2713</span> OK: ${escapeHtml(
        flag.parameter
      )} matched ${escapeHtml(flag.expected)} ${escapeHtml(flag.unit)}</div>`;
    }
    if (flag.status === "mismatch") {
      return `<div class="log-flag"><span aria-hidden="true">\u26a0</span> Warning: expected ${escapeHtml(
        flag.expected
      )} ${escapeHtml(flag.unit)}, logged ${escapeHtml(flag.logged)} ${escapeHtml(
        flag.unit
      )}</div>`;
    }
    return "";
  }

  // Order the Activity Stream for #log-rows. Returns a NEW array; never mutates
  // logCache (stored oldest-first and reused by the WS delta paths). Modes mirror
  // the #log-sort <select>. Tiebreaker: ISO-8601 timestamp localeCompare, then the
  // monotonic id, so order stays deterministic when timestamps collide. "automatic"
  // is the only automatic value; everything else (incl. edited) groups as manual.
  function sortLog(log, mode) {
    const isAuto = (e) => (e && e.entry_type === "automatic" ? 1 : 0);
    const byNewest = (a, b) => {
      const t = String(b.timestamp || "").localeCompare(String(a.timestamp || ""));
      return t !== 0 ? t : (b.id || 0) - (a.id || 0);
    };
    const arr = [...log];
    switch (mode) {
      case "date-asc":
        return arr.sort((a, b) => -byNewest(a, b));
      case "manual-first":
        return arr.sort((a, b) => isAuto(a) - isAuto(b) || byNewest(a, b));
      case "auto-first":
        return arr.sort((a, b) => isAuto(b) - isAuto(a) || byNewest(a, b));
      case "date-desc":
      default:
        return arr.sort(byNewest);
    }
  }

  // The text-search filter (notebook.html inline script) toggles row.style on the
  // rendered .log-entry-row nodes; any renderLog() rebuild drops those styles. Re-
  // apply the active term after a render so sorting and live WS deltas don't
  // silently clear the filter.
  function reapplyLogSearch() {
    const box = $("log-search");
    const term = ((box && box.value) || "").toLowerCase();
    if (!term) return;
    document.querySelectorAll("#log-rows .log-entry-row").forEach((row) => {
      row.style.display = row.innerText.toLowerCase().includes(term) ? "grid" : "none";
    });
  }

  function renderLog(log) {
    const host = $("log-rows");
    if (!host) return;
    if (!log.length) {
      host.innerHTML = `<div class="p-12 flex flex-col items-center justify-center opacity-30 select-none"><span class="material-symbols-outlined text-6xl mb-4">history_edu</span><p class="font-headline-md">No log entries yet</p></div>`;
      return;
    }
    const displayLog = sortLog(log, logSortMode);
    host.innerHTML = displayLog
      .map((e) => {
        const flagged = e.flag && e.flag.status === "mismatch" ? " flagged" : "";
        // Provenance: automatic = system step-note, manual = human entry. An
        // edited entry is always manual and reads "manual · edited".
        const type = e.entry_type === "automatic" ? "automatic" : "manual";
        const typeClass = type === "automatic" ? "entry-automatic" : "entry-manual";
        const typeLabel = e.edited ? "manual · edited" : type;
        const typeLabelCls = type === "automatic" ? "text-on-surface-variant" : "text-secondary";
        return `<div class="log-entry-row ${typeClass}${flagged} group relative grid grid-cols-12 gap-4 px-6 py-5 border-b border-outline-variant items-center transition-colors" data-log-id="${e.id}">
      <div class="col-span-3 font-data-label text-on-surface text-sm">${escapeHtml(fmtTime(e.timestamp))}</div>
      <div class="col-span-3 flex flex-col gap-1 items-start"><span class="bg-primary-container/20 text-primary-fixed px-2 py-0.5 rounded text-xs font-bold">${escapeHtml(
        e.category || (e.sample_id ? "Sample " + e.sample_id : "Note")
      )}</span><span class="text-[10px] lowercase ${typeLabelCls}">${escapeHtml(typeLabel)}</span></div>
      <div class="col-span-6"><p class="text-on-surface text-sm log-text">${escapeHtml(
        e.text
      )}</p>${renderLogFlag(e.flag)}</div>
      <button type="button" data-edit-id="${e.id}" title="Edit entry" aria-label="Edit entry" class="absolute top-2 right-2 opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity text-on-surface-variant hover:text-primary p-1"><span class="material-symbols-outlined text-base">edit</span></button>
    </div>`;
      })
      .join("");
    reapplyLogSearch();
  }

  // --- notebooks (multi-notebook log) ---------------------------------------
  async function fetchNotebooks() {
    return await getJSON("/api/notebooks");
  }
  function renderNotebooks(data) {
    const nbs = (data && data.notebooks) || [];
    const title = $("notebook-title");
    if (title) {
      const active = nbs.find((n) => n.active);
      title.textContent = active ? active.name : "Notebook";
    }
    const host = $("notebook-list");
    if (!host) return;
    host.innerHTML = nbs
      .map((n) => {
        const count = `${n.entry_count} ${n.entry_count === 1 ? "entry" : "entries"}`;
        return `<button type="button" data-nb-id="${n.id}" class="nb-item w-full text-left px-4 py-3 rounded-xl border transition-colors flex items-center justify-between gap-3 ${
          n.active
            ? "border-primary bg-primary/10"
            : "border-outline-variant hover:bg-surface-variant"
        }">
        <span class="flex items-center gap-3 min-w-0">
          <span class="material-symbols-outlined ${
            n.active ? "text-primary" : "text-on-surface-variant"
          }">${n.active ? "menu_book" : "book"}</span>
          <span class="truncate font-medium text-on-surface">${escapeHtml(n.name)}</span>
        </span>
        <span class="text-xs font-data-label text-on-surface-variant shrink-0">${count}</span>
      </button>`;
      })
      .join("");
    host.querySelectorAll(".nb-item").forEach((b) =>
      b.addEventListener("click", () => selectNotebook(b.getAttribute("data-nb-id")))
    );
  }

  // Dashboard "Recent Notebooks": clickable cards mirroring the protocol cards.
  // Newest-first and capped so the dashboard stays tidy ("View All" covers the
  // rest). No-ops on pages without the mount, so it's safe to call from the
  // shared hydrate path and the notebook_list event.
  const DASHBOARD_NOTEBOOKS_LIMIT = 3;
  function renderDashboardNotebooks(data) {
    const host = $("dashboard-notebooks");
    if (!host) return;
    const nbs = [...((data && data.notebooks) || [])].sort((a, b) =>
      String(b.created_at || "").localeCompare(String(a.created_at || ""))
    );
    if (!nbs.length) {
      host.innerHTML = `<div class="text-on-surface-variant text-sm col-span-3">No notebooks yet.</div>`;
      return;
    }
    host.innerHTML = nbs
      .slice(0, DASHBOARD_NOTEBOOKS_LIMIT)
      .map((n) => {
        const count = `${n.entry_count} ${n.entry_count === 1 ? "entry" : "entries"}`;
        const created = n.created_at ? new Date(n.created_at) : null;
        const createdStr =
          created && !isNaN(created.getTime()) ? created.toLocaleDateString() : "";
        return `<button type="button" data-nb-id="${n.id}" class="nb-card text-left bg-surface-container-low rounded-xl p-6 flex flex-col gap-4 border ${
          n.active ? "border-primary" : "border-outline-variant"
        } hover:bg-surface-variant transition-all active:scale-[0.99]">
  <div class="flex justify-between items-start">
    <div class="w-12 h-12 rounded-lg bg-surface-variant flex items-center justify-center">
      <span class="material-symbols-outlined text-primary"${
        n.active ? " style=\"font-variation-settings:'FILL' 1;\"" : ""
      }>${n.active ? "menu_book" : "book"}</span>
    </div>
    ${
      n.active
        ? `<span class="font-data-label text-xs px-2 py-1 rounded bg-primary/10 text-primary">Active</span>`
        : ""
    }
  </div>
  <h3 class="font-headline-md text-headline-md text-on-surface truncate">${escapeHtml(n.name)}</h3>
  <div class="flex items-center gap-2 text-on-surface-variant text-sm">
    <span class="material-symbols-outlined text-sm">history_edu</span>
    <span class="font-data-value">${escapeHtml(count)}</span>
  </div>
  ${
    createdStr
      ? `<p class="text-[10px] text-on-surface-variant uppercase font-bold tracking-wider">Created ${escapeHtml(createdStr)}</p>`
      : ""
  }
</button>`;
      })
      .join("");
    host.querySelectorAll(".nb-card").forEach((b) =>
      b.addEventListener("click", () => openNotebook(b.getAttribute("data-nb-id")))
    );
  }
  // Open a notebook from the dashboard: make it active server-side, then show
  // its feed on the Notebook page.
  async function openNotebook(id) {
    await selectNotebook(id);
    window.location.href = "notebook.html";
  }

  async function refreshNotebookFeed() {
    if ($("notebook-list") || $("notebook-title") || $("notebook-select")) {
      const data = await fetchNotebooks();
      renderNotebooks(data);
      renderNotebookSelect(data);
    }
    if ($("log-rows") || $("log-preview")) {
      logCache = await fetchLog();
      renderLog(logCache);
      renderLogPreview(logCache);
    }
  }
  async function createNotebook(name) {
    await fetch(API + "/api/notebooks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    await refreshNotebookFeed();
  }
  async function selectNotebook(id) {
    await fetch(API + "/api/notebooks/" + id + "/select", { method: "POST" });
    await refreshNotebookFeed();
  }
  function refreshNotebookCounts() {
    // A new/removed log entry changes the active notebook's count; keep the
    // list in sync without disturbing the feed.
    if ($("notebook-list")) fetchNotebooks().then(renderNotebooks).catch(() => {});
  }
  // New-notebook modal. Mirrors the "Manual Entry" log modal (openLogModal /
  // submitLogForm) so both share the same look, backdrop-close, and inline
  // validation instead of a bare browser prompt dialog.
  function openNotebookModal() {
    const m = $("notebook-modal");
    if (!m) return;
    const res = $("notebook-result");
    if (res) res.textContent = "";
    const name = $("notebook-name");
    if (name) name.value = "";
    m.classList.remove("hidden");
    if (name) name.focus();
  }
  function closeNotebookModal() {
    const m = $("notebook-modal");
    if (m) m.classList.add("hidden");
  }
  async function submitNotebookForm(e) {
    if (e) e.preventDefault();
    const nameEl = $("notebook-name");
    const result = $("notebook-result");
    const name = ((nameEl && nameEl.value) || "").trim();
    if (!name) {
      if (result) result.textContent = "Enter a name for your notebook.";
      if (nameEl) nameEl.focus();
      return;
    }
    if (result) result.textContent = "Creating...";
    try {
      await createNotebook(name);
      if (nameEl) nameEl.value = "";
      if (result) result.textContent = "";
      closeNotebookModal();
    } catch (err) {
      if (result) result.textContent = "Could not create: " + err.message;
    }
  }
  function wireNotebookNew() {
    const btn = $("notebook-new");
    if (!btn || btn.dataset.wired) return;
    btn.dataset.wired = "1";
    btn.addEventListener("click", openNotebookModal);
    const cancel = $("notebook-cancel");
    if (cancel) cancel.addEventListener("click", closeNotebookModal);
    const form = $("notebook-form");
    if (form) form.addEventListener("submit", submitNotebookForm);
    const modal = $("notebook-modal");
    if (modal)
      modal.addEventListener("click", (e) => {
        if (e.target === modal) closeNotebookModal();
      });
  }

  // The guide page's notebook <select> picks the active notebook (where step
  // notes land). It shares the same /api/notebooks/{id}/select gate the sidebar
  // list uses, so both stay in sync over the notebook_list event.
  function renderNotebookSelect(data) {
    const sel = $("notebook-select");
    if (!sel) return;
    const nbs = (data && data.notebooks) || [];
    const activeId =
      data && data.active_id != null
        ? data.active_id
        : (nbs.find((n) => n.active) || {}).id;
    sel.innerHTML = nbs
      .map((n) => `<option value="${n.id}">${escapeHtml(n.name)}</option>`)
      .join("");
    if (activeId != null) sel.value = String(activeId);
  }
  function wireNotebookSelect() {
    const sel = $("notebook-select");
    if (!sel || sel.dataset.wired) return;
    sel.dataset.wired = "1";
    sel.addEventListener("change", () => {
      if (sel.value) selectNotebook(sel.value);
    });
  }

  // Compact preview of the last 3 entries in the active notebook, shown under
  // the notebook selector so the user sees what step notes are landing where.
  function renderLogPreview(log) {
    const host = $("log-preview");
    if (!host) return;
    const recent = (log || []).slice(-3).reverse();
    if (!recent.length) {
      host.innerHTML = `<p class="text-sm text-on-surface-variant italic">No entries in this notebook yet.</p>`;
      return;
    }
    host.innerHTML = recent
      .map((e) => {
        const cat = e.category || (e.sample_id ? "Sample " + e.sample_id : "Note");
        return `<div class="flex items-start gap-3 bg-surface-container-high rounded-lg px-3 py-2 border border-outline-variant">
        <span class="material-symbols-outlined text-primary text-base mt-0.5">history_edu</span>
        <div class="min-w-0 flex-1">
          <p class="text-sm text-on-surface truncate">${escapeHtml(e.text)}</p>
          <p class="text-[10px] font-data-label text-on-surface-variant">${escapeHtml(
            cat
          )} &middot; ${escapeHtml(fmtTime(e.timestamp))}</p>
        </div>
      </div>`;
      })
      .join("");
  }

  // --- step actions: Skip advances WITHOUT logging --------------------------
  // Confirm Action (#confirm-step) is wired by wireGuideConfirm() and sends
  // "next step" through the spine, which logs a "Completed step N" note. Skip
  // calls the endpoint with log=false so it advances without a note.
  async function advanceStep(log) {
    await fetch(API + "/api/step/next", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ log: !!log }),
    });
  }
  function wireStepActions() {
    const skip = $("skip-action");
    if (skip && !skip.dataset.wired) {
      skip.dataset.wired = "1";
      skip.addEventListener("click", () => {
        if (skip.disabled) return;
        advanceStep(false).catch(() => {});
      });
    }
    // Previous: click == saying "previous step" — routes through the spine to the
    // prev_step intent (handlers.py `_handle_prev_step`), same as voice.
    const prev = $("prev-step");
    if (prev && !prev.dataset.wired) {
      prev.dataset.wired = "1";
      prev.addEventListener("click", () => {
        if (prev.disabled) return;
        ingestCommand("previous step").catch(() => {});
      });
    }
  }

  function renderStep(step) {
    if (!step) {
      // No active run (e.g. after cancel/finish or a demo reset): keep the nav
      // slot in sync — without this it would keep its stale "Jump to guide" state.
      renderResumeRun(null);
      return;
    }
    // Finishing the final step clamps the cursor to the last real step and flips
    // ``finished``; the card then reads as "protocol complete" rather than just
    // sitting on the last step.
    const finished = !!step.finished;
    const cur = $("step-current");
    if (cur) {
      if (finished && step.protocol_name)
        cur.textContent = `${step.protocol_name} protocol finished.`;
      else if (step.current_step) cur.textContent = step.current_step.text;
    }
    const name = $("protocol-name");
    if (name && step.protocol_name) name.textContent = step.protocol_name;

    const idx = step.current_index == null ? -1 : step.current_index;
    const panel = $("step-panel");
    if (panel && idx >= 0) panel.classList.remove("hidden");
    // Reagent-prep is reachable from the breadcrumb once a protocol is active.
    const prepOpen = $("prep-open");
    if (prepOpen && idx >= 0) prepOpen.classList.remove("hidden");
    // Cancel sits beside Reagent Prep; both appear only while a run is active.
    const cancelBtn = $("cancel-protocol");
    if (cancelBtn && idx >= 0) cancelBtn.classList.remove("hidden");

    // Live step counters (Guide): mirror the tracker so "STEP x / N" and the
    // "Protocol Phase 0x / 0N" header track the loaded protocol instead of fake data.
    const total = Array.isArray(step.all_steps) ? step.all_steps.length : 0;
    const human = idx >= 0 ? idx + 1 : 0;
    const counter = $("step-counter");
    if (counter) {
      if (finished && total) counter.textContent = `STEP ${total} / ${total}`;
      else counter.textContent = total ? `STEP ${human} / ${total}` : "STEP —";
    }
    const phase = $("step-phase");
    if (phase) {
      if (finished) phase.textContent = "PROTOCOL COMPLETE";
      else
        phase.textContent = total
          ? `Protocol Phase ${String(human).padStart(2, "0")} / ${String(total).padStart(2, "0")}`
          : "Protocol Phase —";
    }
    const confirmBtn = $("confirm-step");
    if (confirmBtn) confirmBtn.disabled = idx < 0 || finished;
    const skipBtn = $("skip-action");
    if (skipBtn) skipBtn.disabled = idx < 0 || finished;
    // Previous only makes sense past the first step (works even when finished,
    // since the cursor is pinned to the last step).
    const prevBtn = $("prev-step");
    if (prevBtn) prevBtn.disabled = idx <= 0;

    const tracker = $("step-tracker");
    if (tracker && Array.isArray(step.all_steps)) {
      // Steps the user skipped (advanced past without confirming) render yellow
      // "Skipped" instead of green "Completed". A skipped index is always < idx;
      // check current first so returning to a step via "prev" shows In Progress.
      const skipped = new Set(
        Array.isArray(step.skipped_indices) ? step.skipped_indices : []
      );
      // Window the tracker to past-2 / current / next-2 (<=5 rows). The right
      // sidebar is fixed and can't scroll, so a long protocol's full step list
      // would push the timer off-screen — and scrolling needs hands, the opposite
      // of the voice-first goal. The window keeps the timer always visible.
      const center = idx < 0 ? 0 : idx; // finished already pins idx to the last step
      const start = Math.max(0, center - 2);
      const end = Math.min(total - 1, center + 2);
      let html = "";
      if (start > 0)
        html += ghostRow(`${start} earlier step${start > 1 ? "s" : ""}`, "expand_less");
      for (let i = start; i <= end; i++) {
        const s = step.all_steps[i];
        let icon = "circle";
        let cls = "border-outline-variant opacity-50";
        let label = "Pending";
        let labelCls = "text-on-surface-variant";
        // Once finished there is no "In Progress" row: every step reads as
        // Completed, except ones the user skipped, which stay yellow.
        if (!finished && i === idx) {
          icon = "pending";
          cls = "border-primary";
          label = "In Progress";
          labelCls = "text-primary";
        } else if (skipped.has(i)) {
          icon = "skip_next";
          cls = "border-tertiary";
          label = "Skipped";
          labelCls = "text-tertiary";
        } else if (finished || i < idx) {
          icon = "check_circle";
          cls = "border-secondary";
          label = "Completed";
          labelCls = "text-secondary";
        }
        html += `<div class="flex items-center gap-3 p-3 bg-surface-container-low rounded-lg border-l-4 ${cls}">
        <span class="material-symbols-outlined text-sm">${icon}</span>
        <div class="flex flex-col"><span class="text-xs font-bold text-on-surface">${escapeHtml(
          s.text
        )}</span><span class="text-[10px] uppercase ${labelCls}">${label}</span></div></div>`;
      }
      const later = total - 1 - end;
      if (later > 0)
        html += ghostRow(`${later} more step${later > 1 ? "s" : ""}`, "expand_more");
      tracker.innerHTML = html;
    }
    renderResumeRun(step);

    // Auto-deduct when the protocol finishes (fires once per run via guard).
    if (finished) autoDeductOnComplete();
  }

  // A muted, non-interactive hint row marking how many steps the window hides
  // above/below. Lets the user see the tracker is windowed without scrolling.
  function ghostRow(text, icon) {
    return `<div class="flex items-center justify-center gap-1 py-1 text-[10px] uppercase tracking-wider text-on-surface-variant opacity-70">
        <span class="material-symbols-outlined text-sm">${icon}</span>${escapeHtml(text)}</div>`;
  }

  // Top-left nav slot. Always filled: a muted, inert "No protocol active"
  // placeholder when nothing is running, swapped to a live "Jump to guide" CTA
  // (with protocol name) while a protocol is loaded — clicking it lands on the
  // Guide via the #run hash, which centers the current step. Kept live by
  // renderStep (Guide + WS step_change) and by the "active run" hydrate section
  // on non-Guide pages; resets to the placeholder on cancel/finish.
  function renderResumeRun(step) {
    const el = $("resume-run");
    if (!el) return;
    const active =
      !!step && step.current_index != null && step.current_index >= 0;
    const icon = el.querySelector(".material-symbols-outlined");
    const label = $("resume-run-label");
    const nm = $("resume-run-name");
    // Live CTA palette (matches the nav primary buttons) vs a muted, inert
    // placeholder (mirrors the Reset Demo button) so the slot is always filled.
    const ACTIVE = ["nav-link", "bg-primary", "text-on-primary", "font-bold",
                    "hover:opacity-90", "transition-opacity", "active:scale-95"];
    const EMPTY = ["bg-surface-container-high", "text-on-surface-variant",
                   "font-medium", "cursor-default"];
    el.classList.remove(...(active ? EMPTY : ACTIVE));
    el.classList.add(...(active ? ACTIVE : EMPTY));
    if (active) {
      el.setAttribute("href", "guide.html#run");
      el.removeAttribute("aria-disabled");
      el.title = "Jump to the protocol guide";
      if (icon) icon.textContent = "play_arrow";
      if (label) label.textContent = "Jump to guide";
      if (nm) nm.textContent = step.protocol_name || "";
    } else {
      el.removeAttribute("href");
      el.setAttribute("aria-disabled", "true");
      el.title = "No protocol is currently active";
      if (icon) icon.textContent = "science";
      if (label) label.textContent = "No protocol active";
      if (nm) nm.textContent = ""; // clear stale name (e.g. after cancel/finish)
    }
  }

  // --- timers (driven by the outer timer_update event) ----------------------
  const timers = new Map();

  function renderTimers() {
    const host = $("timer-list");
    if (!host) return;
    if (!timers.size) {
      host.innerHTML = `<p class="text-sm text-on-surface-variant">No active timers.</p>`;
      return;
    }
    host.innerHTML = Array.from(timers.values())
      .map(
        (t) => `<div class="timer-card relative bg-surface-container-low border border-outline-variant rounded-xl p-4 flex flex-col items-center" data-timer-id="${escapeHtml(
          t.timer_id
        )}">
      <button type="button" class="timer-dismiss absolute top-2 right-2 w-7 h-7 rounded-lg flex items-center justify-center text-on-surface-variant hover:bg-error/10 hover:text-error transition-colors active:scale-95" data-timer-id="${escapeHtml(
        t.timer_id
      )}" title="Delete timer" aria-label="Delete timer"><span class="material-symbols-outlined text-[18px]">delete</span></button>
      <h3 class="font-data-label text-data-label text-on-surface-variant tracking-widest uppercase mb-2">${escapeHtml(
        t.label
      )}</h3>
      <span class="font-display-timer text-2xl ${t.expired ? "text-tertiary" : "text-on-surface"}">${
          t.expired ? "DONE" : fmtClock(t.remaining_s)
        }</span></div>`
      )
      .join("");
  }

  function onTimerUpdate(p) {
    timers.set(p.timer_id, p);
    renderTimers();
  }
  function onTimerRemoved(id) {
    timers.delete(id);
    renderTimers();
  }

  function renderTimersClear() {
    timers.clear();
    renderTimers();
  }

  // Delegated click handler for the per-card delete buttons. Bound ONCE to the
  // stable #timer-list host because renderTimers() rewrites innerHTML every tick
  // (a per-card listener would be destroyed each second). The dataset.wired guard
  // keeps a demo reset — which re-runs hydrate() — from stacking duplicate
  // listeners (and thus duplicate POSTs).
  function wireTimerList() {
    const host = $("timer-list");
    if (!host || host.dataset.wired) return;
    host.dataset.wired = "1";
    host.addEventListener("click", async (e) => {
      const btn = e.target.closest(".timer-dismiss");
      if (!btn) return;
      btn.disabled = true;
      try {
        await stopTimer(btn.dataset.timerId);
      } catch (err) {
        btn.disabled = false; // card stays put so the user can retry
      }
    });
  }

  // --- transcript / clarify -------------------------------------------------
  // The transcript panel mirrors the old frontend: a single in-place interim
  // line plus appended finals, so Deepgram output is visible on every page.
  let interimEl = null;
  function clearInterim() {
    if (interimEl) {
      interimEl.remove();
      interimEl = null;
    }
  }

  function revealTranscript() {
    const panel = $("live-transcript");
    if (panel) panel.classList.remove("hidden");
  }

  // Append a finalized line. role "ai" -> blue + ">" marker (via CSS .transcript-ai);
  // anything else -> white user speech (.transcript-final). An AI reply also closes
  // any dangling interim line. Shared by Deepgram finals and AI messages.
  function appendFinalLine(text, role) {
    const el = $("live-transcript");
    if (!el) return;
    revealTranscript();
    clearInterim();
    const div = document.createElement("div");
    div.className = "transcript-line " + (role === "ai" ? "transcript-ai" : "transcript-final");
    div.textContent = text;
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
  }

  function onTranscript(p) {
    if (p && p.is_final) {
      appendFinalLine(p.text, "user");
      return;
    }
    const el = $("live-transcript");
    if (!el) return;
    revealTranscript();
    if (!interimEl) {
      interimEl = document.createElement("div");
      interimEl.className = "transcript-line transcript-interim";
      el.appendChild(interimEl);
    }
    interimEl.textContent = (p && p.text) || "";
    el.scrollTop = el.scrollHeight;
  }

  function clearTranscript() {
    clearInterim();
    const el = $("live-transcript");
    if (el) {
      el.innerHTML = "";
      el.classList.add("hidden");
    }
  }

  // Muting hides the transcript: drop any in-flight interim and wipe the box so no
  // spoken text lingers while muted. Dock-style panels (hidden-when-empty) re-hide;
  // the Guide's always-on panel (data-persistent) stays visible at its reserved
  // min-height so the layout doesn't jump.
  function clearTranscriptForMute() {
    clearInterim();
    const el = $("live-transcript");
    if (!el) return;
    el.innerHTML = "";
    if (!el.dataset.persistent) el.classList.add("hidden");
  }

  function renderClarify(message) {
    const el = $("clarify");
    if (el) el.textContent = message;
    else appendFinalLine(message, "ai");
  }

  function clearTransientState(opts) {
    const notesCleared = opts && opts.notesCleared;
    renderTimersClear();
    clearTranscript();
    const cl = $("clarify");
    if (cl) cl.textContent = "";
    // Reset the step tracker / active-protocol label back to the empty state.
    const cur = $("step-current");
    if (cur) cur.textContent = "No protocol loaded.";
    const pname = $("protocol-name");
    if (pname) pname.textContent = "No protocol loaded";
    const counter = $("step-counter");
    if (counter) counter.textContent = "STEP —";
    const phase = $("step-phase");
    if (phase) phase.textContent = "Protocol Phase —";
    const confirmBtn = $("confirm-step");
    if (confirmBtn) confirmBtn.disabled = true;
    const skipBtn = $("skip-action");
    if (skipBtn) skipBtn.disabled = true;
    const prevBtn = $("prev-step");
    if (prevBtn) prevBtn.disabled = true;
    const prev = $("step-prev");
    if (prev) prev.textContent = "";
    const nxt = $("step-next");
    if (nxt) nxt.textContent = "";
    const tracker = $("step-tracker");
    if (tracker) tracker.innerHTML = "";
    const panel = $("step-panel");
    if (panel) panel.classList.add("hidden");
    // Run-only breadcrumb controls + the cancel confirm modal go away with the run
    // (this also re-hides Reagent Prep after a demo reset).
    ["prep-open", "cancel-protocol", "cancel-modal"].forEach((id) => {
      const el = $(id);
      if (el) el.classList.add("hidden");
    });
    const lookup = $("inventory-result");
    if (lookup) lookup.textContent = "";
    if (notesCleared) {
      logCache = [];
      renderLog(logCache);
    }
  }

  async function handleDemoReset() {
    const btn = $("demo-reset") || document.querySelector('[data-action="demo-reset"]');
    if (btn) btn.disabled = true;
    try {
      const response = await fetch(API + "/api/demo/reset", { method: "POST" });
      const data = await response.json();
      if (data.ok) {
        clearTransientState({ notesCleared: data.notes_cleared });
        await hydrate();
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function wireDemoReset() {
    const btn = $("demo-reset") || document.querySelector('[data-action="demo-reset"]');
    if (btn) btn.addEventListener("click", handleDemoReset);
  }

  // --- typed/click command channel ------------------------------------------
  // Posts a transcript through the SAME spine as voice (/api/ingest -> route ->
  // handle_command -> broadcast over /ws/events), so on-screen buttons and
  // spoken commands behave identically. Events arrive via WS; ignore the body.
  async function ingestCommand(transcript) {
    await fetch(API + "/api/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript }),
    });
  }

  // Guide "Confirm Action": click == saying "next step". Disabled until a step
  // is active (renderStep toggles it), so it never fires with no protocol loaded.
  function wireGuideConfirm() {
    const btn = $("confirm-step");
    if (!btn || btn.dataset.wired) return;
    btn.dataset.wired = "1";
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      ingestCommand("next step").catch(() => {});
    });
  }

  // Guide "Cancel": opens a confirm modal; confirming sends "cancel protocol"
  // through the SAME /api/ingest spine as the voice command. (Voice cancels
  // immediately; only the on-screen button asks first.)
  function wireGuideCancel() {
    const openBtn = $("cancel-protocol");
    if (openBtn && !openBtn.dataset.wired) {
      openBtn.dataset.wired = "1";
      openBtn.addEventListener("click", () => {
        if (openBtn.classList.contains("hidden")) return;
        const m = $("cancel-modal");
        if (m) m.classList.remove("hidden");
      });
    }
    const dismiss = $("cancel-dismiss");
    if (dismiss && !dismiss.dataset.wired) {
      dismiss.dataset.wired = "1";
      dismiss.addEventListener("click", () => {
        const m = $("cancel-modal");
        if (m) m.classList.add("hidden");
      });
    }
    const confirm = $("cancel-confirm");
    if (confirm && !confirm.dataset.wired) {
      confirm.dataset.wired = "1";
      confirm.addEventListener("click", () => {
        const m = $("cancel-modal");
        if (m) m.classList.add("hidden");
        ingestCommand("cancel protocol").catch(() => {});
      });
    }
  }

  // --- in-memory log mirror so WS deltas can re-render the feed --------------
  let logCache = [];
  // Active Activity-Stream order; mirrors the #log-sort <select>. renderLog reads
  // it on every (re)render so WS deltas and notebook switches keep the chosen order.
  let logSortMode = "date-desc";
  function applyLogEntry(p) {
    const entry = {
      id: p.id,
      text: p.text,
      timestamp: p.timestamp,
      sample_id: p.sample_id,
      step_ref: p.step_ref,
      category: p.category,
      flag: p.flag,
      entry_type: p.entry_type,
      edited: p.edited,
    };
    const i = logCache.findIndex((e) => e.id === entry.id);
    if (i >= 0) logCache[i] = entry;
    else logCache.push(entry);
    renderLog(logCache);
    renderLogPreview(logCache);
    refreshNotebookCounts();
  }
  function applyLogRemoved(id) {
    logCache = logCache.filter((e) => e.id !== id);
    renderLog(logCache);
    renderLogPreview(logCache);
    refreshNotebookCounts();
  }
  function applyLogUpdate(p) {
    const e = logCache.find((x) => x.id === p.id);
    if (e) {
      e.text = p.text;
      if ("flag" in p) e.flag = p.flag;
      if ("entry_type" in p) e.entry_type = p.entry_type;
      if ("edited" in p) e.edited = p.edited;
      renderLog(logCache);
      renderLogPreview(logCache);
    }
  }

  // --- command-driven navigation --------------------------------------------
  // A voice/typed command lands the user on the matching page. Driven by the
  // same /ws/events stream, so voice and the typed box behave identically.
  function currentPage() {
    return (location.pathname.split("/").pop() || "dashboard.html") || "dashboard.html";
  }
  function navTo(page) {
    if (currentPage() !== page) window.location.href = page;
  }
  // Center the current-step card. Used by the #run hash on guide load and by the
  // "navigate" command when the operator is already on the guide.
  function scrollToRun() {
    const el = $("step-current");
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  function maybeNavigate(p) {
    switch (p.kind) {
      case "navigate":
        // show_protocol ("jump to guide" / "what step am I on"): land on the page;
        // if already there, just center the current step. A fresh cross-page load
        // carries the #run hash, which re-centers after hydrate.
        if (currentPage() === p.page) {
          if ((p.hash || "") === "#run") scrollToRun();
        } else {
          window.location.href = p.page + (p.hash || "");
        }
        return;
      case "step_change":
        if (p.loaded) {
          // Cross-page loads reload the Guide; flag the prep modal so it opens
          // after hydrate. An in-page load (already on the Guide) is handled by
          // the dispatcher below, which opens the modal directly.
          if (currentPage() !== "guide.html") flagPrepOnLoad();
          navTo("guide.html"); // only on protocol LOAD, not step nav
        }
        return;
      case "log_entry":
        // The auto-note from advancing a step must NOT yank the user off the
        // guide; only an explicit "log ..." command navigates to the notebook.
        if (p.step_log) return;
        return navTo("notebook.html");
      case "log_update":
      case "log_removed":
        return navTo("notebook.html");
      case "inventory_result":
      case "inventory_added":
        return navTo("inventory.html");
    }
  }

  // --- websocket dispatch ---------------------------------------------------
  function onCommandResult(p) {
    maybeNavigate(p);
    switch (p.kind) {
      case "step_change":
        renderStep(p);
        // A fresh protocol load pops the prep modal; a plain step advance does
        // not (the prep table is run-level, not per-step). If the modal is
        // already open, keep its availability numbers fresh.
        if (p.loaded) {
          // A load (incl. voice) changes recency: refresh the dashboard panel.
          refreshRecent();
          _deductTriggeredForRun = false; // new protocol — allow one auto-deduct
          openPrepModal(p.protocol_name, true);  // gate the run until prep confirmed
        } else if (prepModalOpen()) handlePrepCompute();
        return;
      case "prep_control":
        // Hands-free reagent prep: "done"/"start protocol" -> close (run starts);
        // "set samples to N" -> reflect the new count and re-scale.
        if (p.action === "close") return closePrepModal();
        if (p.action === "set_samples") {
          const input = $("prep-samples");
          if (input && p.sample_count != null) input.value = String(p.sample_count);
          if (prepModalOpen()) handlePrepCompute();
          else openPrepModal();
          return;
        }
        return;
      case "log_entry":
        return applyLogEntry(p);
      case "log_removed":
        return applyLogRemoved(p.id);
      case "log_update":
        return applyLogUpdate(p);
      case "timer_removed":
        return onTimerRemoved(p.timer_id);
      case "protocol_imported":
        refreshRecent(); // keeps the cold-start fallback list current
        return refreshProtocols();
      case "protocol_updated":
        refreshRecent(); // name/desc may have changed in the dashboard fallback
        return refreshProtocols();
      case "notebook_list":
        renderNotebooks(p);
        renderNotebookSelect(p);
        renderDashboardNotebooks(p);
        if ($("log-rows") || $("log-preview")) {
          fetchLog().then((l) => {
            logCache = l;
            renderLog(logCache);
            renderLogPreview(logCache);
          });
        }
        return;
      case "reset":
        clearTransientState({ notesCleared: p.notes_cleared });
        return hydrate();
      case "voice_state":
        // Muting wipes the transcript so no spoken text shows while muted; the
        // backend also stops broadcasting muted transcripts, so this just clears
        // anything already on screen at the moment of muting.
        if (p && p.muted) clearTranscriptForMute();
        return window.LabVoice.onVoiceState(p);
      case "clarify":
        return renderClarify(p.message);
      case "inventory_added":
        showToast(`"${p.name}" was added to inventory`);
        refreshInventory();
        return;
      case "inventory_result":
        return;
      case "navigate": // navigation handled in maybeNavigate; nothing to render
        return;
      case "ask_result":
        // The AI's answer to an "ask" (e.g. "what's next?") — render it as an AI
        // line so it's actually visible. maybeNavigate has no ask_result case, so
        // the user stays on the current page.
        appendFinalLine(p.answer, "ai");
        return;
      default:
        return;
    }
  }

  function dispatch(evt) {
    switch (evt.type) {
      case "transcript_update":
        return onTranscript(evt.payload);
      case "command_result":
        return onCommandResult(evt.payload);
      case "timer_update":
        return onTimerUpdate(evt.payload);
      case "error":
        return window.LabVoice.onError(evt.payload);
    }
  }

  let ws = null;
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/events`);
    ws.onmessage = (e) => {
      try {
        dispatch(JSON.parse(e.data));
      } catch (_) {}
    };
    ws.onclose = () => setTimeout(connect, 1500);
  }

  // --- voice/mic subsystem ---------------------------------------------------
  // Extracted to voice.js (loaded before app.js): it owns getUserMedia /
  // MediaRecorder / the /ws/audio socket + voice-state sessionStorage and exposes
  // window.LabVoice. app.js forwards voice_state/error WS events to it, wires the
  // dock via LabVoice.wireVoice(), and registers clearInterim as its stop hook.

  function wireNav() {
    const here = (location.pathname.split("/").pop() || "dashboard.html") || "dashboard.html";
    document.querySelectorAll("#main-nav [data-nav]").forEach((a) => {
      const match = a.getAttribute("data-nav") === here;
      a.classList.toggle("nav-active", match);
      if (match) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
  }

  // --- hydration error surface ----------------------------------------------
  // hydrate() previously swallowed every section error, so a broken API contract
  // looked like an empty panel. Each section now runs through hydrateSection():
  // the failure is logged and summarised in a dismissible page-level banner.
  const hydrateFailures = new Set();
  function showHydrateError(section) {
    hydrateFailures.add(section);
    let bar = $("hydrate-error");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "hydrate-error";
      bar.setAttribute("role", "alert");
      bar.className =
        "fixed top-0 inset-x-0 z-[100] bg-error-container text-on-error-container text-sm px-4 py-2 flex items-center justify-between gap-4 shadow-lg";
      const msg = document.createElement("span");
      msg.id = "hydrate-error-msg";
      const close = document.createElement("button");
      close.type = "button";
      close.setAttribute("aria-label", "Dismiss");
      close.className = "font-bold opacity-80 hover:opacity-100 shrink-0";
      close.textContent = "✕";
      close.addEventListener("click", () => bar.remove());
      bar.appendChild(msg);
      bar.appendChild(close);
      document.body.appendChild(bar);
    }
    const m = $("hydrate-error-msg");
    if (m)
      m.textContent = `Couldn't load: ${Array.from(hydrateFailures).join(
        ", "
      )}. The backend may be unavailable — showing what loaded.`;
  }
  function clearHydrateError() {
    hydrateFailures.clear();
    const bar = $("hydrate-error");
    if (bar) bar.remove();
  }
  async function hydrateSection(name, run) {
    try {
      await run();
    } catch (err) {
      console.warn(`[hydrate] ${name} failed:`, err);
      showHydrateError(name);
    }
  }

  // --- bootstrap ------------------------------------------------------------
  async function hydrate() {
    wireNav();
    clearHydrateError(); // start each (re)hydrate clean; a fixed section clears its error
    await hydrateSection("protocols", async () => {
      if ($("protocol-cards")) renderProtocolCards(await fetchProtocols());
    });
    await hydrateSection("recent protocols", async () => {
      if ($("recent-protocols")) renderRecentProtocols(await fetchRecent());
    });
    await hydrateSection("inventory", async () => {
      if ($("inventory-rows")) renderInventory(await fetchInventory());
    });
    await hydrateSection("log", async () => {
      if ($("log-rows") || $("log-preview")) {
        logCache = await fetchLog();
        renderLog(logCache);
        renderLogPreview(logCache);
      }
    });
    await hydrateSection("notebooks", async () => {
      if (
        $("notebook-list") ||
        $("notebook-title") ||
        $("notebook-select") ||
        $("dashboard-notebooks")
      ) {
        const data = await fetchNotebooks();
        renderNotebooks(data);
        renderNotebookSelect(data);
        renderDashboardNotebooks(data);
        wireNotebookNew();
        wireNotebookSelect();
      }
    });
    await hydrateSection("protocol state", async () => {
      if ($("step-tracker") || $("step-current")) {
        const st = await fetchState();
        renderStep(st.step);
        // Pop the prep modal once, right after a protocol load lands here.
        const active = st.step && st.step.current_index != null;
        if (active && $("prep-modal") && consumePrepOnLoad()) {
          openPrepModal(st.step.protocol_name, true);  // gate the run until confirmed
        }
        // Arriving via the nav "Jump to guide" button or a "jump to guide" voice
        // command (#run): center the current step, then drop the hash so a later
        // refresh doesn't re-scroll.
        if (location.hash === "#run") {
          scrollToRun();
          history.replaceState(null, "", location.pathname);
        }
      }
    });
    // Light the "Jump to guide" nav button on non-Guide pages (the Guide already
    // does this via renderStep above). Guarded so the Guide doesn't double-fetch.
    await hydrateSection("active run", async () => {
      if ($("resume-run") && !($("step-tracker") || $("step-current"))) {
        renderResumeRun((await fetchState()).step);
      }
    });
    renderTimers();
    wirePrepModal();
    wireImportModal();
    wireEditProtocolModal();
    wireAddItemModal();
    wireLogModal();
    wireNotebookExport();
    wireLogEditModal();
    wireLogSort();
    wireDemoReset();
    wireGuideConfirm();
    wireGuideCancel();
    wireStepActions();
    wireTimerList();
    // voice.js owns the mic; register clearInterim so a mic-stop also clears the
    // in-progress transcript line, then wire the dock buttons.
    window.LabVoice.setStopHook(clearInterim);
    window.LabVoice.wireVoice();
  }

  window.LabClient = {
    fetchProtocols,
    fetchRecent,
    fetchInventory,
    refreshInventory,
    addInventoryItem,
    fetchLog,
    fetchState,
    fetchScale,
    renderPrepTable,
    openPrepModal,
    closePrepModal,
    loadProtocol,
    importProtocol,
    handleProtocolImport,
    postLog,
    patchLog,
    exportNotebook,
    ingestCommand,
    renderProtocolCards,
    renderRecentProtocols,
    fetchFullProtocol,
    openEditProtocolModal,
    handleProtocolEdit,
    renderDashboardNotebooks,
    renderInventory,
    renderLog,
    renderLogFlag,
    renderStep,
    renderTimers,
    clearTransientState,
    handleDemoReset,
    onTranscript,
    startMic: (...a) => window.LabVoice.startMic(...a),
    stopMic: (...a) => window.LabVoice.stopMic(...a),
    hydrate,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      hydrate();
      connect();
      window.LabVoice.maybeResumeVoice();
    });
  } else {
    hydrate();
    connect();
    window.LabVoice.maybeResumeVoice();
  }
})();
