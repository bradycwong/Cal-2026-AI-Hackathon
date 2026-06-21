# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A voice-driven Electronic Lab Notebook ("Lab" / BenchPilot). A researcher speaks or
types a command — load a protocol, log an observation, start a timer, find/add a
reagent, ask what's next — and the screen updates live over a WebSocket. FastAPI
backend, vanilla-JS multi-page frontend, SQLite for log persistence.

## Commands

```bash
# Setup (Python 3.12+; deps are pinned for a reproducible build, incl. py3.14 wheels)
python -m venv .venv && source .venv/Scripts/activate   # Windows; use bin/activate on POSIX
pip install -r requirements.txt
cp .env.example .env          # optional — typed demo works with NO keys

# Run (serves the UI + API on http://127.0.0.1:8000)
uvicorn backend.main:app --reload

# Test
pytest -q                                  # full suite
pytest tests/test_router.py -q             # one file
pytest tests/test_router.py::test_name -q  # one test
```

There is no configured linter/formatter — do not invent one.

## The locked spine (do not violate)

The whole product hangs off one data-flow invariant. Read `backend/schema.py` and
the `ingest()` function in `backend/main.py` before changing anything here.

```
transcript (str)
  -> route(transcript)         backend/router.py — the ONLY place an LLM may run
  -> handle_command(Command)   backend/handlers.py — deterministic, mutates SessionState
  -> broadcast(events)         WS /ws/events — 4 outer event types, frozen
```

Locked invariants:
- **One `Command` shape** (`schema.py`): flat intent enum + all-Optional payload
  fields. A clear-but-incomplete utterance ("load a protocol" with no name) is a
  *valid* Command, not a crash — the field stays null and a question goes in
  `clarify_prompt`. The router never guesses a value that wasn't said.
- **4 outer WS event types** (`transcript_update`, `command_result`, `timer_update`,
  `error`). New behavior adds a `command_result` **`kind`** (built via the
  `*_event()` helpers in `schema.py`) — never a new outer type. The frontend
  (`FrontendTest/app.js`) dispatches on these 4 types and on `payload.kind`.
- **Input channels are swappable; the spine is not.** The typed box POSTs
  `/api/ingest`; Deepgram final transcripts call the *same* `ingest()` over
  `/ws/audio`. Voice is just another way to fill the transcript.
- **`router.py` is the only LLM caller.** Everything downstream is deterministic.
  Swapping LLM providers touches only that file.

## Critical gotchas

- **The LLM router path is currently dead-by-design.** `_llm_route()` calls
  `client.messages.parse(...)`, which **does not exist** in the pinned
  `anthropic==0.40.0`. So `route()` always throws there and falls back to
  `deterministic_route()` — the regex parser in `router.py` is the live path. Do
  not "fix" this by calling `messages.parse`; if you need an LLM call on the pinned
  SDK use `messages.create` + manual JSON validation (this is how
  `protocol_import.py` and `answer_question()` already do it). The deterministic
  fallback is intentional ("API weirdness must not kill the pitch") — every LLM
  touchpoint has a network-free fallback so the demo runs with zero keys.
- **`FrontendTest/` is the live served UI**, not `frontend/`. `backend/main.py`
  serves `FrontendTest/` at `/` and mounts it statically. `frontend/` is legacy,
  kept only because its service worker is still served at `/sw.js`. Edit
  `FrontendTest/`.
- **`backend/seed/` is the golden image; `backend/data/` is the working copy.**
  `POST /api/demo/reset` calls `state.restore_factory_state()`, which wipes notes +
  notebooks and restores inventory/protocols **from `seed/`**. Anything that must
  survive a demo reset has to be added to `backend/seed/`, not just `backend/data/`.
- **Arize tracing no-ops** unless both `ARIZE_SPACE_ID` and `ARIZE_API_KEY` are set,
  and is always off under pytest. Spans are emitted **manually** (`chain_span` /
  `llm_span` in `backend/instrumentation.py`) because the Anthropic
  auto-instrumentor requires `anthropic>=0.41`, which conflicts with the 0.40.0 pin.
- **ASCII normalization**: `normalize_ascii()` in `router.py` collapses unit
  spellings (`µL`/`microliters` → `uL`, `°C` → `degrees C`) before any
  matching/asserting. Machine-read text uses this; only display text keeps pretty
  symbols. Tests and regex assume normalized input.

## Backend layout (`backend/`)

- `main.py` — FastAPI app: REST (`/api/ingest`, `/api/state`, `/api/protocols*`,
  `/api/inventory*`, `/api/log*`, `/api/notebooks*`, `/api/scale`, `/api/step/next`,
  `/api/demo/reset`, `/api/timers/*`), WS `/ws/events` (output bus) + `/ws/audio`,
  the `ingest()` spine, the 1 Hz timer loop, and static UI serving.
- `schema.py` — `Command` + the locked event-envelope builders (the boundary).
- `router.py` — `route()`: LLM primary (dead on the pin) + deterministic fallback;
  `answer_question()` for the `ask` intent; `normalize_ascii()`.
- `handlers.py` — `handle_command()` deterministic dispatch; missing param → clarify.
- `state.py` — `SessionState`: YAML/CSV loaders, step cursor, timers, log, notebooks,
  inventory mutations, `restore_factory_state()`. `ProtocolParseError` on bad YAML.
- `db.py` — SQLite `NoteStore` (the one stateful organ). Override with `LAB_DB_PATH`;
  `:memory:` disables on-disk persistence.
- `protocol_import.py` — prose → canonical protocol YAML (`POST /api/protocols/import`),
  `messages.create` + JSON validation when a key is set, deterministic otherwise.
- `scaling.py` — `build_prep_table()`: deterministic reagent scaling (read-only, no
  LLM, no state mutation). Inventory matching here is stricter than voice
  `find_inventory`.
- `reproducibility.py` — `check()`: flags logged-vs-expected `volume_ul` mismatches.
- `deepgram_stt.py` — server-side Deepgram live STT proxy (key never reaches browser).
- `voice_control.py` — process-wide mute/unmute gate shared by typed box, spoken
  word, and Mute button. Mute is a **command gate, not mic-off**: the mic keeps
  listening while muted and only watches for "unmute"; it is sticky across reconnects.
- `instrumentation.py` — Arize span helpers (no-op without creds / under pytest).
- `env.py` — `.env` loading.
- `data/` (working) and `seed/` (golden) — `protocols/*.yaml` + `inventory.csv`.
  `lab.db` is created at runtime. Override the working dir with `LAB_DATA_DIR`.

Protocols are file-driven: drop a YAML in `data/protocols/` and it is auto-loaded and
selectable by name — no code change.

## Frontend (`FrontendTest/`)

Multi-page vanilla JS (`dashboard`/`protocols`/`guide`/`notebook`/`inventory`/`commands`.html,
each with a matching `.css`, plus `shared.css` and Tailwind via CDN config). One
shared `app.js` exposes `window.LabClient`: it hydrates from REST (`/api/state` etc.)
on load, then stays in sync over `/ws/events`. **Pages opt in by including the
matching container ids** (`#protocol-cards`, `#inventory-rows`, `#log-rows`,
`#step-tracker`, `#step-current`, `#timer-list`, `#live-transcript`); a page without a
hook simply skips that renderer. `voice.js` drives the mic dock.

## Test conventions

- No `tests/conftest.py`. Tests isolate state by replacing `main.state` with a fresh
  `SessionState(db_path=":memory:", data_dir=<scratch copy of shipped data>)` inside a
  `client` fixture (see `tests/test_api.py`), so imports/adds never mutate repo files.
- Force the offline parsers with env in-test: `ROUTER_MODE=deterministic`,
  `IMPORT_MODE=deterministic` (`monkeypatch.setenv`).
- The router test harness asserts on `normalize_ascii`-normalized transcripts.

## Repo conventions

- Implementation plans are kept as **visible `*.txt` files in the repo root**
  (e.g. `reproducibility_plan.txt`, `plan_demo_reset.txt`), not hidden.
- `.env` holds **server-side keys only** — never shipped to the browser. The typed
  demo works with no keys. Env knobs: `ROUTER_MODE`/`IMPORT_MODE` (`auto|llm|deterministic`),
  `LAB_MODEL`, `LAB_DB_PATH`, `LAB_DATA_DIR`, `LAB_DEMO_MODE`, `DEEPGRAM_*`, `ARIZE_*`.
