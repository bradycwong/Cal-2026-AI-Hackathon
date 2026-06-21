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
  micBtn: $("mic-btn"),
  muteBtn: $("mute-btn"),
  mic: $("chip-mic"),
  transcriptHint: document.querySelector(".panel-hint"),
  srStatus: $("sr-status"),
  alertBanner: $("alert-banner"),
};

const DEMO_LINES = [
  "Load DNA extraction protocol",
  "What's next",
  "Go back",
  "Repeat that",
  "log: added 200 uL lysis buffer to sample A",
  "Scratch that",
  "Change that to added 300 uL lysis buffer",
  "Start a 10-minute timer",
  "Where's the proteinase K?",
  "How much lysis buffer in step 1?"
];

function populateDemoLines() {
  const list = $("demo-lines");
  if (!list) return;
  list.innerHTML = "";
  DEMO_LINES.forEach((line) => {
    const option = document.createElement("option");
    option.value = line;
    list.appendChild(option);
  });
}

const timers = new Map(); // timer_id -> element

function setState(label, cls) {
  els.state.textContent = label;
  els.state.className = "chip " + (cls || "");
}

// Announce a concise message to screen readers via the polite live region.
function announce(msg) {
  if (!els.srStatus || !msg) return;
  els.srStatus.textContent = "";        // re-trigger even if text repeats
  window.requestAnimationFrame(() => { els.srStatus.textContent = msg; });
}

function fmtClock(s) {
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${mm}:${ss}`;
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

let interimEl = null;
function clearInterimTranscript() {
  if (interimEl) {
    interimEl.remove();
    interimEl = null;
  }
}

function onTranscript(p) {
  if (p.is_final) {
    clearInterimTranscript();
    const div = document.createElement("div");
    div.className = "line final";
    div.textContent = p.text;
    els.transcript.appendChild(div);
    setState("thinking", "chip-warn");
  } else {
    // interim: update a single in-place line so the panel doesn't flood
    if (!interimEl) {
      interimEl = document.createElement("div");
      interimEl.className = "line interim";
      els.transcript.appendChild(interimEl);
    }
    interimEl.textContent = p.text;
    setState("listening", "chip-warn");
  }
  els.transcript.scrollTop = els.transcript.scrollHeight;
}

// command_result dispatches on `kind` — the only place that grows per feature.
function onCommandResult(p) {
  switch (p.kind) {
    case "step_change":
      return onStepChange(p);
    case "log_entry":
      return onLogEntry(p);
    case "log_removed":
      return onLogRemoved(p);
    case "log_update":
      return onLogUpdate(p);
    case "ask_result":
      return onAskResult(p);
    case "inventory_result":
      return onInventory(p);
    case "clarify":
      return onClarify(p);
    case "voice_state":
      return onVoiceState(p);
    default:
      console.warn("unknown command_result kind", p);
  }
  setState("done", "chip-ok");
}

function fmtStep(s) {
  return s ? `Step ${s.id}: ${s.text}` : "";
}

function onStepChange(p) {
  // A null prev_step means this is the first step of a freshly loaded protocol;
  // the server cleared its timers, so drop stale timer cards to match.
  if (!p.prev_step) clearTimerCards();
  els.stepPrev.textContent = fmtStep(p.prev_step);
  const cur = p.current_step ? fmtStep(p.current_step) : "Protocol complete.";
  els.stepCurrent.textContent = cur;
  els.stepNext.textContent = fmtStep(p.next_step);
  clearClarify();
  announce(cur);
  setState("done", "chip-ok");
}

function clearTimerCards() {
  timers.forEach((el) => el.remove());
  timers.clear();
  if (!els.timers.querySelector(".muted")) {
    const m = document.createElement("div");
    m.className = "muted";
    m.textContent = "No active timers.";
    els.timers.appendChild(m);
  }
}

function removeLogEmptyState() {
  const empty = els.log.querySelector(".log-empty");
  if (empty) empty.remove();
}

function ensureLogEmptyState() {
  if (els.log.querySelector("li:not(.log-empty)")) return;
  if (els.log.querySelector(".log-empty")) return;
  const li = document.createElement("li");
  li.className = "muted log-empty";
  li.textContent = 'No entries yet - say "log: added 200 uL to sample A," or type it below.';
  els.log.appendChild(li);
}

function onLogEntry(p) {
  const li = document.createElement("li");
  removeLogEmptyState();
  li.dataset.logId = String(p.id);
  const sample = p.sample_id ? ` · sample ${p.sample_id}` : "";
  const step = p.step_ref ? ` · step ${p.step_ref}` : "";
  li.innerHTML = `<span class="log-time">${p.timestamp}</span>` +
    `<span class="log-text">${escapeHtml(p.text)}</span>` +
    `<span class="log-meta">${escapeHtml(sample + step)}</span>`;
  els.log.prepend(li);
  clearClarify();
  announce(`Logged: ${p.text}`);
  setState("done", "chip-ok");
}

function onLogRemoved(p) {
  const li = els.log.querySelector(`[data-log-id="${String(p.id)}"]`);
  if (li) li.remove();
  ensureLogEmptyState();
  clearClarify();
  announce("Removed last note");
  setState("done", "chip-ok");
}

function onLogUpdate(p) {
  const li = els.log.querySelector(`[data-log-id="${String(p.id)}"]`);
  const text = li ? li.querySelector(".log-text") : null;
  if (text) text.textContent = p.text;
  clearClarify();
  announce(`Updated note: ${p.text}`);
  setState("done", "chip-ok");
}

function onAskResult(p) {
  els.clarify.innerHTML = `<div class="clarify-msg">${escapeHtml(p.answer)}</div>`;
  els.clarifyPanel.classList.add("active");
  announce(p.answer);
  setState("done", "chip-ok");
}

function onInventory(p) {
  els.inventory.innerHTML =
    `<div class="inv-name">${escapeHtml(p.name)}</div>` +
    `<div class="inv-loc">${escapeHtml(p.location)}</div>` +
    `<div class="inv-qty">${escapeHtml(p.quantity_approx)}</div>`;
  clearClarify();
  announce(`${p.name}: ${p.location}, ${p.quantity_approx}`);
  setState("done", "chip-ok");
}

function onClarify(p) {
  els.clarify.innerHTML = `<div class="clarify-msg">${escapeHtml(p.message)}</div>`;
  els.clarifyPanel.classList.add("active");
  setState("needs input", "chip-warn");
}

function onVoiceState(p) {
  voiceMuted = !!p.muted;
  if (voiceMuted) clearInterimTranscript();
  setMuteUI(!!micStream);
  if (micStream) {
    els.mic.textContent = voiceMuted ? "muted" : "listening";
    els.mic.className = "chip " + (voiceMuted ? "chip-warn" : "chip-ok");
    setState(voiceMuted ? "muted" : "listening", "chip-warn");
  }
  announce(voiceMuted ? "Voice muted" : "Voice listening");
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
  const wasExpired = el.classList.contains("expired");
  el.classList.toggle("expired", !!p.expired);
  el.innerHTML =
    `<div class="timer-label">${escapeHtml(p.label)}</div>` +
    `<div class="timer-clock">${p.expired ? "DONE" : fmtClock(p.remaining_s)}</div>`;
  if (p.expired && !wasExpired) {
    chime();
    showTimerAlert(p.label); // visible + role="alert" announces it
  }
  setState("done", "chip-ok");
}

let alertTimeout = null;
function showTimerAlert(label) {
  if (!els.alertBanner) return;
  els.alertBanner.textContent = `Timer finished: ${label}`;
  els.alertBanner.hidden = false;
  if (alertTimeout) clearTimeout(alertTimeout);
  alertTimeout = setTimeout(() => { els.alertBanner.hidden = true; }, 10000);
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

// --- voice: arm-once mic -> /ws/audio -> Deepgram (server-proxied) ----------
let micStream = null;
let recorder = null;
let audioWS = null;
let manualStop = false;        // distinguish a user Stop from an unexpected drop
let reconnects = 0;
let voiceMuted = false;
const MAX_RECONNECTS = 3;

function pickMime() {
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
  return candidates.find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
}

function setMicUI(active) {
  els.mic.textContent = active ? (voiceMuted ? "muted" : "listening") : "mic off";
  els.mic.className = "chip " + (active ? (voiceMuted ? "chip-warn" : "chip-ok") : "chip-idle");
  els.micBtn.textContent = active ? "Stop session" : "Start session";
  els.micBtn.classList.toggle("active", active);
  els.micBtn.setAttribute("aria-pressed", active ? "true" : "false");
  setMuteUI(active);
}

function setMuteUI(active) {
  if (!els.muteBtn) return;
  els.muteBtn.disabled = !active;
  els.muteBtn.textContent = voiceMuted ? "Unmute" : "Mute";
  els.muteBtn.classList.toggle("active", active && voiceMuted);
  els.muteBtn.setAttribute("aria-pressed", active && voiceMuted ? "true" : "false");
}

async function startMic() {
  manualStop = false;
  reconnects = 0;
  // Feature detection -> a clear, actionable error instead of a silent no-op.
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    onError({ message: "Microphone needs a secure context — open this over https or http://localhost." });
    return;
  }
  if (!window.MediaRecorder) {
    onError({ message: "This browser doesn't support MediaRecorder; try Chrome or Firefox." });
    return;
  }
  const mimeType = pickMime();
  if (!mimeType) {
    onError({ message: "No supported audio codec (webm/opus) in this browser." });
    return;
  }
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch (err) {
    onError({ message: "Microphone permission denied: " + err });
    return;
  }
  openAudioSocket(mimeType);
}

// Each socket gets its OWN MediaRecorder so the webm header is sent fresh on
// (re)connect — a header-mid-stream would make the new Deepgram session deaf.
function openAudioSocket(mimeType) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  audioWS = new WebSocket(`${proto}://${location.host}/ws/audio`);
  audioWS.binaryType = "arraybuffer";
  audioWS.onopen = () => {
    reconnects = 0;
    voiceMuted = false;
    recorder = new MediaRecorder(micStream, mimeType ? { mimeType } : undefined);
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0 && audioWS && audioWS.readyState === WebSocket.OPEN) {
        e.data.arrayBuffer().then((buf) => audioWS.send(buf)).catch(() => {});
      }
    };
    recorder.onerror = () => onError({ message: "Recorder error — restarting may help." });
    recorder.start(250);
    setMicUI(true);
    setState("listening", "chip-warn");
  };
  audioWS.onclose = () => {
    // The recorder is bound to a dead socket; stop it so a reconnect starts clean.
    try { if (recorder && recorder.state !== "inactive") recorder.stop(); } catch (_) {}
    recorder = null;
    if (manualStop) { finalizeStop(); return; }
    if (micStream && reconnects < MAX_RECONNECTS) {
      reconnects += 1;
      els.mic.textContent = "reconnecting";
      els.mic.className = "chip chip-warn";
      setState("reconnecting", "chip-warn");
      setTimeout(() => { if (!manualStop && micStream) openAudioSocket(mimeType); }, 500 * reconnects);
    } else {
      onError({ message: "Voice connection lost. Click Start session to retry." });
      finalizeStop();
    }
  };
  audioWS.onerror = () => { /* onclose fires next and handles recovery */ };
}

function stopMic() {
  manualStop = true;
  try { if (recorder && recorder.state !== "inactive") recorder.stop(); } catch (_) {}
  try {
    if (audioWS && audioWS.readyState === WebSocket.OPEN) {
      audioWS.send(JSON.stringify({ type: "stop" }));
      audioWS.close();
    }
  } catch (_) {}
  if (!audioWS || audioWS.readyState === WebSocket.CLOSED) finalizeStop();
}

function finalizeStop() {
  try { if (micStream) micStream.getTracks().forEach((t) => t.stop()); } catch (_) {}
  recorder = null;
  micStream = null;
  audioWS = null;
  reconnects = 0;
  voiceMuted = false;
  clearInterimTranscript();
  setMicUI(false);
  setState("idle", "");
}

els.micBtn.addEventListener("click", () => {
  if (micStream) stopMic();
  else startMic();
});

function sendMuteControl(muted) {
  if (!audioWS || audioWS.readyState !== WebSocket.OPEN) return;
  audioWS.send(JSON.stringify({ type: "set_muted", muted }));
}

if (els.muteBtn) {
  els.muteBtn.addEventListener("click", () => {
    if (!micStream) return;
    sendMuteControl(!voiceMuted);
  });
}

// --- hydrate from persisted/in-memory state on (re)load --------------------
async function hydrate() {
  try {
    const r = await fetch("/api/state");
    const s = await r.json();
    (s.log || []).forEach((row) => onLogEntry(row)); // ascending id -> newest on top
    if (s.step) onStepChange(s.step);
    (s.timers || []).forEach((t) => onTimerUpdate(t));
    setState("idle", "");
  } catch (_) { /* fresh start */ }
}
populateDemoLines();
hydrate();

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
