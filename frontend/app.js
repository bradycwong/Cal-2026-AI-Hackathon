// app.js — WS client + typed command box.
// Renders on the 4 LOCKED event types only. Adding a new command kind must NOT
// require a new branch here beyond a `kind` case inside command_result.

const $ = (id) => document.getElementById(id);

const els = {
  conn: $("chip-conn"),
  state: $("chip-state"),
  transcript: $("transcript"),
  stepPrev: $("step-prev"),
  stepCurrent: $("step-current"),
  stepNext: $("step-next"),
  timers: $("timers"),
  log: $("log"),
  inventory: $("inventory"),
  clarify: $("clarify"),
  clarifyPanel: $("clarify-panel"),
  form: $("composer"),
  input: $("composer-input"),
};

const timers = new Map(); // timer_id -> element

function setState(label, cls) {
  els.state.textContent = label;
  els.state.className = "chip " + (cls || "");
}

// --- WS wiring --------------------------------------------------------------
let ws;
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/events`);
  ws.onopen = () => {
    els.conn.textContent = "connected";
    els.conn.className = "chip chip-ok";
  };
  ws.onclose = () => {
    els.conn.textContent = "disconnected";
    els.conn.className = "chip chip-idle";
    setTimeout(connect, 1000);
  };
  ws.onmessage = (e) => dispatch(JSON.parse(e.data));
}

// --- the 4 outer event types ------------------------------------------------
function dispatch(evt) {
  switch (evt.type) {
    case "transcript_update":
      return onTranscript(evt.payload);
    case "command_result":
      return onCommandResult(evt.payload);
    case "timer_update":
      return onTimerUpdate(evt.payload);
    case "error":
      return onError(evt.payload);
    default:
      console.warn("unknown event type", evt);
  }
}

function onTranscript(p) {
  const div = document.createElement("div");
  div.className = "line " + (p.is_final ? "final" : "interim");
  div.textContent = p.text;
  els.transcript.appendChild(div);
  els.transcript.scrollTop = els.transcript.scrollHeight;
  setState("thinking", "chip-warn");
}

// command_result dispatches on `kind` — the only place that grows per feature.
function onCommandResult(p) {
  switch (p.kind) {
    case "step_change":
      return onStepChange(p);
    case "log_entry":
      return onLogEntry(p);
    case "inventory_result":
      return onInventory(p);
    case "clarify":
      return onClarify(p);
    default:
      console.warn("unknown command_result kind", p);
  }
  setState("done", "chip-ok");
}

function fmtStep(s) {
  return s ? `Step ${s.id}: ${s.text}` : "";
}

function onStepChange(p) {
  els.stepPrev.textContent = fmtStep(p.prev_step);
  els.stepCurrent.textContent = p.current_step ? fmtStep(p.current_step) : "Protocol complete.";
  els.stepNext.textContent = fmtStep(p.next_step);
  clearClarify();
  setState("done", "chip-ok");
}

function onLogEntry(p) {
  const li = document.createElement("li");
  const sample = p.sample_id ? ` · sample ${p.sample_id}` : "";
  const step = p.step_ref ? ` · step ${p.step_ref}` : "";
  li.innerHTML = `<span class="log-time">${p.timestamp}</span>` +
    `<span class="log-text">${escapeHtml(p.text)}</span>` +
    `<span class="log-meta">${escapeHtml(sample + step)}</span>`;
  els.log.prepend(li);
  clearClarify();
  setState("done", "chip-ok");
}

function onInventory(p) {
  els.inventory.innerHTML =
    `<div class="inv-name">${escapeHtml(p.name)}</div>` +
    `<div class="inv-loc">${escapeHtml(p.location)}</div>` +
    `<div class="inv-qty">${escapeHtml(p.quantity_approx)}</div>`;
  clearClarify();
  setState("done", "chip-ok");
}

function onClarify(p) {
  els.clarify.innerHTML = `<div class="clarify-msg">${escapeHtml(p.message)}</div>`;
  els.clarifyPanel.classList.add("active");
  setState("needs input", "chip-warn");
}

function clearClarify() {
  els.clarify.innerHTML = `<div class="muted">—</div>`;
  els.clarifyPanel.classList.remove("active");
}

function onTimerUpdate(p) {
  let el = timers.get(p.timer_id);
  if (!el) {
    const empty = els.timers.querySelector(".muted");
    if (empty) empty.remove();
    el = document.createElement("div");
    el.className = "timer-card";
    els.timers.appendChild(el);
    timers.set(p.timer_id, el);
  }
  el.classList.toggle("expired", !!p.expired);
  const mm = String(Math.floor(p.remaining_s / 60)).padStart(2, "0");
  const ss = String(p.remaining_s % 60).padStart(2, "0");
  el.innerHTML =
    `<div class="timer-label">${escapeHtml(p.label)}</div>` +
    `<div class="timer-clock">${p.expired ? "DONE" : `${mm}:${ss}`}</div>`;
  if (p.expired) chime();
  setState("done", "chip-ok");
}

function onError(p) {
  els.clarify.innerHTML = `<div class="clarify-msg error">${escapeHtml(p.message)}</div>`;
  els.clarifyPanel.classList.add("active");
  setState("error", "chip-err");
}

// --- typed input ------------------------------------------------------------
els.form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const transcript = els.input.value.trim();
  if (!transcript) return;
  els.input.value = "";
  setState("thinking", "chip-warn");
  try {
    await fetch("/api/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript }),
    });
  } catch (err) {
    onError({ message: String(err) });
  }
});

// --- helpers ----------------------------------------------------------------
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

let audioCtx;
function chime() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator();
    const g = audioCtx.createGain();
    o.frequency.value = 880;
    o.connect(g);
    g.connect(audioCtx.destination);
    g.gain.setValueAtTime(0.25, audioCtx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.6);
    o.start();
    o.stop(audioCtx.currentTime + 0.6);
  } catch (_) { /* no-op */ }
}

connect();
