"""wake.py — hands-free wake-word gate for the voice channel.

Voice is always-on while a session is armed, but we should only ACT on speech
addressed to the assistant. This gate decides, per finished utterance, whether it
is a command:

* "Hey Lab, what's next?"  -> route "what's next?"            (wake + command)
* "Hey Lab."               -> open a short follow-up window    (bare wake)
* "what's next?"  (<=window after a wake) -> route it           (follow-up)
* "...background chatter..." (no wake, no window) -> ignore      (not for us)

Typed input bypasses this entirely — typing is already an explicit address.

The wake word is runtime-configurable (UI / POST /api/config), so the defaults
below are only the starting values; see ``config``.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

# Optional greeting before the wake word: "hey lab", "ok lab", bare "lab".
_GREET = r"(?:hey|hi|hello|ok|okay|yo)?"
# STT routinely hears a wake word as a near-homophone; accept those so the demo
# isn't held hostage by one mistranscribed syllable. Keyed by the chosen word.
_HOMOPHONES = {
    "lab": ["labb", "labh"],
    "jarvis": ["jervis", "darvis"],
    "computer": ["computor"],
}


def _split_aliases(raw: str) -> list[str]:
    return [a.strip().lower() for a in raw.split(",") if a.strip()]


def _derive_aliases(word: str) -> list[str]:
    """Default alias set for a wake word: itself + known homophones."""
    return sorted({word, *_HOMOPHONES.get(word, [])})


@dataclass
class WakeConfig:
    """Mutable wake settings shared across sessions; recompiles its regex on change."""

    word: str = "lab"
    aliases: list[str] = field(default_factory=lambda: _derive_aliases("lab"))
    required: bool = True
    window_s: float = 8.0
    _re: re.Pattern[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._recompile()

    @classmethod
    def from_env(cls) -> "WakeConfig":
        word = os.getenv("LAB_WAKE_WORD", "lab").strip().lower() or "lab"
        env_aliases = os.getenv("LAB_WAKE_ALIASES")
        aliases = (
            sorted({word, *_split_aliases(env_aliases)})
            if env_aliases is not None
            else _derive_aliases(word)
        )
        required = os.getenv("LAB_WAKE_REQUIRED", "true").lower() not in {"false", "0", "no"}
        window_s = float(os.getenv("LAB_WAKE_WINDOW_S", "8"))
        return cls(word=word, aliases=aliases, required=required, window_s=window_s)

    def _recompile(self) -> None:
        alts = sorted({self.word, *self.aliases}, key=len, reverse=True)
        alt = "|".join(re.escape(a) for a in alts if a)
        self._re = re.compile(
            rf"^\s*{_GREET}[\s,.:;!-]*(?:{alt})\b[\s,.:;!-]*(.*)$",
            flags=re.IGNORECASE | re.DOTALL,
        )

    def update(
        self,
        *,
        word: str | None = None,
        aliases: list[str] | None = None,
        required: bool | None = None,
        window_s: float | None = None,
    ) -> None:
        word_changed = False
        if word is not None:
            cleaned = word.strip().lower()
            if cleaned and cleaned != self.word:
                self.word = cleaned
                word_changed = True
        if aliases is not None:
            self.aliases = [a.strip().lower() for a in aliases if a.strip()]
        elif word_changed:
            # New word, no explicit aliases -> reset to that word's homophones so
            # the previous wake word stops triggering.
            self.aliases = _derive_aliases(self.word)
        if required is not None:
            self.required = required
        if window_s is not None:
            self.window_s = max(0.0, float(window_s))
        self._recompile()

    def as_dict(self) -> dict[str, object]:
        return {
            "word": self.word,
            "aliases": sorted({self.word, *self.aliases}),
            "required": self.required,
            "window_s": self.window_s,
        }


# Process-wide config (one assistant identity per server).
config = WakeConfig.from_env()


@dataclass
class WakeDecision:
    should_route: bool
    command_text: str
    just_woke: bool  # bare wake word -> we opened the listening window


class WakeGate:
    """Per-connection wake state. One instance per /ws/audio session."""

    def __init__(self, cfg: WakeConfig | None = None) -> None:
        self._cfg = cfg or config
        self._open_until = 0.0

    def process(self, transcript: str, *, now: float | None = None) -> WakeDecision:
        now = time.monotonic() if now is None else now
        text = (transcript or "").strip()
        if not self._cfg.required:
            return WakeDecision(should_route=bool(text), command_text=text, just_woke=False)

        m = self._cfg._re.match(text)
        if m:
            command = m.group(1).strip()
            self._open_until = now + self._cfg.window_s
            if command:
                return WakeDecision(True, command, just_woke=False)
            return WakeDecision(False, "", just_woke=True)  # bare wake -> listening

        if now < self._open_until:
            self._open_until = now + self._cfg.window_s  # refresh on each follow-up
            return WakeDecision(True, text, just_woke=False)

        return WakeDecision(False, "", just_woke=False)  # not addressed to us
