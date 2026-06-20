"""deepgram_stt.py — server-side Deepgram live STT proxy.

The browser streams mic audio (webm/opus blobs) to our ``/ws/audio``; this module
relays it to Deepgram's live endpoint and turns Deepgram's result messages back
into two callbacks:

* ``on_interim(text)``  — partial words, for the live transcript panel only.
* ``on_final(text)``    — a completed utterance, routed through the SAME ``ingest()``.

The Deepgram API key is read from the server env ONLY and never reaches the
browser (locked invariant #4). Audio is containerized webm/opus, which Deepgram
auto-detects and resamples server-side — so we do NOT send encoding/sample_rate.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Awaitable, Callable
from urllib.parse import urlencode

import websockets
from starlette.websockets import WebSocket, WebSocketDisconnect

DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"
DEEPGRAM_MODEL = os.getenv("DEEPGRAM_MODEL", "nova-2")

# Rare lab nouns that general STT mangles (PDF §3.3.6). keyword:boost for nova-2.
# Only boost distinctive domain terms — boosting common verbs ("load", "log")
# pulls neighbouring words toward them and hurts overall accuracy.
KEYWORDS = [
    ("microliters", 2), ("proteinase", 3), ("lysis", 2), ("PCR", 2),
    ("centrifuge", 2), ("pipette", 2), ("aliquot", 2), ("incubate", 1),
    ("ethanol", 1), ("EDTA", 2), ("nuclease", 1), ("DNA", 2), ("Otto", 2),
]

OnText = Callable[[str], Awaitable[None]]


def _build_url() -> str:
    params = [
        ("model", DEEPGRAM_MODEL),
        ("language", "en-US"),
        ("smart_format", "true"),
        ("punctuate", "true"),
        ("interim_results", "true"),
        ("endpointing", "300"),
        ("utterance_end_ms", "1000"),
        ("vad_events", "true"),
    ]
    params += [("keywords", f"{kw}:{boost}") for kw, boost in KEYWORDS]
    return f"{DEEPGRAM_URL}?{urlencode(params)}"


async def run_deepgram_session(
    browser_ws: WebSocket, *, on_interim: OnText, on_final: OnText
) -> None:
    """Bridge one browser audio socket to one Deepgram live session."""
    key = os.getenv("DEEPGRAM_API_KEY")
    if not key:
        raise RuntimeError("DEEPGRAM_API_KEY is not set on the server")

    async with websockets.connect(
        _build_url(), additional_headers={"Authorization": f"Token {key}"}
    ) as dg:
        stop = asyncio.Event()
        segments: list[str] = []

        async def pump_browser_to_dg() -> None:
            try:
                while not stop.is_set():
                    msg = await browser_ws.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    if (data := msg.get("bytes")) is not None:
                        await dg.send(data)
                    elif (text := msg.get("text")) is not None:
                        # control messages from the browser, e.g. {"type":"stop"}
                        try:
                            ctrl = json.loads(text)
                        except json.JSONDecodeError:
                            continue
                        if ctrl.get("type") == "stop":
                            break
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                stop.set()
                try:
                    await dg.send(json.dumps({"type": "CloseStream"}))
                except Exception:
                    pass

        async def pump_dg_to_callbacks() -> None:
            try:
                async for raw in dg:
                    data = json.loads(raw)
                    mtype = data.get("type")
                    if mtype == "Results":
                        alt = data["channel"]["alternatives"][0]
                        text = (alt.get("transcript") or "").strip()
                        if not text:
                            continue
                        if data.get("is_final"):
                            segments.append(text)
                            if data.get("speech_final"):
                                await _flush(segments, on_final)
                        else:
                            await on_interim(text)
                    elif mtype == "UtteranceEnd":
                        await _flush(segments, on_final)
            except websockets.ConnectionClosed:
                pass
            finally:
                await _flush(segments, on_final)  # drain leftovers on close
                stop.set()

        async def keepalive() -> None:
            # Cheap warm socket during silence (PDF §3.3.4). Harmless with audio flowing.
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=8)
                except asyncio.TimeoutError:
                    try:
                        await dg.send(json.dumps({"type": "KeepAlive"}))
                    except Exception:
                        return

        await asyncio.gather(
            pump_browser_to_dg(), pump_dg_to_callbacks(), keepalive()
        )


async def _flush(segments: list[str], on_final: OnText) -> None:
    if not segments:
        return
    utterance = " ".join(segments).strip()
    segments.clear()
    if utterance:
        await on_final(utterance)
