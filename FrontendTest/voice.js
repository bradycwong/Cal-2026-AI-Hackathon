/* FrontendTest voice subsystem — arm-once mic -> /ws/audio -> Deepgram
 * (server-proxied). Split out of app.js, which keeps the api/render/ws/modals
 * client. This file owns getUserMedia / MediaRecorder / the audio socket and the
 * voice-state sessionStorage; nothing here touches app.js internals.
 *
 * Contract with app.js (both are plain <script defer>; every cross-call runs
 * after DOMContentLoaded, so load order is irrelevant):
 *   app.js calls   -> LabVoice.{wireVoice, maybeResumeVoice, onVoiceState, onError, startMic, stopMic}
 *   voice.js calls -> a stop hook app.js registers via LabVoice.setStopHook(),
 *                     so stopping the mic also clears the in-progress transcript
 *                     line without voice.js reaching into app.js's transcript DOM.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // app.js registers clearInterim here (one-way: voice.js never imports app.js).
  let stopHook = null;
  function setStopHook(fn) {
    stopHook = fn;
  }

  // --- voice session persistence across page navigations --------------------
  // This is a multi-page app: every nav is a full reload that tears down the
  // mic/recorder/socket. To make the voice assistant feel like it "stays on"
  // (or muted, or off) as the user moves between pages, we persist the user's
  // INTENT in sessionStorage and re-arm the mic on the next load. The server
  // keeps the mute gate sticky and re-broadcasts it on every /ws/audio connect,
  // so muted is authoritative server-side; we only mirror it locally for a
  // flicker-free UI before the socket syncs.
  const VOICE_ACTIVE_KEY = "lab.voice.active";
  const VOICE_MUTED_KEY = "lab.voice.muted";
  function storageGet(key) {
    try {
      return window.sessionStorage.getItem(key);
    } catch (_) {
      return null;
    }
  }
  function storageSet(key, val) {
    try {
      window.sessionStorage.setItem(key, val);
    } catch (_) {}
  }
  const setVoiceActiveStored = (on) => storageSet(VOICE_ACTIVE_KEY, on ? "1" : "0");
  const getVoiceActiveStored = () => storageGet(VOICE_ACTIVE_KEY) === "1";
  const setVoiceMutedStored = (m) => storageSet(VOICE_MUTED_KEY, m ? "1" : "0");
  const getVoiceMutedStored = () => storageGet(VOICE_MUTED_KEY) === "1";

  // --- voice: arm-once mic -> /ws/audio -> Deepgram (server-proxied) --------
  // Ported from the original frontend so every served page can feed Deepgram.
  let micStream = null;
  let recorder = null;
  let audioWS = null;
  let manualStop = false;
  let reconnects = 0;
  let voiceMuted = getVoiceMutedStored();
  let voiceErrorMsg = null; // sticky "off" reason (e.g. STT unavailable); see onVoiceUnavailable
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
    if (!active) setVoiceStatus(voiceErrorMsg || "Voice off", false);
    else setVoiceStatus(voiceMuted ? "Muted" : "Listening", !voiceMuted);
  }

  function onVoiceState(p) {
    voiceMuted = !!p.muted;
    setVoiceMutedStored(voiceMuted); // mirror so the next page shows it instantly
    if (micStream) setVoiceUI(true);
  }

  // A server-side STT failure (no DEEPGRAM_API_KEY, or a Deepgram auth/transport
  // error) closes the audio socket the instant it opens. Without this, onclose
  // would just flicker Listening -> Reconnecting -> retry forever. Stop the
  // futile loop and tell the user *why* instead.
  function onVoiceUnavailable(p) {
    manualStop = true; // make any pending onclose finalize instead of reconnecting
    setVoiceActiveStored(false); // don't auto-resume this on the next page
    const msg = (p && p.message) || "";
    // Sticky so the trailing onclose/finalizeStop renders the reason, not "Voice off".
    voiceErrorMsg = /key|unauthor|forbidden|401|403/i.test(msg)
      ? "Voice unavailable — STT key not configured"
      : "Voice unavailable";
    try {
      if (audioWS) audioWS.close();
    } catch (_) {}
    finalizeStop(); // resets state + renders voiceErrorMsg via setVoiceUI(false)
  }

  function onError(p) {
    // Only Deepgram/STT transport errors touch the voice session, and only when
    // THIS page actually has a session running (the error is broadcast to every
    // open client). Other error sources stay silent no-ops, as before.
    if (p && (p.source === "deepgram" || p.code === "stt_failed") && micStream) {
      onVoiceUnavailable(p);
    }
  }

  async function startMic() {
    manualStop = false;
    reconnects = 0;
    voiceErrorMsg = null; // clear any prior fatal reason on a fresh start/retry
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setVoiceActiveStored(false); // can't run here; don't keep retrying on nav
      setVoiceStatus("Needs https/localhost", false);
      return;
    }
    if (!window.MediaRecorder) {
      setVoiceActiveStored(false);
      setVoiceStatus("Unsupported browser", false);
      return;
    }
    const mimeType = pickMime();
    if (!mimeType) {
      setVoiceActiveStored(false);
      setVoiceStatus("No audio codec", false);
      return;
    }
    setVoiceStatus("Connecting", false);
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
    } catch (err) {
      setVoiceActiveStored(false); // permission gone; stop auto-resuming
      setVoiceStatus("Mic denied", false);
      return;
    }
    // Mic is live — remember the intent so navigating to another page re-arms it.
    setVoiceActiveStored(true);
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
    setVoiceActiveStored(false); // explicit user stop: stay off across pages
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
    setVoiceMutedStored(false);
    if (stopHook) stopHook(); // app.js's clearInterim: drop the in-progress transcript line
    setVoiceUI(false);
  }

  function sendMuteControl(muted) {
    if (!audioWS || audioWS.readyState !== WebSocket.OPEN) return;
    audioWS.send(JSON.stringify({ type: "set_muted", muted }));
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

  // Re-arm the mic after a navigation if the user had voice on when they left
  // the previous page. The mute gate is restored by the server's voice_state
  // broadcast on the fresh /ws/audio connect, so a muted session stays muted.
  function maybeResumeVoice() {
    if (!micStream && getVoiceActiveStored()) startMic();
  }

  window.LabVoice = {
    wireVoice,
    maybeResumeVoice,
    onVoiceState,
    onError,
    startMic,
    stopMic,
    setStopHook,
  };
})();
