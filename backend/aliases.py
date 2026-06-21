"""aliases.py — user-defined trigger -> built-in command expansion.

Custom voice commands are authored in the browser (the Commands page) and synced
here so the ingestion spine can expand a spoken/typed TRIGGER into a real
built-in command phrase BEFORE routing. That is what lets "fire it up" execute
"load DNA extraction protocol" with no "I didn't understand that" detour — on
every input channel (typed box + live mic) and every page.

Design notes:

* The store is process-wide (this is a single-tenant demo) and in-memory. The
  browser re-syncs its localStorage copy on every Commands-page load, so a server
  restart just needs that page opened once to re-register the aliases.
* Expansion is exact-match on a NORMALIZED phrase. ``normalize`` MUST stay in
  lockstep with the frontend's ``normalize()`` so a trigger typed in the browser
  and the spoken transcript collapse to the same key.
* It never invents behaviour: an alias only ever maps to a phrase the router
  already understands, so the existing deterministic/LLM routing stays the single
  source of truth for what a command does.
"""

from __future__ import annotations

import re
from typing import Optional


def normalize(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace.

    Mirrors the frontend ``normalize()`` exactly (same character class) so the
    stored trigger and the heard transcript hash to the same key.
    """
    s = (text or "").lower()
    s = re.sub(r"[.,!?;:'\"]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


class AliasStore:
    """Holds the current trigger -> phrase map for the session."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def set_all(self, items: list[dict]) -> int:
        """Replace the whole alias set from ``[{trigger, phrase}, ...]``.

        Blank triggers/phrases are dropped; later duplicates win. Returns the
        number of aliases stored.
        """
        new_map: dict[str, str] = {}
        for item in items or []:
            trigger = normalize(str((item or {}).get("trigger", "")))
            phrase = str((item or {}).get("phrase", "") or "").strip()
            if trigger and phrase:
                new_map[trigger] = phrase
        self._map = new_map
        return len(self._map)

    def expand(self, text: str) -> Optional[str]:
        """Return the mapped command phrase for ``text``, or ``None`` if no alias."""
        return self._map.get(normalize(text))

    def as_list(self) -> list[dict]:
        return [{"trigger": trigger, "phrase": phrase} for trigger, phrase in self._map.items()]

    def __len__(self) -> int:
        return len(self._map)
