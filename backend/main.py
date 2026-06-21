"""main.py — FastAPI: the single ingestion spine + WS event bus + static UI.

The spine (locked):

    transcript -> route() -> handle_command() -> broadcast() over /ws/events

Input channels are swappable; the spine is not. The typed box POSTs to
``/api/ingest`` today; live Deepgram ``is_final`` will call the SAME ``ingest()``
later. API keys are read from env ONLY and never sent to the browser.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .deepgram_stt import run_deepgram_session, set_custom_keywords
from .router import ROUTER_MODE, route
from .protocol_import import import_protocol
from .pdf_extract import PdfExtractError, extract_pdf_text, reflow_pdf_text
from .scaling import build_prep_table
from .reproducibility import check as check_reproducibility
from .schema import (
    Command,
    error_event,
    log_entry_event,
    log_update_event,
    notebook_list_event,
    protocol_imported_event,
    protocol_updated_event,
    reset_event,
    timer_removed_event,
    timer_update_event,
    transcript_update_event,
    voice_state_event,
)
from .handlers import advance_step, edit_log_entry, handle_command, resync_active_protocol
from .instrumentation import chain_span, setup_tracing
from .state import ProtocolParseError, SessionState
from .voice_control import VoiceControl, classify_control
from .aliases import AliasStore

# FrontendTest is the served live UI. The legacy ``frontend/`` app is kept on
# disk (its service worker is still served at /sw.js) but is no longer the root.
FRONTEND_DIR = Path(__file__).parent.parent / "FrontendTest"
LEGACY_FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# Log persistence (the one stateful organ). Override with LAB_DB_PATH; set to
# ":memory:" to disable on-disk persistence.
DB_PATH = os.getenv("LAB_DB_PATH", str(Path(__file__).parent / "data" / "lab.db"))

# File-driven protocols + inventory live here. Override with LAB_DATA_DIR (e.g. a
# scratch copy for tests) so uploads/adds don't mutate the shipped data.
DATA_DIR = Path(os.getenv("LAB_DATA_DIR", str(Path(__file__).parent / "data")))

state = SessionState(data_dir=DATA_DIR, db_path=DB_PATH)

# Process-wide mute gate shared by every input channel: the typed box, the
# spoken "mute"/"unmute", and the Mute button all toggle this one state.
voice = VoiceControl()

# User-defined custom commands (authored on the Commands page, synced here). The
# spine expands a matching trigger into its mapped built-in phrase before routing.
aliases = AliasStore()


class ConnectionManager:
    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)

    async def broadcast(self, events: list[dict[str, Any]]) -> None:
        dead: list[WebSocket] = []
        for ws in self.active:
            for event in events:
                try:
                    await ws.send_json(event)
                except Exception:
                    dead.append(ws)
                    break
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


async def ingest(
    transcript: str, *, echo_transcript: bool = True, do_route: bool = True
) -> list[dict[str, Any]]:
    """The single spine. transcript in -> events broadcast + returned.

    ``do_route=False`` only echoes the transcript (used by voice after the
    heard text has already been broadcast).
    """
    events: list[dict[str, Any]] = []
    text = (transcript or "").strip()
    if not text:
        return events
    # A typed/spoken "mute"/"unmute" toggles the shared mic gate instead of being
    # routed as a lab command. (Voice handles its own control words upstream, so
    # this is the path the typed command box takes.)
    control = classify_control(text)
    if control is not None:
        vs = voice.set_muted(control == "mute")
        events.append(voice_state_event(vs.muted, vs.label))
        await manager.broadcast(events)
        return events
    # Expand a user-defined custom command into its mapped built-in phrase BEFORE
    # routing, so a custom trigger executes cleanly instead of falling through to
    # "I didn't understand that". The user's ORIGINAL words are still echoed to
    # the transcript; only the text handed to route() is the expansion.
    route_text = aliases.expand(text) or text
    if echo_transcript:
        events.append(transcript_update_event(text, is_final=True))
    if do_route:
        # CHAIN span over the routing decision + command execution. The
        # auto-instrumented Anthropic span (the `ask` path) nests under it.
        with chain_span("ingest", route_text) as span:
            if route_text != text:
                span.set_attribute("lab.alias_trigger", text)
            cmd = route(route_text)
            span.set_attribute("lab.intent", str(cmd.intent))
            events.extend(handle_command(cmd, state))
            span.set_output(cmd.intent)
    await manager.broadcast(events)
    return events


async def _timer_loop() -> None:
    """Tick active timers ~1/s and broadcast timer_update; mark expiry once."""
    while True:
        await asyncio.sleep(1)
        if not state.timers:
            continue
        events: list[dict[str, Any]] = []
        for timer in list(state.timers):
            if timer.paused:  # frozen: don't tick or expire until started
                continue
            remaining = timer.remaining_s()
            just_expired = remaining <= 0 and not timer.expired
            if just_expired:
                timer.expired = True
            if not timer.expired or just_expired:
                events.append(
                    timer_update_event(timer.timer_id, timer.label, remaining, expired=timer.expired)
                )
        if events:
            await manager.broadcast(events)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Arize tracing: patches the Anthropic SDK before any client is created.
    # No-ops without ARIZE_* creds or under pytest, so the typed demo stays free.
    setup_tracing()
    state.load_files()
    print(
        f"[lab] loaded {len(state.protocols)} protocol(s), "
        f"{len(state.inventory)} inventory item(s); router mode={ROUTER_MODE}; "
        f"anthropic_key={'set' if os.getenv('ANTHROPIC_API_KEY') else 'absent'}"
    )
    task = asyncio.create_task(_timer_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Lab — Voice-Driven ELN (typed skeleton)", lifespan=lifespan)


class IngestIn(BaseModel):
    transcript: str


@app.post("/api/ingest")
async def api_ingest(body: IngestIn) -> dict[str, Any]:
    """Typed input channel (also the curl-able test entrypoint)."""
    try:
        events = await ingest(body.transcript)
        return {"ok": True, "events": events}
    except Exception as exc:  # never let one bad command 500 the whole UI
        ev = error_event("ingest_failed", str(exc), "ingest")
        await manager.broadcast([ev])
        return {"ok": False, "events": [ev]}


class AliasIn(BaseModel):
    trigger: str
    phrase: str


class AliasesIn(BaseModel):
    aliases: list[AliasIn] = []


@app.get("/api/aliases")
async def get_aliases() -> dict[str, Any]:
    """Current custom-command set the spine will expand (trigger -> phrase)."""
    return {"aliases": aliases.as_list()}


@app.post("/api/aliases")
async def set_aliases(body: AliasesIn) -> dict[str, Any]:
    """Replace the custom-command set. The Commands page syncs its localStorage
    copy here on load and on every add/delete, so a spoken/typed trigger expands
    to its mapped built-in phrase before routing."""
    count = aliases.set_all([{"trigger": a.trigger, "phrase": a.phrase} for a in body.aliases])
    # Mirror the live trigger set into the Deepgram boost list so a user's custom
    # phrase is heard reliably. Recomputed wholesale here, so deleting a command
    # (which re-syncs the shrunken list) drops its boost on the next session.
    set_custom_keywords([a.trigger for a in body.aliases])
    return {"ok": True, "count": count}


@app.get("/api/protocols")
async def list_protocols() -> dict[str, Any]:
    return {"protocols": state.protocol_catalog()}


@app.get("/api/protocols/recent")
async def list_recent_protocols() -> dict[str, Any]:
    """The 3 most-recently-used protocols for the dashboard (newest first).
    Declared before the ``/{protocol_id}`` routes so the static path wins."""
    return {"recent": state.recent_protocols_view()}


@app.post("/api/protocols/{protocol_id}/load")
async def load_protocol_by_id(protocol_id: str) -> dict[str, Any]:
    """Deterministic catalog-card load: never touches the router LLM."""
    events = handle_command(
        Command(intent="load_protocol", protocol_name=protocol_id), state
    )
    await manager.broadcast(events)
    return {"ok": True, "events": events}


@app.delete("/api/protocols/{protocol_id}")
async def delete_protocol(protocol_id: str) -> dict[str, Any]:
    """Remove a protocol from the library and delete its YAML file. If it was the
    active protocol, the stage is cleared server-side."""
    if not state.remove_protocol(protocol_id):
        raise HTTPException(status_code=404, detail="No such protocol.")
    return {"ok": True, "protocols": state.protocol_catalog()}


@app.get("/api/protocols/{protocol_id}")
async def get_protocol(protocol_id: str) -> dict[str, Any]:
    """Full protocol incl. step text/duration/params, for prefilling the editor.
    Declared after ``/recent`` so that static path still matches first."""
    detail = state.protocol_detail(protocol_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="No such protocol.")
    return {"ok": True, "protocol": detail}


class ProtocolStepEdit(BaseModel):
    text: str
    duration_s: int | None = None
    timer_label: str | None = None
    parameters: dict[str, Any] = {}


class ProtocolEditIn(BaseModel):
    name: str
    description: str = ""
    steps: list[ProtocolStepEdit] = []


@app.patch("/api/protocols/{protocol_id}")
async def edit_protocol(protocol_id: str, body: ProtocolEditIn) -> dict[str, Any]:
    """Deterministic structured edit of name/description/steps (id frozen, no LLM).
    reagents + duration re-derive; if this protocol is the active run, the Guide
    is resynced in place. Malformed input is a controlled {ok: False}, not a 500."""
    if protocol_id not in state.protocols:
        raise HTTPException(status_code=404, detail="No such protocol.")
    steps = [
        {
            "text": s.text,
            "duration_s": s.duration_s,
            "timer_label": s.timer_label,
            "parameters": s.parameters,
        }
        for s in body.steps
    ]
    try:
        proto = state.update_protocol(protocol_id, body.name, body.description, steps)
    except ProtocolParseError as exc:
        await manager.broadcast(
            [error_event("protocol_update_failed", str(exc), "protocol_edit")]
        )
        return {"ok": False, "error": str(exc)}
    events: list[dict[str, Any]] = []
    if state.active_protocol is not None and state.active_protocol.id == proto.id:
        events.extend(resync_active_protocol(state, proto))
    summary = next(p for p in state.protocol_catalog() if p["id"] == proto.id)
    events.append(protocol_updated_event(proto.name, proto.id, len(proto.steps)))
    await manager.broadcast(events)
    return {"ok": True, "protocol": summary, "events": events}


class StepAdvanceIn(BaseModel):
    # Confirm Action -> log=True (step completed); Skip -> log=False (skipped).
    log: bool = True


@app.post("/api/step/next")
async def api_step_next(body: StepAdvanceIn | None = None) -> dict[str, Any]:
    """Button-driven step advance. ``log=True`` (Confirm Action, also the
    default for a bodyless POST) writes a "Completed step N" note to the active
    notebook the same way a spoken/typed "next step" does; ``log=False`` (Skip)
    writes a "Skipped step N" note and marks the step skipped in the tracker."""
    completed = True if body is None else body.log
    events = advance_step(state, completed=completed)
    await manager.broadcast(events)
    return {"ok": True, "events": events}


class ProtocolImportIn(BaseModel):
    text: str
    name: str | None = None


async def _import_failed(message: str) -> dict[str, Any]:
    """Broadcast an import failure and return the controlled {ok: False} body."""
    await manager.broadcast(
        [error_event("protocol_import_failed", message, "protocol_import")]
    )
    return {"ok": False, "error": message}


async def _run_import(text: str, name: str | None) -> dict[str, Any]:
    """Shared tail for prose- and PDF-sourced imports: split -> register -> broadcast.

    Malformed prose returns a controlled {ok: False} response, never a 500.
    """
    try:
        proto, _ = import_protocol(text, name, state)
    except ValueError as exc:
        return await _import_failed(str(exc))
    summary = next(p for p in state.protocol_catalog() if p["id"] == proto.id)
    load_hint = f'Say "Load {proto.name}" to start it.'
    event = protocol_imported_event(
        proto.name, proto.id, len(proto.steps), proto.aliases, load_hint
    )
    await manager.broadcast([event])
    return {"ok": True, "protocol": summary, "load_hint": load_hint}


@app.post("/api/protocols/import")
async def import_protocol_endpoint(body: ProtocolImportIn) -> dict[str, Any]:
    """Paste-to-import: prose -> YAML -> registered protocol."""
    return await _run_import(body.text, body.name)


MAX_PDF_BYTES = 10 * 1024 * 1024  # reject oversized uploads before reading the whole thing


@app.post("/api/protocols/import/file")
async def import_protocol_file_endpoint(
    file: UploadFile = File(...), name: str | None = Form(None)
) -> dict[str, Any]:
    """PDF-to-import: extract the PDF's text, then run the same import pipeline.

    Request errors (not a PDF / too large) are 4xx; a readable PDF that yields no
    usable steps returns a controlled {ok: False}, mirroring the prose endpoint.
    """
    raw = await file.read()
    if len(raw) > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF is too large (max 10 MB).")
    if not raw.startswith(b"%PDF"):
        raise HTTPException(status_code=422, detail="expected a PDF file")
    try:
        text = reflow_pdf_text(extract_pdf_text(raw))
    except PdfExtractError as exc:
        return await _import_failed(f"Couldn't read this PDF ({exc}).")
    if not text.strip():
        return await _import_failed(
            "Couldn't read any text from this PDF -- it may be a scanned image."
        )
    return await _run_import(text, name)


@app.post("/api/demo/reset")
async def demo_reset() -> dict[str, Any]:
    """Full demo reset: returns everything to the original (seed) baseline —
    clears run state, wipes notes + every created notebook, and restores the
    inventory CSV and protocol library. Always wipes (no LAB_DEMO_MODE gate)."""
    state.restore_factory_state()
    vs = voice.set_muted(False)
    event = reset_event(notes_cleared=True)
    await manager.broadcast([event, voice_state_event(vs.muted, vs.label)])
    return {"ok": True, "notes_cleared": True, "events": [event]}


@app.get("/api/inventory")
async def list_inventory() -> dict[str, Any]:
    return {"items": state.inventory_view()}


class ScaleIn(BaseModel):
    sample_count: int
    overage_percent: float = 10.0
    protocol_id: str | None = None


@app.post("/api/scale")
async def scale_protocol(body: ScaleIn) -> dict[str, Any]:
    """Read-only reagent scaling for the active or selected protocol."""
    if body.sample_count < 1:
        raise HTTPException(status_code=422, detail="sample count must be at least 1")
    if body.overage_percent < 0:
        raise HTTPException(status_code=422, detail="overage percent must be non-negative")

    if body.protocol_id:
        proto = state.protocols.get(body.protocol_id)
        if proto is None:
            raise HTTPException(status_code=404, detail=f"unknown protocol: {body.protocol_id}")
    else:
        proto = state.active_protocol
        if proto is None:
            raise HTTPException(status_code=404, detail="load a protocol or provide protocol_id")

    rows = build_prep_table(
        proto,
        n_samples=body.sample_count,
        overage_pct=body.overage_percent,
        inventory=state.inventory,
    )
    return {
        "ok": True,
        "protocol_id": proto.id,
        "protocol_name": proto.name,
        "sample_count": body.sample_count,
        "overage_percent": body.overage_percent,
        "reagents": rows,
    }


@app.get("/api/log")
async def list_log() -> dict[str, Any]:
    return {"log": state.log}


@app.get("/api/notebooks")
async def list_notebooks() -> dict[str, Any]:
    return {"notebooks": state.notebooks_view(), "active_id": state.active_notebook_id}


class NotebookIn(BaseModel):
    name: str | None = None


@app.post("/api/notebooks", status_code=201)
async def create_notebook(body: NotebookIn) -> dict[str, Any]:
    """Create a notebook and make it active; new log entries land in it."""
    notebook = state.create_notebook(body.name or "")
    await manager.broadcast(
        [notebook_list_event(state.notebooks_view(), state.active_notebook_id)]
    )
    return {
        "ok": True,
        "notebook": notebook,
        "notebooks": state.notebooks_view(),
        "active_id": state.active_notebook_id,
    }


@app.post("/api/notebooks/{notebook_id}/select")
async def select_notebook(notebook_id: int) -> dict[str, Any]:
    """Switch the active notebook; the log feed follows it."""
    if not state.select_notebook(notebook_id):
        raise HTTPException(status_code=404, detail="no such notebook")
    await manager.broadcast(
        [notebook_list_event(state.notebooks_view(), state.active_notebook_id)]
    )
    return {"ok": True, "active_id": state.active_notebook_id, "log": state.log}


class LogEntryIn(BaseModel):
    text: str
    sample_id: str | None = None
    category: str | None = None


@app.post("/api/log")
async def add_log(body: LogEntryIn) -> dict[str, Any]:
    """Typed/manual log entry (same persisted feed as the voice/typed spine)."""
    step = state.current_step()
    flag = check_reproducibility(step.parameters, body.text) if step else None
    entry = state.append_log(body.text, body.sample_id, body.category, flag)
    await manager.broadcast([log_entry_event(**entry)])
    return {"ok": True, "entry": entry}


class LogEditIn(BaseModel):
    text: str


@app.patch("/api/log/{log_id}")
async def edit_log(log_id: int, body: LogEditIn) -> dict[str, Any]:
    """Edit a notebook entry by id (the per-row edit button). Re-tags the entry
    manual + edited and recomputes the reproducibility flag; 404 if no such id."""
    entry = edit_log_entry(state, log_id, body.text)
    if entry is None:
        raise HTTPException(status_code=404, detail="no such log entry")
    await manager.broadcast([
        log_update_event(
            int(entry["id"]), str(entry["text"]), entry.get("flag"),
            entry_type=entry.get("entry_type", "manual"), edited=bool(entry.get("edited")),
        )
    ])
    return {"ok": True, "entry": entry}


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    """Snapshot for the UI to hydrate on (re)load. The log is DB-backed, so it
    survives a refresh or a server restart; step/timers are in-memory."""
    idx = state.current_step_index
    cur = state.current_step()
    proto = state.active_protocol
    return {
        "log": state.log,
        "step": {
            "prev_step": (s.as_event() if (s := state.step_at(idx - 1)) else None),
            "current_step": (cur.as_event() if cur else None),
            "next_step": (s.as_event() if (s := state.step_at(idx + 1)) else None),
            "all_steps": [s.as_event() for s in proto.steps] if proto else [],
            "current_index": idx,
            "protocol_name": proto.name if proto else None,
            "finished": state.protocol_complete,
            "skipped_indices": sorted(state.skipped_steps),
        }
        if cur
        else None,
        "timers": [
            {
                "timer_id": t.timer_id,
                "label": t.label,
                "remaining_s": t.remaining_s(),
                "expired": t.expired,
                "paused": t.paused,
            }
            for t in state.timers
            if not t.expired
        ],
    }


class InventoryItemIn(BaseModel):
    name: str
    location: str = ""
    quantity_approx: str = ""
    amount: str = ""
    unit: str = ""
    notes: str = ""
    date: str = ""


class InventoryItemEdit(BaseModel):
    """Partial edit — only provided (non-None) fields are changed."""
    name: Optional[str] = None
    location: Optional[str] = None
    amount: Optional[str] = None
    unit: Optional[str] = None
    date: Optional[str] = None


def _inventory_item_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "location": item.location,
        "amount": item.amount,
        "unit": item.unit,
        "quantity_approx": item.quantity_approx,
        "notes": item.notes,
        "date": item.date,
    }


@app.post("/api/inventory", status_code=201)
async def add_inventory(body: InventoryItemIn) -> dict[str, Any]:
    """Manual add-item entry: persist a reagent to the file-driven inventory."""
    try:
        item = state.add_inventory_item(
            body.name,
            body.location,
            body.quantity_approx,
            body.notes,
            date=body.date,
            amount=body.amount,
            unit=body.unit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "ok": True,
        "item": _inventory_item_payload(item),
        "inventory_count": len(state.inventory),
    }


@app.put("/api/inventory/{item_id}")
async def edit_inventory(item_id: int, body: InventoryItemEdit) -> dict[str, Any]:
    """Edit fields of the inventory item with ``item_id`` (stable id, not position)."""
    try:
        item = state.update_inventory_item(
            item_id,
            name=body.name,
            location=body.location,
            amount=body.amount,
            unit=body.unit,
            date=body.date,
        )
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "ok": True,
        "item": _inventory_item_payload(item),
        "inventory_count": len(state.inventory),
    }


@app.delete("/api/inventory/{item_id}")
async def remove_inventory(item_id: int) -> dict[str, Any]:
    """Delete the inventory item with ``item_id`` (stable id, not position)."""
    try:
        removed = state.delete_inventory_item(item_id)
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "ok": True,
        "removed": removed.name,
        "inventory_count": len(state.inventory),
    }


@app.post("/api/protocols", status_code=201)
async def upload_protocol(file: UploadFile = File(...)) -> dict[str, Any]:
    """Standard file upload: validate a protocol YAML and register it for loading."""
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="file must be UTF-8 text") from exc
    try:
        proto = state.add_protocol_from_text(text)
    except ProtocolParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "ok": True,
        "protocol": {"id": proto.id, "name": proto.name, "steps": len(proto.steps)},
        "protocols": list(state.protocols.keys()),
    }


@app.post("/api/timers/{timer_id}/stop")
async def stop_timer(timer_id: str) -> dict[str, Any]:
    """Stop one timer early (the clicked card "x"); broadcasts so every client
    drops the card and silences its alarm. Same gate voice/typed "stop timer" use."""
    if not state.remove_timer(timer_id):
        raise HTTPException(status_code=404, detail="No such timer.")
    ev = timer_removed_event(timer_id)
    await manager.broadcast([ev])
    return {"ok": True, "events": [ev]}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "router_mode": ROUTER_MODE,
        "protocols": list(state.protocols.keys()),
        "inventory_count": len(state.inventory),
        "voice": {"mode": "always_listening", "mute_controls": True},
    }


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        while True:
            # Output-only channel in this pass; drain anything the client sends.
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket) -> None:
    """Browser mic audio in -> Deepgram -> same ingest() spine.

    Interim results display only; a finished utterance is routed exactly like a
    typed line. Voice is just another way to fill the transcript.
    """
    await ws.accept()
    # Mute is sticky: a (re)connect must NOT clear it, so a muted mic keeps
    # listening across reconnects until an explicit "unmute". Just sync the UI to
    # the shared gate's current state.
    await manager.broadcast([voice_state_event(voice.muted, voice.label)])

    async def on_interim(text: str) -> None:
        if voice.muted:
            # Watch only for the resume word so "unmute" takes effect the instant
            # it's heard. Every other interim is dropped (never broadcast), so the
            # transcript box shows nothing while muted.
            state = voice.process_interim(text)
            if state is not None:
                await manager.broadcast([voice_state_event(state.muted, state.label)])
            return
        await manager.broadcast([transcript_update_event(text, is_final=False)])

    async def on_final(text: str) -> None:
        decision = voice.process_final(text)
        if decision.voice_state_changed:
            await manager.broadcast([voice_state_event(decision.muted, decision.label)])
        if decision.report_transcript:
            await manager.broadcast([transcript_update_event(decision.command_text, is_final=True)])
        if decision.route_command:
            await ingest(decision.command_text, echo_transcript=False)

    async def on_control(ctrl: dict[str, Any]) -> None:
        if ctrl.get("type") != "set_muted":
            return
        state = voice.set_muted(bool(ctrl["muted"]))
        await manager.broadcast([voice_state_event(state.muted, state.label)])

    try:
        await run_deepgram_session(
            ws, on_interim=on_interim, on_final=on_final, on_control=on_control
        )
    except Exception as exc:
        await manager.broadcast([error_event("stt_failed", str(exc), "deepgram")])
    finally:
        try:
            await ws.close()
        except Exception:
            pass


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "dashboard.html")


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    # Served from the root so the worker's default scope is the whole origin
    # (a worker under /static would only control /static).
    return FileResponse(LEGACY_FRONTEND_DIR / "sw.js", media_type="application/javascript")


# Root static mount LAST so every /api/* and /ws/* route is matched first.
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
