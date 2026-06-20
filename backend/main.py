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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .deepgram_stt import run_deepgram_session
from .router import ROUTER_MODE, route
from .schema import (
    clarify_event,
    error_event,
    timer_update_event,
    transcript_update_event,
)
from .handlers import handle_command
from .state import SessionState
from .wake import WAKE_REQUIRED, WAKE_WORD, WakeGate

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

state = SessionState()


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

    ``do_route=False`` only echoes the transcript (used when a voice utterance is
    not addressed to the assistant — see the wake gate).
    """
    events: list[dict[str, Any]] = []
    text = (transcript or "").strip()
    if not text:
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
        f"[otto] loaded {len(state.protocols)} protocol(s), "
        f"{len(state.inventory)} inventory item(s); router mode={ROUTER_MODE}; "
        f"anthropic_key={'set' if os.getenv('ANTHROPIC_API_KEY') else 'absent'}"
    )
    task = asyncio.create_task(_timer_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Otto — Voice-Driven ELN (typed skeleton)", lifespan=lifespan)


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


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "router_mode": ROUTER_MODE,
        "protocols": list(state.protocols.keys()),
        "inventory_count": len(state.inventory),
        "wake_required": WAKE_REQUIRED,
        "wake_word": WAKE_WORD,
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
    gate = WakeGate()

    async def on_interim(text: str) -> None:
        await manager.broadcast([transcript_update_event(text, is_final=False)])

    async def on_final(text: str) -> None:
        # Always show what was heard; only ACT when addressed to the assistant.
        decision = gate.process(text)
        await manager.broadcast([transcript_update_event(text, is_final=True)])
        if decision.just_woke:
            await manager.broadcast([clarify_event("Listening - what would you like to do?")])
        elif decision.should_route:
            await ingest(decision.command_text, echo_transcript=False)

    try:
        await run_deepgram_session(ws, on_interim=on_interim, on_final=on_final)
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
