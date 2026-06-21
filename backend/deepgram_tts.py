"""deepgram_tts.py — server-side Deepgram Speak (Aura) wrapper.

The output-side mirror of ``deepgram_stt.py``: the AI's text answers/step
instructions are synthesized to speech here so the key stays server-side
(locked invariant #4). This is read-only and downstream of the spine — no WS
events, no Command changes. When no key is set (or on any error) ``synthesize``
returns ``None`` so the frontend can fall back to the browser's built-in voice.
"""

from __future__ import annotations

import os

import httpx

DEEPGRAM_SPEAK_URL = "https://api.deepgram.com/v1/speak"


def tts_available() -> bool:
    return bool(os.getenv("DEEPGRAM_API_KEY"))


async def synthesize(text: str) -> bytes | None:
    """AI text -> mp3 bytes via Deepgram Speak.

    Returns ``None`` on no-key / empty text / error so the caller falls back to
    the browser voice. The key never leaves the server.
    """
    key = os.getenv("DEEPGRAM_API_KEY")
    text = (text or "").strip()
    if not key or not text:
        return None
    model = os.getenv("DEEPGRAM_TTS_MODEL", "aura-asteria-en")
    url = f"{DEEPGRAM_SPEAK_URL}?model={model}&encoding=mp3"
    try:
        async with httpx.AsyncClient(
            timeout=float(os.getenv("DEEPGRAM_TTS_TIMEOUT", "10"))
        ) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Token {key}",
                    "Content-Type": "application/json",
                },
                json={"text": text[:1900]},  # Deepgram Speak ~2000-char limit
            )
        return resp.content if (resp.status_code == 200 and resp.content) else None
    except Exception:
        return None
