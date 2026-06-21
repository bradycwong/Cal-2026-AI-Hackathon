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

**Wake word.** Voice is gated by "Hey Lab" so background talk doesn't fire
commands: say `"Hey Lab, what's next?"`, or just `"Hey Lab"` then your command
within ~8 s. The typed box never needs a wake word. Disable with
`LAB_WAKE_REQUIRED=false`.

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

Type each into the command box at the bottom:

1. `Load DNA extraction protocol.`        → BIG current-step panel shows Step 1
2. `Log: added 200 microliters lysis buffer to sample A.` → structured log row
3. `Start a 10-minute incubation timer.`  → countdown card, chime on expiry
4. `Where's the proteinase K?`            → inventory card (Freezer 2, shelf B)
5. `What's next?`                         → step panel advances

Clarification path (never fails silently): `Load a protocol.` → clarification
area asks "Which protocol?" (listing the loaded protocols) instead of guessing.

**Auto-timers.** Steps that declare a `duration_s` start their countdown
automatically when they become the current step (on load or "what's next"),
labelled from the step's `timer_label`. You can still start ad-hoc timers by
voice/typing. Disable with `LAB_AUTO_TIMERS=false`.

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
  wake.py        "Hey Lab" wake gate: which spoken utterances become commands
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
tests/           test_router.py, test_handlers.py, test_wake.py, test_persistence.py
```

Log persistence: the log feed is written to SQLite (`backend/data/lab.db`, set
`LAB_DB_PATH` to override or `:memory:` to disable) and rehydrated by the UI from
`GET /api/state` on load, so it survives a page refresh or a server restart.
Protocols + inventory stay file-driven; the rest of session state is in-memory.

Deferred swappable organs (NOT yet): VAD-gated streaming (cost), TTS,
open-ended Q&A, upload/library pages.
