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
  async function fetchInventory() {
    return (await getJSON("/api/inventory")).items || [];
  }
  async function fetchLog() {
    return (await getJSON("/api/log")).log || [];
  }
  async function fetchState() {
    return getJSON("/api/state");
  }

  async function loadProtocol(id) {
    await fetch(`${API}/api/protocols/${encodeURIComponent(id)}/load`, {
      method: "POST",
    });
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

  async function refreshProtocols() {
    if ($("protocol-cards")) renderProtocolCards(await fetchProtocols());
  }

  async function handleProtocolImport() {
    const result = $("import-result");
    const name = ($("import-name") || {}).value || "";
    const text = ($("import-text") || {}).value || "";
    if (!text.trim()) {
      if (result) {
        result.textContent = "Paste at least one step.";
        result.className = "text-sm mb-4 min-h-[1.25rem] text-tertiary";
      }
      return;
    }
    if (result) {
      result.textContent = "Importing...";
      result.className = "text-sm mb-4 min-h-[1.25rem] text-on-surface-variant";
    }
    try {
      const data = await importProtocol(text, name);
      if (data.ok) {
        if (result) {
          result.textContent = `Imported "${data.protocol.name}". ${data.load_hint || ""}`;
          result.className = "text-sm mb-4 min-h-[1.25rem] text-secondary";
        }
        await refreshProtocols();
        const ta = $("import-text");
        const nm = $("import-name");
        if (ta) ta.value = "";
        if (nm) nm.value = "";
        setTimeout(closeImportModal, 1200);
      } else if (result) {
        result.textContent = data.error || "Import failed.";
        result.className = "text-sm mb-4 min-h-[1.25rem] text-tertiary";
      }
    } catch (e) {
      if (result) {
        result.textContent = "Import failed: " + e.message;
        result.className = "text-sm mb-4 min-h-[1.25rem] text-tertiary";
      }
    }
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
  }

  // --- inventory add / edit / delete ----------------------------------------
  let inventoryCache = []; // last-rendered items, for edit prefill (index-based)
  let editingIndex = null; // null = add mode; number = editing that row index

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

  async function updateInventoryItem(index, payload) {
    const r = await fetch(`${API}/api/inventory/${index}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `update failed (${r.status})`);
    return data;
  }

  async function deleteInventoryItem(index) {
    const r = await fetch(`${API}/api/inventory/${index}`, { method: "DELETE" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `delete failed (${r.status})`);
    return data;
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
    ["additem-name", "additem-amount", "additem-unit", "additem-location", "additem-date", "additem-expiration"].forEach(
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
    editingIndex = null;
    setModalMode("add");
    clearAddItemForm();
    // Default "Date Created" to today for convenience.
    const dc = $("additem-date");
    if (dc) dc.value = new Date().toISOString().slice(0, 10);
    m.classList.remove("hidden");
    const name = $("additem-name");
    if (name) name.focus();
  }

  function openEditItemModal(index) {
    const m = $("additem-modal");
    const it = inventoryCache[index];
    if (!m || !it) return;
    editingIndex = index;
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
    set("additem-expiration", asDateInputValue(it.expiration));
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
    const expiration = (($("additem-expiration") || {}).value || "").trim();
    if (!name) {
      setMsg("Reagent name is required.", "text-tertiary");
      return;
    }
    setMsg("Saving...", "text-on-surface-variant");
    const isEdit = editingIndex !== null;
    try {
      if (isEdit) {
        await updateInventoryItem(editingIndex, { name, amount, unit, location, date, expiration });
      } else {
        await addInventoryItem({ name, amount, unit, location, date, expiration });
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

  async function handleDeleteItem(index) {
    const it = inventoryCache[index];
    const name = it ? it.name : "this item";
    if (!window.confirm(`Delete "${name}" from inventory?`)) return;
    try {
      await deleteInventoryItem(index);
      await refreshInventory();
      showToast(`"${name}" was deleted`);
    } catch (e) {
      showToast("Could not delete: " + e.message);
    }
  }

  function wireAddItemModal() {
    const open = $("add-item");
    if (!open) return;
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
        if (editBtn) openEditItemModal(parseInt(editBtn.dataset.index, 10));
        else if (delBtn) handleDeleteItem(parseInt(delBtn.dataset.index, 10));
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
        const reagents = (p.reagents || [])
          .map(
            (r) =>
              `<span class="text-[12px] bg-surface-variant px-2 py-1 rounded border border-outline-variant">${escapeHtml(
                r
              )}</span>`
          )
          .join("");
        const archived = p.status === "ARCHIVED";
        const button = archived
          ? `<button class="w-full border border-outline text-on-surface py-3 rounded-lg font-bold flex items-center justify-center gap-2" disabled><span class="material-symbols-outlined">history</span>Archived</button>`
          : `<button class="protocol-load w-full bg-primary text-on-primary py-3 rounded-lg font-bold hover:bg-primary/90 transition-all active:opacity-80 flex items-center justify-center gap-2" data-protocol-id="${escapeHtml(
              p.id
            )}"><span class="material-symbols-outlined">play_arrow</span>Load Protocol</button>`;
        return `<div class="protocol-card bg-surface-container-low rounded-xl p-6 flex flex-col">
  <div class="flex justify-between items-start mb-4">
    <div class="w-12 h-12 rounded-lg bg-surface-variant flex items-center justify-center">
      <span class="material-symbols-outlined text-primary" style="font-variation-settings:'FILL' 1;">science</span>
    </div>
    <span class="font-data-label text-xs px-2 py-1 rounded ${badge}">${escapeHtml(
          p.status.replace("_", " ")
        )}</span>
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
  }

  function renderInventory(items) {
    const host = $("inventory-rows");
    if (!host) return;
    inventoryCache = items;
    const header = `<div class="grid grid-cols-12 gap-4 px-6 py-3 bg-surface-container-low text-on-surface-variant text-xs font-bold uppercase tracking-wider border-b border-outline-variant sticky top-0 z-10">
      <div class="col-span-3">Reagent Name</div><div class="col-span-2">Amount</div><div class="col-span-2">Location</div><div class="col-span-2">Date Created</div><div class="col-span-2">Expiration</div><div class="col-span-1 text-right">Actions</div></div>`;
    const rows = items
      .map((it, i) => {
        const amtRaw = (it.amount == null ? "" : String(it.amount)).trim();
        const unit = (it.unit == null ? "" : String(it.unit)).trim();
        const amtNum = parseFloat(amtRaw);
        const isZero = amtRaw !== "" && !isNaN(amtNum) && amtNum === 0;
        const depletedCls = isZero ? "inv-depleted" : "";
        const amtCls = isZero ? "text-error font-bold" : "text-on-surface";
        const unitCls = isZero ? "text-error" : "text-on-surface-variant";
        const amtText = amtRaw === "" ? "—" : escapeHtml(amtRaw);
        const unitText = amtRaw !== "" && unit
          ? ` <span class="text-sm ${unitCls}">${escapeHtml(unit)}</span>`
          : "";
        return `<div class="inventory-row grid grid-cols-12 gap-4 px-6 py-5 border-b border-outline-variant items-center transition-colors group ${depletedCls}">
      <div class="col-span-3 flex items-center gap-3">
        <div class="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center text-primary"><span class="material-symbols-outlined text-lg">science</span></div>
        <div><p class="font-bold text-on-surface">${escapeHtml(it.name)}</p><p class="text-[10px] font-data-label text-outline">${escapeHtml(
          it.code || ""
        )}</p></div>
      </div>
      <div class="col-span-2 whitespace-nowrap"><span class="text-sm font-data-label ${amtCls}">${amtText}</span>${unitText}</div>
      <div class="col-span-2"><p class="text-on-surface text-sm">${escapeHtml(it.location)}</p></div>
      <div class="col-span-2"><p class="font-data-label text-on-surface-variant text-sm">${escapeHtml(
        it.date || "—"
      )}</p></div>
      <div class="col-span-2"><p class="font-data-label text-on-surface-variant text-sm">${escapeHtml(
        it.expiration || "N/A"
      )}</p></div>
      <div class="col-span-1 flex items-center justify-end gap-1 opacity-60 group-hover:opacity-100 transition-opacity">
        <button type="button" class="inv-edit text-on-surface-variant hover:text-primary" data-index="${i}" title="Edit"><span class="material-symbols-outlined text-base">edit</span></button>
        <button type="button" class="inv-delete text-on-surface-variant hover:text-error" data-index="${i}" title="Delete"><span class="material-symbols-outlined text-base">delete</span></button>
      </div>
    </div>`;
      })
      .join("");
    host.innerHTML = header + (rows || `<div class="p-12 text-center opacity-40">Inventory is empty.</div>`);
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

  function renderLog(log) {
    const host = $("log-rows");
    if (!host) return;
    if (!log.length) {
      host.innerHTML = `<div class="p-12 flex flex-col items-center justify-center opacity-30 select-none"><span class="material-symbols-outlined text-6xl mb-4">history_edu</span><p class="font-headline-md">No log entries yet</p></div>`;
      return;
    }
    host.innerHTML = log
      .map((e) => {
        const flagged = e.flag && e.flag.status === "mismatch" ? " flagged" : "";
        return `<div class="log-entry-row${flagged} grid grid-cols-12 gap-4 px-6 py-5 border-b border-outline-variant items-center transition-colors" data-log-id="${e.id}">
      <div class="col-span-3 font-data-label text-on-surface text-sm">${escapeHtml(fmtTime(e.timestamp))}</div>
      <div class="col-span-3"><span class="bg-primary-container/20 text-primary-fixed px-2 py-0.5 rounded text-xs font-bold">${escapeHtml(
        e.category || (e.sample_id ? "Sample " + e.sample_id : "Note")
      )}</span></div>
      <div class="col-span-6"><p class="text-on-surface text-sm log-text">${escapeHtml(
        e.text
      )}</p>${renderLogFlag(e.flag)}</div>
    </div>`;
      })
      .join("");
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
  async function refreshNotebookFeed() {
    if ($("notebook-list") || $("notebook-title")) renderNotebooks(await fetchNotebooks());
    if ($("log-rows")) {
      logCache = await fetchLog();
      renderLog(logCache);
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
  function wireNotebookNew() {
    const btn = $("notebook-new");
    if (!btn || btn.dataset.wired) return;
    btn.dataset.wired = "1";
    btn.addEventListener("click", () => {
      const name = window.prompt("Name your new notebook:");
      if (name && name.trim()) createNotebook(name.trim());
    });
  }

  function renderStep(step) {
    if (!step) return;
    const cur = $("step-current");
    if (cur && step.current_step) cur.textContent = step.current_step.text;
    const name = $("protocol-name");
    if (name && step.protocol_name) name.textContent = step.protocol_name;

    const idx = step.current_index == null ? -1 : step.current_index;
    const panel = $("step-panel");
    if (panel && idx >= 0) panel.classList.remove("hidden");

    const tracker = $("step-tracker");
    if (tracker && Array.isArray(step.all_steps)) {
      tracker.innerHTML = step.all_steps
        .map((s, i) => {
          let icon = "circle";
          let cls = "border-outline-variant opacity-50";
          let label = "Pending";
          let labelCls = "text-on-surface-variant";
          if (i < idx) {
            icon = "check_circle";
            cls = "border-secondary";
            label = "Completed";
            labelCls = "text-secondary";
          } else if (i === idx) {
            icon = "pending";
            cls = "border-primary";
            label = "In Progress";
            labelCls = "text-primary";
          }
          return `<div class="flex items-center gap-3 p-3 bg-surface-container-low rounded-lg border-l-4 ${cls}">
        <span class="material-symbols-outlined text-sm">${icon}</span>
        <div class="flex flex-col"><span class="text-xs font-bold text-on-surface">${escapeHtml(
          s.text
        )}</span><span class="text-[10px] uppercase ${labelCls}">${label}</span></div></div>`;
        })
        .join("");
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
        (t) => `<div class="timer-card bg-surface-container-low border border-outline-variant rounded-xl p-4 flex flex-col items-center" data-timer-id="${escapeHtml(
          t.timer_id
        )}">
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

  function onTranscript(p) {
    const el = $("live-transcript");
    if (!el) return;
    revealTranscript();
    if (p && p.is_final) {
      clearInterim();
      const div = document.createElement("div");
      div.className = "transcript-line transcript-final";
      div.textContent = p.text;
      el.appendChild(div);
    } else {
      if (!interimEl) {
        interimEl = document.createElement("div");
        interimEl.className = "transcript-line transcript-interim";
        el.appendChild(interimEl);
      }
      interimEl.textContent = (p && p.text) || "";
    }
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

  function renderClarify(message) {
    const el = $("clarify");
    if (el) el.textContent = message;
    else onTranscript({ text: message, is_final: true });
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
    const prev = $("step-prev");
    if (prev) prev.textContent = "";
    const nxt = $("step-next");
    if (nxt) nxt.textContent = "";
    const tracker = $("step-tracker");
    if (tracker) tracker.innerHTML = "";
    const panel = $("step-panel");
    if (panel) panel.classList.add("hidden");
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

  // --- in-memory log mirror so WS deltas can re-render the feed --------------
  let logCache = [];
  function applyLogEntry(p) {
    const entry = {
      id: p.id,
      text: p.text,
      timestamp: p.timestamp,
      sample_id: p.sample_id,
      step_ref: p.step_ref,
      category: p.category,
      flag: p.flag,
    };
    const i = logCache.findIndex((e) => e.id === entry.id);
    if (i >= 0) logCache[i] = entry;
    else logCache.push(entry);
    renderLog(logCache);
    refreshNotebookCounts();
  }
  function applyLogRemoved(id) {
    logCache = logCache.filter((e) => e.id !== id);
    renderLog(logCache);
    refreshNotebookCounts();
  }
  function applyLogUpdate(p) {
    const e = logCache.find((x) => x.id === p.id);
    if (e) {
      e.text = p.text;
      if ("flag" in p) e.flag = p.flag;
      renderLog(logCache);
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
  function maybeNavigate(p) {
    switch (p.kind) {
      case "step_change":
        if (p.loaded) navTo("guide.html"); // only on protocol LOAD, not step nav
        return;
      case "log_entry":
      case "log_update":
      case "log_removed":
        return navTo("notebook.html");
      case "inventory_result":
        return navTo("inventory.html");
    }
  }

  // --- websocket dispatch ---------------------------------------------------
  function onCommandResult(p) {
    maybeNavigate(p);
    switch (p.kind) {
      case "step_change":
        return renderStep(p);
      case "log_entry":
        return applyLogEntry(p);
      case "log_removed":
        return applyLogRemoved(p.id);
      case "log_update":
        return applyLogUpdate(p);
      case "timer_removed":
        return onTimerRemoved(p.timer_id);
      case "protocol_imported":
        return refreshProtocols();
      case "notebook_list":
        renderNotebooks(p);
        if ($("log-rows")) {
          fetchLog().then((l) => {
            logCache = l;
            renderLog(logCache);
          });
        }
        return;
      case "reset":
        clearTransientState({ notesCleared: p.notes_cleared });
        return hydrate();
      case "voice_state":
        return onVoiceState(p);
      case "clarify":
        return renderClarify(p.message);
      case "inventory_result":
      case "ask_result":
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
        return;
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

  // --- voice: arm-once mic -> /ws/audio -> Deepgram (server-proxied) --------
  // Ported from the original frontend so every served page can feed Deepgram.
  let micStream = null;
  let recorder = null;
  let audioWS = null;
  let manualStop = false;
  let reconnects = 0;
  let voiceMuted = false;
  const MAX_RECONNECTS = 3;

  function pickMime() {
    const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
    return (
      candidates.find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || ""
    );
  }

  function setVoiceStatus(text, listening) {
    const s = $("voice-status");
    if (s) s.textContent = text;
    const pulse = $("voice-pulse");
    if (pulse) pulse.classList.toggle("voice-pulse-on", !!listening);
  }

  function setVoiceUI(active) {
    const toggle = $("voice-toggle");
    if (toggle) {
      toggle.setAttribute("aria-pressed", active ? "true" : "false");
      toggle.title = active ? "Stop voice session" : "Start voice session";
    }
    const mute = $("voice-mute");
    if (mute) {
      mute.disabled = !active;
      mute.textContent = voiceMuted ? "mic_off" : "mic";
      mute.title = voiceMuted ? "Unmute" : "Mute";
    }
    if (!active) setVoiceStatus("Voice off", false);
    else setVoiceStatus(voiceMuted ? "Muted" : "Listening", !voiceMuted);
  }

  function onVoiceState(p) {
    voiceMuted = !!p.muted;
    if (micStream) setVoiceUI(true);
  }

  async function startMic() {
    manualStop = false;
    reconnects = 0;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setVoiceStatus("Needs https/localhost", false);
      return;
    }
    if (!window.MediaRecorder) {
      setVoiceStatus("Unsupported browser", false);
      return;
    }
    const mimeType = pickMime();
    if (!mimeType) {
      setVoiceStatus("No audio codec", false);
      return;
    }
    setVoiceStatus("Connecting", false);
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
    } catch (err) {
      setVoiceStatus("Mic denied", false);
      return;
    }
    openAudioSocket(mimeType);
  }

  // Each socket gets its own MediaRecorder so the webm header is sent fresh on
  // (re)connect; a header mid-stream would make the new Deepgram session deaf.
  function openAudioSocket(mimeType) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    audioWS = new WebSocket(`${proto}://${location.host}/ws/audio`);
    audioWS.binaryType = "arraybuffer";
    audioWS.onopen = () => {
      reconnects = 0;
      recorder = new MediaRecorder(micStream, mimeType ? { mimeType } : undefined);
      recorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0 && audioWS && audioWS.readyState === WebSocket.OPEN) {
          e.data.arrayBuffer().then((buf) => audioWS.send(buf)).catch(() => {});
        }
      };
      recorder.onerror = () => setVoiceStatus("Recorder error", false);
      recorder.start(250);
      setVoiceUI(true);
    };
    audioWS.onclose = () => {
      try {
        if (recorder && recorder.state !== "inactive") recorder.stop();
      } catch (_) {}
      recorder = null;
      if (manualStop) {
        finalizeStop();
        return;
      }
      if (micStream && reconnects < MAX_RECONNECTS) {
        reconnects += 1;
        setVoiceStatus("Reconnecting", false);
        setTimeout(() => {
          if (!manualStop && micStream) openAudioSocket(mimeType);
        }, 500 * reconnects);
      } else {
        setVoiceStatus("Connection lost", false);
        finalizeStop();
      }
    };
    audioWS.onerror = () => {};
  }

  function stopMic() {
    manualStop = true;
    try {
      if (recorder && recorder.state !== "inactive") recorder.stop();
    } catch (_) {}
    try {
      if (audioWS && audioWS.readyState === WebSocket.OPEN) {
        audioWS.send(JSON.stringify({ type: "stop" }));
        audioWS.close();
      }
    } catch (_) {}
    if (!audioWS || audioWS.readyState === WebSocket.CLOSED) finalizeStop();
  }

  function finalizeStop() {
    try {
      if (micStream) micStream.getTracks().forEach((t) => t.stop());
    } catch (_) {}
    recorder = null;
    micStream = null;
    audioWS = null;
    reconnects = 0;
    voiceMuted = false;
    clearInterim();
    setVoiceUI(false);
  }

  function sendMuteControl(muted) {
    if (!audioWS || audioWS.readyState !== WebSocket.OPEN) return;
    audioWS.send(JSON.stringify({ type: "set_muted", muted }));
  }

  function wireNav() {
    const here = (location.pathname.split("/").pop() || "dashboard.html") || "dashboard.html";
    document.querySelectorAll("#main-nav [data-nav]").forEach((a) => {
      const match = a.getAttribute("data-nav") === here;
      a.classList.toggle("nav-active", match);
      if (match) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
  }

  function wireVoice() {
    const toggle = $("voice-toggle");
    if (toggle && !toggle.dataset.wired) {
      toggle.dataset.wired = "1";
      toggle.addEventListener("click", () => {
        if (micStream) stopMic();
        else startMic();
      });
    }
    const mute = $("voice-mute");
    if (mute && !mute.dataset.wired) {
      mute.dataset.wired = "1";
      mute.addEventListener("click", () => {
        if (!micStream) return;
        sendMuteControl(!voiceMuted);
      });
    }
    setVoiceUI(!!micStream);
  }

  // --- bootstrap ------------------------------------------------------------
  async function hydrate() {
    wireNav();
    try {
      if ($("protocol-cards")) renderProtocolCards(await fetchProtocols());
    } catch (_) {}
    try {
      if ($("inventory-rows")) renderInventory(await fetchInventory());
    } catch (_) {}
    try {
      if ($("log-rows")) {
        logCache = await fetchLog();
        renderLog(logCache);
      }
    } catch (_) {}
    try {
      if ($("notebook-list") || $("notebook-title")) {
        renderNotebooks(await fetchNotebooks());
        wireNotebookNew();
      }
    } catch (_) {}
    try {
      if ($("step-tracker") || $("step-current")) {
        const st = await fetchState();
        renderStep(st.step);
      }
    } catch (_) {}
    renderTimers();
    wireImportModal();
    wireAddItemModal();
    wireDemoReset();
    wireVoice();
  }

  window.LabClient = {
    fetchProtocols,
    fetchInventory,
    refreshInventory,
    addInventoryItem,
    fetchLog,
    fetchState,
    loadProtocol,
    importProtocol,
    handleProtocolImport,
    postLog,
    renderProtocolCards,
    renderInventory,
    renderLog,
    renderLogFlag,
    renderStep,
    renderTimers,
    clearTransientState,
    handleDemoReset,
    onTranscript,
    startMic,
    stopMic,
    hydrate,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      hydrate();
      connect();
    });
  } else {
    hydrate();
    connect();
  }
})();
