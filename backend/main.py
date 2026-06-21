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
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .deepgram_stt import run_deepgram_session
from .router import ROUTER_MODE, route
from .schema import (
    error_event,
    timer_update_event,
    transcript_update_event,
    voice_state_event,
)
from .handlers import handle_command
from .state import ProtocolParseError, SessionState
from .voice_control import VoiceControl, classify_control

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

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
    if echo_transcript:
        events.append(transcript_update_event(text, is_final=True))
    if do_route:
        cmd = route(text)
        events.extend(handle_command(cmd, state))
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


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    """Snapshot for the UI to hydrate on (re)load. The log is DB-backed, so it
    survives a refresh or a server restart; step/timers are in-memory."""
    idx = state.current_step_index
    cur = state.current_step()
    return {
        "log": state.log,
        "step": {
            "prev_step": (s.as_event() if (s := state.step_at(idx - 1)) else None),
            "current_step": (cur.as_event() if cur else None),
            "next_step": (s.as_event() if (s := state.step_at(idx + 1)) else None),
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
    notes: str = ""


@app.post("/api/inventory", status_code=201)
async def add_inventory(body: InventoryItemIn) -> dict[str, Any]:
    """Manual add-item entry: persist a reagent to the file-driven inventory."""
    try:
        item = state.add_inventory_item(
            body.name, body.location, body.quantity_approx, body.notes
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "ok": True,
        "item": {
            "name": item.name,
            "location": item.location,
            "quantity_approx": item.quantity_approx,
            "notes": item.notes,
        },
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
            # Don't display while muted, but still watch for the resume word so
            # "unmute" takes effect the instant it's heard.
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
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
