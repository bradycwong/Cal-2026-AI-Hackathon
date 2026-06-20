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
  mic: $("chip-mic"),
  wakeInput: $("wake-input"),
  transcriptHint: document.querySelector(".panel-hint"),
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

let interimEl = null;
function onTranscript(p) {
  if (p.is_final) {
    if (interimEl) {
      interimEl.remove();
      interimEl = null;
    }
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

// --- voice: arm-once mic -> /ws/audio -> Deepgram (server-proxied) ----------
let micStream = null;
let recorder = null;
let audioWS = null;

function pickMime() {
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
  return candidates.find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
}

async function startMic() {
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch (err) {
    onError({ message: "Microphone permission denied: " + err });
    return;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  audioWS = new WebSocket(`${proto}://${location.host}/ws/audio`);
  audioWS.binaryType = "arraybuffer";
  audioWS.onopen = () => {
    const mimeType = pickMime();
    recorder = new MediaRecorder(micStream, mimeType ? { mimeType } : undefined);
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0 && audioWS && audioWS.readyState === WebSocket.OPEN) {
        e.data.arrayBuffer().then((buf) => audioWS.send(buf));
      }
    };
    recorder.start(250); // one MediaRecorder for the session => header-safe stream
    els.mic.textContent = "listening";
    els.mic.className = "chip chip-ok";
    els.micBtn.textContent = "Stop session";
    els.micBtn.classList.add("active");
    setState("listening", "chip-warn");
  };
  audioWS.onclose = () => stopMic();
  audioWS.onerror = () => onError({ message: "Audio socket error" });
}

function stopMic() {
  try { if (recorder && recorder.state !== "inactive") recorder.stop(); } catch (_) {}
  try { if (micStream) micStream.getTracks().forEach((t) => t.stop()); } catch (_) {}
  try {
    if (audioWS && audioWS.readyState === WebSocket.OPEN) {
      audioWS.send(JSON.stringify({ type: "stop" }));
      audioWS.close();
    }
  } catch (_) {}
  recorder = null;
  micStream = null;
  audioWS = null;
  els.mic.textContent = "mic off";
  els.mic.className = "chip chip-idle";
  els.micBtn.textContent = "Start session";
  els.micBtn.classList.remove("active");
  setState("idle", "");
}

els.micBtn.addEventListener("click", () => {
  if (micStream) stopMic();
  else startMic();
});

// --- runtime-settable wake word --------------------------------------------
function applyWake(word) {
  const w = (word || "otto").trim();
  if (els.wakeInput && document.activeElement !== els.wakeInput) els.wakeInput.value = w;
  if (els.transcriptHint) els.transcriptHint.textContent = `say "Hey ${w}, …"`;
}

async function loadWake() {
  try {
    const r = await fetch("/api/config");
    const d = await r.json();
    applyWake(d.wake && d.wake.word);
  } catch (_) { /* defaults stay */ }
}

async function saveWake() {
  const word = (els.wakeInput.value || "").trim();
  if (!word) return loadWake();
  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ word }),
    });
    const d = await r.json();
    applyWake(d.wake && d.wake.word);
  } catch (err) {
    onError({ message: "Could not update wake word: " + err });
  }
}

if (els.wakeInput) {
  els.wakeInput.addEventListener("change", saveWake);
  els.wakeInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); els.wakeInput.blur(); }
  });
}
loadWake();

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
