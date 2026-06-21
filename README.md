# Lab — Voice-Driven Electronic Lab Notebook

A hands-free lab assistant: speak (or type) a command — load a protocol, log an
observation, start a timer, find a reagent, ask what's next — and the screen
updates in real time. **Voice is now live** (Deepgram nova-3, server-proxied);
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
as webm/opus to `/ws/audio`; the server proxies it to Deepgram nova-3 (key stays
server-side), shows interim words live, and routes finished utterances through the
same spine. Requires `DEEPGRAM_API_KEY` in `.env`.

**Mute/unmute.** After Start, every finished utterance is routed as a command.
Say or type `mute` (the word `mute` **anywhere** in an utterance mutes, e.g.
"okay, mute"), or click **Mute**, to stop transcript updates and command routing
— all three toggle the same gate. While muted the mic
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

## Demo

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
start ad-hoc timers with a spoken duration ("start a 10-minute timer"). When a
timer ends it **beeps** (for up to 30s). Say or type **"stop timer"**, or click
the **×** on a timer card, to silence the alarm and/or cancel a running timer
early.

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
  main.py        FastAPI: /api/ingest, /api/state, /api/protocols(+/{id}/load), /api/inventory, /api/log, WS /ws/events + /ws/audio, serves FrontendTest, timer loop, ingest() spine
  deepgram_stt.py server-side Deepgram live STT proxy (key never reaches browser)
  voice_control.py always-listening mute/unmute gate for spoken utterances
  schema.py      Command (flat-5 + unknown) + locked event-envelope builders
  router.py      route(transcript)->Command: LLM primary + deterministic fallback; ASCII normalize
  handlers.py    handle_command(): deterministic dispatch; missing param -> clarify
  state.py       SessionState; YAML/CSV loaders; timers; log (DB-backed via db.py)
  db.py          SQLite NoteStore: persists the log so it survives refresh/restart
  data/protocols/*.yaml (DNA Extraction, PCR Setup, Bacterial Transformation, Plasmid Miniprep), data/inventory.csv  (lab.db created at runtime)
FrontendTest/     served live UI (dashboard/protocols/guide/notebook/inventory.html)
  app.js         window.LabClient: REST hydrate + WS dispatch on the 4 event types
frontend/         legacy app, kept on disk (sw.js still served at /sw.js); no longer the root
tests/           router, handler, STT, voice-control, and persistence checks
```

Log persistence: the log feed is written to SQLite (`backend/data/lab.db`, set
`LAB_DB_PATH` to override or `:memory:` to disable) and rehydrated by the UI from
`GET /api/state` on load, so it survives a page refresh or a server restart.
Protocols + inventory stay file-driven; the rest of session state is in-memory.

Read snapshots for the UI: `GET /api/protocols` -> `{"protocols": [...]}`,
`POST /api/protocols/{id}/load` (deterministic load), `GET /api/inventory` ->
`{"items": [...]}`, `GET /api/log` -> `{"log": [...]}`, and `GET /api/state`
(step enriched with `all_steps`, `current_index`, `protocol_name`).

Protocol import: `POST /api/protocols/import` with `{"text": "...", "name": "..."}`
turns pasted prose into the canonical protocol YAML (written to
`backend/data/protocols/`), registers it immediately, and surfaces it in
`GET /api/protocols`. `IMPORT_MODE` (`auto|llm|deterministic`) selects the parser;
with no API key it falls back to a deterministic line/duration parser, so import
works offline. Imported protocols are always `READY` and survive a restart. The
FrontendTest protocols page has an "Import Protocol" modal wired to this endpoint.

Reagent prep scaling: `POST /api/scale` computes a deterministic prep table for
the active protocol or a provided `protocol_id`. It reads structured protocol
parameters (`reagent`, `volume_ul`), scales totals by sample count and overage,
and compares those totals against inventory without calling the LLM or mutating
lab state. Inventory matching here is stricter than the voice `find_inventory`
command so generic shared words (e.g. "buffer") don't pair unrelated reagents. In
the dashboard, load DNA Extraction, enter `12` samples and `10` percent overage,
then compute: ethanol and nuclease-free water show in stock, while lysis buffer
shows missing (no close inventory match).

Reproducibility flags: when a log entry is added at a step that declares a
`volume_ul` parameter, a deterministic checker compares the logged volume against
the expected one and attaches an optional `flag` (`status: ok|mismatch`) to the
`log_entry`/`log_update` payloads and to `/api/log` + `/api/state`. v1 flags only
`volume_ul` mismatches; it never edits the researcher's log text, and the flag is
persisted as part of the lab record. The notebook renders a `OK`/`Warning` line.

## Demo reset

The FrontendTest UI includes a Reset Demo control. It clears protocol progress,
timers, transcript, clarification, and transient page state. Persisted notes are
kept by default. Set `LAB_DEMO_MODE=true` before starting the server to wipe notes
as part of reset. Reset never deletes protocol YAML files or inventory CSV data.

Deferred swappable organs (NOT yet): VAD-gated streaming (cost), TTS,
upload/library pages.
