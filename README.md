# Otto — Voice-Driven Electronic Lab Notebook

A hands-free lab assistant: speak (or type) a command — load a protocol, log an
observation, start a timer, find a reagent, ask what's next — and the screen
updates in real time. **This pass is the typed-first skeleton** (Plan 3): the
full pipeline minus the microphone. Voice (Deepgram) and persistence (SQLite)
are swappable organs added later without touching the spine.

## The spine (locked)

```
transcript (string)
  -> route(transcript)        ONE validated Command   (router.py — the only LLM call)
  -> handle_command(Command)  deterministic; mutates SessionState (handlers.py)
  -> emit UI events           over WS /ws/events       (4 outer types, locked)
```

Input channels are swappable; the spine is not. The typed box POSTs to
`/api/ingest`; live Deepgram `is_final` will call the **same** `ingest()` later.

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
area asks "Which protocol?" instead of guessing.

## Test

```bash
pytest -q          # router harness + handler shape checks
```

## Layout

```
backend/
  main.py        FastAPI: /api/ingest, WS /ws/events, static, timer loop, ingest() spine
  schema.py      Command (flat-5 + unknown) + locked event-envelope builders
  router.py      route(transcript)->Command: LLM primary + deterministic fallback; ASCII normalize
  handlers.py    handle_command(): deterministic dispatch; missing param -> clarify
  state.py       SessionState; YAML/CSV loaders; in-memory log + timers
  data/protocols/dna_extraction.yaml, data/inventory.csv
frontend/
  index.html     panels + typed command box (permanent fallback)
  app.js         WS client; dispatch on the 4 event types
  styles.css
tests/           test_router.py, test_handlers.py
```

Deferred swappable organs (NOT in this pass): live Deepgram STT/VAD/wake word,
SQLite persistence, custom vocabulary, 2nd protocol, auto-timers, TTS,
open-ended Q&A, upload/library pages.
