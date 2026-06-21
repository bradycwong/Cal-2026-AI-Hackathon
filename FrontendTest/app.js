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
    const header = `<div class="grid grid-cols-12 gap-4 px-6 py-3 bg-surface-container-low text-on-surface-variant text-xs font-bold uppercase tracking-wider border-b border-outline-variant sticky top-0 z-10">
      <div class="col-span-4">Reagent Name</div><div class="col-span-3">Location</div><div class="col-span-3">Category</div><div class="col-span-2">Status</div></div>`;
    const rows = items
      .map(
        (it) => `<div class="inventory-row grid grid-cols-12 gap-4 px-6 py-5 border-b border-outline-variant items-center transition-colors">
      <div class="col-span-4 flex items-center gap-3">
        <div class="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center text-primary"><span class="material-symbols-outlined text-lg">science</span></div>
        <div><p class="font-bold text-on-surface">${escapeHtml(it.name)}</p><p class="text-[10px] font-data-label text-outline">${escapeHtml(
          it.code || ""
        )}</p></div>
      </div>
      <div class="col-span-3"><p class="text-on-surface text-sm">${escapeHtml(it.location)}</p></div>
      <div class="col-span-3"><span class="bg-primary-container/20 text-primary-fixed px-2 py-0.5 rounded text-xs font-bold">${escapeHtml(
        it.category
      )}</span></div>
      <div class="col-span-2"><span class="text-xs font-bold uppercase">${escapeHtml(it.status)}</span></div>
    </div>`
      )
      .join("");
    host.innerHTML = header + (rows || `<div class="p-12 text-center opacity-40">Inventory is empty.</div>`);
  }

  // Plan 3 hook: a log entry may carry an optional `flag`. Render its mark when present.
  function flagMark(flag) {
    if (!flag || !flag.symbol) return "";
    const cls = flag.match ? "text-secondary" : "text-tertiary";
    return ` <span class="log-flag ${cls}" title="${escapeHtml(flag.detail || "")}">${escapeHtml(
      flag.symbol
    )}</span>`;
  }

  function renderLog(log) {
    const host = $("log-rows");
    if (!host) return;
    if (!log.length) {
      host.innerHTML = `<div class="p-12 flex flex-col items-center justify-center opacity-30 select-none"><span class="material-symbols-outlined text-6xl mb-4">history_edu</span><p class="font-headline-md">No log entries yet</p></div>`;
      return;
    }
    host.innerHTML = log
      .map(
        (e) => `<div class="log-entry-row grid grid-cols-12 gap-4 px-6 py-5 border-b border-outline-variant items-center transition-colors" data-log-id="${e.id}">
      <div class="col-span-3 font-data-label text-on-surface text-sm">${escapeHtml(fmtTime(e.timestamp))}</div>
      <div class="col-span-3"><span class="bg-primary-container/20 text-primary-fixed px-2 py-0.5 rounded text-xs font-bold">${escapeHtml(
        e.category || (e.sample_id ? "Sample " + e.sample_id : "Note")
      )}</span></div>
      <div class="col-span-6"><p class="text-on-surface text-sm log-text">${escapeHtml(e.text)}${flagMark(
          e.flag
        )}</p></div>
    </div>`
      )
      .join("");
  }

  function renderStep(step) {
    if (!step) return;
    const cur = $("step-current");
    if (cur && step.current_step) cur.textContent = step.current_step.text;
    const name = $("protocol-name");
    if (name && step.protocol_name) name.textContent = step.protocol_name;

    const tracker = $("step-tracker");
    if (tracker && Array.isArray(step.all_steps)) {
      const idx = step.current_index == null ? -1 : step.current_index;
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
      host.innerHTML = `<p class="text-on-surface-variant text-xs uppercase tracking-widest">No active timers</p>`;
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
  function renderTranscript(text) {
    const el = $("live-transcript");
    if (el) el.textContent = text;
  }

  function renderClarify(message) {
    const el = $("clarify");
    if (el) el.textContent = message;
    else renderTranscript(message);
  }

  function clearTransientState() {
    renderTimersClear();
    renderTranscript("");
    const cl = $("clarify");
    if (cl) cl.textContent = "";
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
  }
  function applyLogRemoved(id) {
    logCache = logCache.filter((e) => e.id !== id);
    renderLog(logCache);
  }
  function applyLogUpdate(p) {
    const e = logCache.find((x) => x.id === p.id);
    if (e) {
      e.text = p.text;
      if ("flag" in p) e.flag = p.flag;
      renderLog(logCache);
    }
  }

  // --- websocket dispatch ---------------------------------------------------
  function onCommandResult(p) {
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
      case "voice_state":
        return; // mute/unmute badge is non-essential for the snapshot pages
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
        return renderTranscript(evt.payload.text);
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

  // --- bootstrap ------------------------------------------------------------
  async function hydrate() {
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
      if ($("step-tracker") || $("step-current")) {
        const st = await fetchState();
        renderStep(st.step);
      }
    } catch (_) {}
    renderTimers();
  }

  window.LabClient = {
    fetchProtocols,
    fetchInventory,
    fetchLog,
    fetchState,
    loadProtocol,
    postLog,
    renderProtocolCards,
    renderInventory,
    renderLog,
    renderStep,
    renderTimers,
    clearTransientState,
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
