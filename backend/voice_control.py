"""Voice mute/unmute gate for the always-listening audio channel."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class VoiceState:
    muted: bool
    label: str
    changed: bool


@dataclass(frozen=True)
class VoiceDecision:
    report_transcript: bool
    route_command: bool
    command_text: str
    voice_state_changed: bool
    muted: bool
    label: str


_MUTE_PATTERNS = [
    re.compile(r"^(?:please\s+)?mute(?:\s+lab)?$", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?stop\s+listening$", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?stop\s+reporting$", re.IGNORECASE),
]

_UNMUTE_PATTERNS = [
    re.compile(r"^(?:please\s+)?unmute(?:\s+lab)?$", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?start\s+listening$", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?resume\s+listening$", re.IGNORECASE),
]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().strip(".,!?;:")).strip()


def _matches(patterns: list[re.Pattern[str]], text: str) -> bool:
    return any(p.match(text) for p in patterns)


def classify_control(text: str) -> Optional[str]:
    """Return ``"mute"`` / ``"unmute"`` if ``text`` is a control phrase, else None.

    Shared by both input channels so a typed "mute" toggles the exact same gate
    a spoken "mute" does. Unmute is checked first so it always wins.
    """
    cleaned = _clean(text)
    if not cleaned:
        return None
    if _matches(_UNMUTE_PATTERNS, cleaned):
        return "unmute"
    if _matches(_MUTE_PATTERNS, cleaned):
        return "mute"
    return None


class VoiceControl:
    """Per-audio-session control state.

    The mic/Deepgram stream stays open while muted so the user can say
    "unmute"; this gate only controls whether transcript text is displayed and
    whether final utterances enter the command spine.
    """

    def __init__(self, muted: bool = False) -> None:
        self.muted = muted

    @property
    def label(self) -> str:
        return "muted" if self.muted else "listening"

    def should_report_interim(self) -> bool:
        return not self.muted

    def set_muted(self, muted: bool) -> VoiceState:
        changed = self.muted != muted
        self.muted = muted
        return VoiceState(muted=self.muted, label=self.label, changed=changed)

    def process_final(self, transcript: str) -> VoiceDecision:
        text = _clean(transcript)
        if not text:
            return self._decision(False, False, "", False)

        control = classify_control(text)
        if control is not None:
            state = self.set_muted(control == "mute")
            return self._decision(False, False, "", state.changed)

        if self.muted:
            return self._decision(False, False, "", False)

        return self._decision(True, True, text, False)

    def _decision(
        self,
        report_transcript: bool,
        route_command: bool,
        command_text: str,
        voice_state_changed: bool,
    ) -> VoiceDecision:
        return VoiceDecision(
            report_transcript=report_transcript,
            route_command=route_command,
            command_text=command_text,
            voice_state_changed=voice_state_changed,
            muted=self.muted,
            label=self.label,
        )
