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


# Anchored to the whole utterance so a control word only fires when it IS the
# command (never mid-sentence). The optional suffixes/spellings absorb common STT
# variants (e.g. "muted", "un mute", "un-mute") so spoken unmute reliably resumes.
_MUTE_PATTERNS = [
    re.compile(r"^(?:please\s+)?mute(?:d)?(?:\s+lab)?$", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?stop\s+listening$", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?stop\s+reporting$", re.IGNORECASE),
]

_UNMUTE_PATTERNS = [
    re.compile(r"^(?:please\s+)?un[\s-]?mute(?:d)?(?:\s+lab)?$", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?(?:start|resume)\s+listening$", re.IGNORECASE),
]

# While muted EVERYTHING is ignored anyway, so the only job left is to catch the
# resume word. This unanchored matcher finds "unmute" anywhere in an utterance or
# interim (e.g. "okay, unmute"), so resuming never depends on perfect STT
# segmentation. Only consulted in the muted state.
_UNMUTE_LOOSE = re.compile(
    r"\b(?:un[\s-]?mute(?:d)?|(?:start|resume)\s+listening)\b", re.IGNORECASE
)


def wants_unmute(text: str) -> bool:
    """True if ``text`` contains a resume word anywhere (muted-state use only)."""
    return bool(_UNMUTE_LOOSE.search(_clean(text)))


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

        # Muted: the mic keeps listening but the ONLY thing that can happen is a
        # resume. Match "unmute" loosely so it works even if STT bundles it into
        # a longer utterance; ignore everything else.
        if self.muted:
            if wants_unmute(text):
                state = self.set_muted(False)
                return self._decision(False, False, "", state.changed)
            return self._decision(False, False, "", False)

        control = classify_control(text)
        if control is not None:
            state = self.set_muted(control == "mute")
            return self._decision(False, False, "", state.changed)

        return self._decision(True, True, text, False)

    def process_interim(self, transcript: str) -> Optional[VoiceState]:
        """Muted-state fast path: resume as soon as an interim shows "unmute".

        Returns the new state when it unmutes, else None. Interims are otherwise
        display-only (and suppressed entirely while muted).
        """
        if self.muted and wants_unmute(transcript):
            return self.set_muted(False)
        return None

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
