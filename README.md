# Lab - Voice-Driven Electronic Lab Notebook

Lab is a hands-free electronic lab notebook for running protocols in the lab:
start a voice session, load a protocol, follow the active step, log observations,
manage timers, check inventory, scale reagent prep, and keep notebook records in
sync across the UI.

## What works now

- **Live app:** FastAPI serves the current UI from `FrontendTest/` at
  `http://127.0.0.1:8000`.
- **Voice control:** browser mic audio streams to `/ws/audio`, the backend proxies
  it to Deepgram nova-3, and final transcripts run through the same command spine.
  The Deepgram key stays server-side in `.env`.
- **Always-listening mute:** after a voice session starts, the mic stays connected.
  `mute` stops transcript updates and command routing; `unmute` resumes. The mute
  state is sticky across reconnects.
- **Protocol library:** four protocols ship in `backend/data/protocols/`. The UI
  can load, import from pasted text, import from PDF, edit, and delete protocols.
- **Guided run:** the Guide page shows the active step, previous/current/next
  context, skipped/completed status, active timers, and a reagent-prep table.
- **Notebooks:** logs are SQLite-backed, scoped to the active notebook, searchable,
  editable, sortable, and exportable as Markdown, CSV, or print-to-PDF.
- **Inventory:** inventory is CSV-backed and can be searched, added, edited, and
  deleted from the UI. Voice can find items and add simple inventory entries.
- **Factory reset:** Reset Demo restores the shipped protocol and inventory seed
  data, clears run state, wipes notes/notebooks, clears timers, and unmutes voice.

## The spine

The central contract is still intentionally small:

```text
spoken transcript
  -> route(transcript)        one validated Command
  -> handle_command(Command)  deterministic state mutation
  -> emit UI events           over /ws/events
```

The frontend dispatches on four outer event types:
`transcript_update`, `command_result`, `timer_update`, and `error`. New behavior
adds `command_result.kind` values instead of adding new outer event types.

Visible UI controls usually use structured REST endpoints directly. Spoken
commands enter through `/ws/audio` and then the same `ingest()` spine used by the
backend tests and command-driven buttons.

## Run

PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload
```

Open `http://127.0.0.1:8000`.

Voice requires `DEEPGRAM_API_KEY` in `.env`. `ANTHROPIC_API_KEY` is optional:
with no Anthropic key, the router uses deterministic fallback paths for the demo
commands. The current `FrontendTest/` UI does not expose a free-form typed command
box, so the demo is voice plus visible page controls.

## Updated demo

1. Click **Reset Demo** to start from the factory seed state.
2. Click **Start voice session** in the lower-right dock and allow microphone
   access.
3. Say `Load DNA extraction protocol`, or open **Protocols** and click the DNA
   Extraction load button. The app lands on the Guide with step 1 active.
4. In the reagent-prep modal, set the sample count to `12` and click **Compute**.
   The table shows scaled reagent totals and inventory status.
5. On the Guide, say `Next step` or click **Confirm Action**. The current step is
   marked complete and an automatic notebook entry is written.
6. Try step controls: say `Go back`, `Repeat that`, or `Skip this step`, or use
   the matching Guide buttons. Skipped steps stay visible in the tracker.
7. When a timed protocol step appears, the timer card starts paused. Say
   `Start timer` to begin it, then say `Stop timer` or delete the timer card to
   clear it.
8. Say `Log added 200 uL lysis buffer to sample A`. Open **Notebook** to see the
   entry, provenance badge, timestamp, and any reproducibility warning.
9. Say `Scratch that`, or say `Correct that to added 300 uL lysis buffer`. You can
   also add and edit entries with the Notebook page controls.
10. On **Notebook**, create a second notebook, switch it active, and confirm
    another protocol step. New step notes land in the selected notebook.
11. Say `Where's the proteinase K?`, or search from **Inventory**. Add a new
    inventory item from the page, then edit or delete it.
12. On **Protocols**, click **Import Protocol**. Paste numbered steps or drop a
    text-readable PDF; imported protocols are registered immediately and can be
    loaded like shipped protocols.
13. Edit a protocol from its card, then click **Reset Demo**. The reset restores
    the shipped protocol/inventory seed data and removes the demo edits, imports,
    notes, notebooks, timers, and active run.

Clarification behavior is deliberate: if you say `Load a protocol` without naming
one, the UI asks which protocol instead of guessing.

## Voice examples

```text
Load DNA extraction protocol
Next step
Skip this step
Go back
Repeat that
Start timer
Stop timer
Clear done timers
Log sample A looks clear
Scratch that
Correct that to sample A looks cloudy
Where is the EDTA?
Add 5 g of EDTA on shelf 4 to inventory
How much lysis buffer in step 1?
Open notebook
Show inventory
Jump to guide
Mute
Unmute
```

The **Commands** page lists the supported voice phrases and what each one does.

## Data and reset model

- Protocols live in `backend/data/protocols/*.yaml`.
- Inventory lives in `backend/data/inventory.csv`.
- Seed copies live under `backend/seed/` and are used by Reset Demo.
- Notes and notebooks persist in SQLite at `backend/data/lab.db` by default.
- Override data with `LAB_DATA_DIR`; override notes with `LAB_DB_PATH`, or set
  `LAB_DB_PATH=:memory:` for non-persistent notes.

Reset Demo is now a full factory reset. It restores seed protocols and inventory,
clears all notes/notebooks, clears the active protocol and timers, clears recent
protocol state, and unmutes voice. It does not depend on `LAB_DEMO_MODE`.

## API surface

- `POST /api/ingest` - command spine entrypoint for tests and command-like UI
  actions.
- `GET /api/state` - hydrate active protocol, log, and timer state.
- `GET /api/protocols` and `GET /api/protocols/recent` - protocol catalog and
  dashboard recents.
- `POST /api/protocols/{id}/load` - deterministic protocol load.
- `GET/PATCH/DELETE /api/protocols/{id}` - full protocol detail, edit, and
  delete.
- `POST /api/protocols/import` - pasted prose to protocol YAML.
- `POST /api/protocols/import/file` - text-readable PDF to protocol YAML.
- `POST /api/protocols` - direct YAML upload.
- `POST /api/step/next` - button-driven confirm or skip.
- `GET/POST /api/notebooks` and `POST /api/notebooks/{id}/select` - notebook
  list, create, and switch active notebook.
- `GET/POST /api/log` and `PATCH /api/log/{id}` - active-notebook log feed and
  entry edits.
- `GET/POST /api/inventory` and `PUT/DELETE /api/inventory/{id}` - inventory
  read and item CRUD.
- `POST /api/scale` - deterministic reagent prep scaling for the active or
  selected protocol.
- `POST /api/demo/reset` - factory reset.
- `GET /api/health` - basic backend health and loaded data counts.
- `WS /ws/events` - UI event stream.
- `WS /ws/audio` - browser mic audio and voice mute controls.

## Layout

```text
backend/
  main.py              FastAPI app, REST endpoints, WS events/audio, static UI
  deepgram_stt.py      server-side Deepgram live STT proxy
  voice_control.py     always-listening mute/unmute gate
  schema.py            Command model and locked event-envelope builders
  router.py            transcript -> Command, LLM optional with deterministic paths
  handlers.py          deterministic command handlers
  state.py             protocols, inventory, timers, notebooks, reset model
  db.py                SQLite NoteStore for notes and notebooks
  protocol_import.py   pasted/PDF protocol import helpers
  pdf_extract.py       PDF text extraction and reflow
  scaling.py           reagent prep scaling and inventory verdicts
  reproducibility.py   deterministic volume checks for notebook flags
  data/                live protocol, inventory, and runtime DB files
  seed/                factory-reset baseline files

FrontendTest/
  dashboard.html       dashboard, recent protocols, recent notebooks, live status
  protocols.html       protocol library, import, edit, delete
  guide.html           active run, step tracker, timers, reagent prep
  notebook.html        notebooks, log feed, edit, search, sort, export
  inventory.html       inventory search/add/edit/delete
  commands.html        voice command reference
  app.js               REST hydrate, WS dispatch, render logic
  voice.js             browser mic session and voice-state UI

frontend/
  sw.js                legacy self-unregistering service worker stub

tests/                 API, router, handlers, voice, protocol import/edit,
                       notebooks, reset, scaling, persistence, and UI static checks
```

## Test

Use the repo virtual environment on Windows:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .pytest-tmp\codex-run -p no:cacheprovider
```

For README-only edits, `git diff --check -- README.md` is a lightweight formatting
guard.

## Not yet

Deferred pieces include TTS, VAD-gated streaming/cost optimization, production
auth, hosted multi-user deployment, and real LIMS integration.
