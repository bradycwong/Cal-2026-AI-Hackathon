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
from typing import Any, Awaitable, Callable
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
    ("ethanol", 1), ("EDTA", 2), ("nuclease", 1), ("DNA", 2), ("Lab", 2),
]

OnText = Callable[[str], Awaitable[None]]
OnControl = Callable[[dict[str, Any]], Awaitable[None]]

# Connect resilience: Deepgram's socket can blip on open. Retry a few times with
# linear backoff before giving up so a transient hiccup doesn't kill the session.
CONNECT_ATTEMPTS = int(os.getenv("DEEPGRAM_CONNECT_ATTEMPTS", "3"))
CONNECT_OPEN_TIMEOUT = float(os.getenv("DEEPGRAM_OPEN_TIMEOUT", "8"))


def interpret_message(data: dict) -> tuple[str, str]:
    """Pure: map one Deepgram message to (action, text).

    action ∈ {"interim", "segment", "segment_flush", "flush", "ignore"}:
      * interim       — partial words for the live panel only
      * segment       — a finalized chunk to accumulate (is_final, not speech_final)
      * segment_flush — finalized chunk that ends the utterance (speech_final)
      * flush         — endpoint with no new text (UtteranceEnd) -> emit what we have
      * ignore        — empty/keepalive/metadata; do nothing
    """
    mtype = data.get("type")
    if mtype == "Results":
        alts = (data.get("channel") or {}).get("alternatives") or [{}]
        text = (alts[0].get("transcript") or "").strip()
        if not text:
            return ("ignore", "")
        if data.get("is_final"):
            return ("segment_flush" if data.get("speech_final") else "segment", text)
        return ("interim", text)
    if mtype == "UtteranceEnd":
        return ("flush", "")
    return ("ignore", "")


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
    browser_ws: WebSocket,
    *,
    on_interim: OnText,
    on_final: OnText,
    on_control: OnControl | None = None,
) -> None:
    """Bridge one browser audio socket to one Deepgram live session."""
    key = os.getenv("DEEPGRAM_API_KEY")
    if not key:
        raise RuntimeError("DEEPGRAM_API_KEY is not set on the server")

    dg = await _connect_with_retry(
        _build_url(), {"Authorization": f"Token {key}"}
    )
    try:
        stop = asyncio.Event()
        segments: list[str] = []
        last_final = [""]  # boxed so the closure can dedup consecutive finals

        async def pump_browser_to_dg() -> None:
            try:
                while not stop.is_set():
                    msg = await browser_ws.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    if (data := msg.get("bytes")) is not None:
                        await dg.send(data)
                    elif (text := msg.get("text")) is not None:
                        if await handle_browser_control_message(text, on_control):
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
                    try:
                        data = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    action, text = interpret_message(data)
                    if action == "interim":
                        await on_interim(text)
                    elif action == "segment":
                        segments.append(text)
                    elif action == "segment_flush":
                        segments.append(text)
                        await _flush(segments, last_final, on_final)
                    elif action == "flush":
                        await _flush(segments, last_final, on_final)
            except websockets.ConnectionClosed:
                pass
            finally:
                await _flush(segments, last_final, on_final)  # drain on close
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
    finally:
        try:
            await dg.close()
        except Exception:
            pass


async def _connect_with_retry(url: str, headers: dict[str, str]):
    """Open the Deepgram socket, retrying transient failures with linear backoff."""
    last_exc: Exception | None = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return await websockets.connect(
                url, additional_headers=headers, open_timeout=CONNECT_OPEN_TIMEOUT
            )
        except Exception as exc:  # network/handshake hiccup -> retry
            last_exc = exc
            if attempt < CONNECT_ATTEMPTS:
                await asyncio.sleep(0.4 * attempt)
    raise RuntimeError(f"could not connect to Deepgram after {CONNECT_ATTEMPTS} attempts") from last_exc


async def handle_browser_control_message(
    text: str, on_control: OnControl | None = None
) -> bool:
    """Handle one browser text control message.

    Returns True when the audio stream should stop. Other controls are forwarded
    to the caller through ``on_control``.
    """
    try:
        ctrl = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(ctrl, dict):
        return False
    if ctrl.get("type") == "stop":
        return True
    if ctrl.get("type") == "set_muted" and isinstance(ctrl.get("muted"), bool):
        if on_control is not None:
            await on_control({"type": "set_muted", "muted": ctrl["muted"]})
    return False


async def _flush(segments: list[str], last_final: list[str], on_final: OnText) -> None:
    if not segments:
        return
    utterance = " ".join(segments).strip()
    segments.clear()
    if not utterance:
        return
    if utterance == last_final[0]:  # drop a duplicate of the immediately prior final
        return
    last_final[0] = utterance
    await on_final(utterance)
