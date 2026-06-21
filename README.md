# Lab — Voice-Driven Electronic Lab Notebook

A hands-free lab assistant: speak (or type) a command — load a protocol, log an
observation, start a timer, find a reagent, ask what's next — and the screen
updates in real time. **Voice is now live** (Deepgram nova-2, server-proxied);
persistence (SQLite) and the rest remain swappable organs added without touching
the spine.

## The spine (locked)

```
transcript (string)
  -> route(transcript)        ONE validated Command   (router.py — the only LLM call)
  -> handle_command(Command)  deterministic; mutates SessionState (handlers.py)
  -> emit UI events           over WS /ws/events       (4 outer types, locked)
```

Input channels are swappable; the spine is not. The typed box POSTs to
`/api/ingest`; live Deepgram final transcripts call the **same** `ingest()` over
`/ws/audio`. Voice is just another way to fill the transcript.

## Voice

Click **Start session** (top-right), allow the mic, and speak. Mic audio streams
as webm/opus to `/ws/audio`; the server proxies it to Deepgram nova-2 (key stays
server-side), shows interim words live, and routes finished utterances through the
same spine. Requires `DEEPGRAM_API_KEY` in `.env`.

**Mute/unmute.** After Start, every finished utterance is routed as a command.
Say `mute`, type `mute` in the command box, or click **Mute** to stop transcript
updates and command routing — all three toggle the same gate. While muted the mic
**keeps listening** but ignores everything except `unmute`; mute is sticky and
survives reconnects, so it stays muted until you say/type `unmute` (or click
**Unmute**) to resume. Other typed commands always work.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # optional — typed demo works with NO keys
uvicorn backend.main:app --reload
# open http://127.0.0.1:8000
```

No API key? The router automatically uses a **deterministic fallback** that
covers the five demo lines, so the typed demo always works. Set
`ANTHROPIC_API_KEY` (and keep `ROUTER_MODE=auto`) to route via Claude.

## The 5 typed demo lines

Type these into the command box at the bottom:

1. `Load DNA extraction protocol.` -> BIG current-step panel shows Step 1
2. `What's next?` -> step panel advances
3. `Go back.` -> step panel returns to the previous step without starting a duplicate auto-timer
4. `Repeat that.` -> current step is re-announced without moving the cursor
5. `Log: added 200 microliters lysis buffer to sample A.` -> structured log row
6. `Scratch that.` -> last log row is removed
7. `Log: added 250 uL lysis buffer.` then `Change that to added 300 uL lysis buffer.` -> last row updates in place
8. `Start a 10-minute incubation timer.` -> countdown card, chime on expiry
9. `Where's the proteinase K?` -> inventory card
10. `How much lysis buffer in step 1?` -> protocol answer in the clarification panel

Clarification path (never fails silently): `Load a protocol.` → clarification
area asks "Which protocol?" (listing the loaded protocols) instead of guessing.

**Manual timers.** When you reach a step that declares a `duration_s`, a
**paused** timer card appears (frozen at the step's full duration, labelled from
its `timer_label`) instead of counting down. It never auto-starts on step
change — say (or type) **"start timer"** to begin the countdown. You can still
start ad-hoc timers with a spoken duration ("start a 10-minute timer").

**Protocols.** Four ship in `backend/data/protocols/` (DNA Extraction, PCR Setup,
Bacterial Transformation, Plasmid Miniprep). Add another by dropping a YAML file
in that directory — no code change; it's auto-loaded and selectable by name.

## Test

```bash
pytest -q          # router harness + handler shape checks
```

## Layout

```
backend/
  main.py        FastAPI: /api/ingest, /api/state, WS /ws/events + /ws/audio, static, timer loop, ingest() spine
  deepgram_stt.py server-side Deepgram live STT proxy (key never reaches browser)
  voice_control.py always-listening mute/unmute gate for spoken utterances
  schema.py      Command (flat-5 + unknown) + locked event-envelope builders
  router.py      route(transcript)->Command: LLM primary + deterministic fallback; ASCII normalize
  handlers.py    handle_command(): deterministic dispatch; missing param -> clarify
  state.py       SessionState; YAML/CSV loaders; timers; log (DB-backed via db.py)
  db.py          SQLite NoteStore: persists the log so it survives refresh/restart
  data/protocols/*.yaml (DNA Extraction, PCR Setup, Bacterial Transformation, Plasmid Miniprep), data/inventory.csv  (lab.db created at runtime)
frontend/
  index.html     panels + typed command box (permanent fallback)
  app.js         WS client; dispatch on the 4 event types
  styles.css
tests/           router, handler, STT, voice-control, and persistence checks
```

Log persistence: the log feed is written to SQLite (`backend/data/lab.db`, set
`LAB_DB_PATH` to override or `:memory:` to disable) and rehydrated by the UI from
`GET /api/state` on load, so it survives a page refresh or a server restart.
Protocols + inventory stay file-driven; the rest of session state is in-memory.

Deferred swappable organs (NOT yet): VAD-gated streaming (cost), TTS,
upload/library pages.
