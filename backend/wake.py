"""wake.py — hands-free wake-word gate for the voice channel.

Voice is always-on while a session is armed, but we should only ACT on speech
addressed to the assistant. This gate decides, per finished utterance, whether it
is a command:

* "Hey Otto, what's next?"  -> route "what's next?"            (wake + command)
* "Hey Otto."               -> open a short follow-up window    (bare wake)
* "what's next?"  (<=window after a wake) -> route it           (follow-up)
* "...background chatter..." (no wake, no window) -> ignore      (not for us)

Typed input bypasses this entirely — typing is already an explicit address.
Disable with OTTO_WAKE_REQUIRED=false (every utterance routes).
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

WAKE_WORD = os.getenv("OTTO_WAKE_WORD", "otto").strip().lower()
WAKE_REQUIRED = os.getenv("OTTO_WAKE_REQUIRED", "true").lower() not in {"false", "0", "no"}
WAKE_WINDOW_S = float(os.getenv("OTTO_WAKE_WINDOW_S", "8"))

# STT routinely hears "Otto" as "auto"/"ado"; accept the homophones so the demo
# isn't held hostage by one mistranscribed syllable. Override with OTTO_WAKE_ALIASES.
_DEFAULT_ALIASES = "otto,auto,ado,oddo"
WAKE_ALIASES = sorted(
    {WAKE_WORD, *(a.strip().lower() for a in os.getenv("OTTO_WAKE_ALIASES", _DEFAULT_ALIASES).split(",") if a.strip())}
)

# Optional greeting before the wake word: "hey otto", "ok otto", bare "otto".
_GREET = r"(?:hey|hi|hello|ok|okay|yo)?"
_WAKE_ALT = "|".join(re.escape(a) for a in WAKE_ALIASES)
_WAKE_RE = re.compile(
    rf"^\s*{_GREET}[\s,.:;!-]*(?:{_WAKE_ALT})\b[\s,.:;!-]*(.*)$",
    flags=re.IGNORECASE | re.DOTALL,
)


@dataclass
class WakeDecision:
    should_route: bool
    command_text: str
    just_woke: bool  # bare wake word -> we opened the listening window


class WakeGate:
    """Per-connection wake state. One instance per /ws/audio session."""

    def __init__(self) -> None:
        self._open_until = 0.0

    def process(self, transcript: str, *, now: float | None = None) -> WakeDecision:
        now = time.monotonic() if now is None else now
        text = (transcript or "").strip()
        if not WAKE_REQUIRED:
            return WakeDecision(should_route=bool(text), command_text=text, just_woke=False)

        m = _WAKE_RE.match(text)
        if m:
            command = m.group(1).strip()
            self._open_until = now + WAKE_WINDOW_S
            if command:
                return WakeDecision(True, command, just_woke=False)
            return WakeDecision(False, "", just_woke=True)  # bare wake -> listening

        if now < self._open_until:
            # follow-up turn inside the window — consume it and refresh
            self._open_until = now + WAKE_WINDOW_S
            return WakeDecision(True, text, just_woke=False)

        return WakeDecision(False, "", just_woke=False)  # not addressed to us
