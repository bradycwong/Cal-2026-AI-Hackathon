"""router.py — transcript -> ONE validated Command.

This is the ONLY place an LLM is allowed to run. Everything downstream is
deterministic. The router is provider-abstracted behind ``route(transcript)``;
swapping Claude for another vendor touches only this file.

Two layers, by design (per the user's directive: "API weirdness should not kill
the pitch"):

* **Primary — LLM** (Anthropic ``messages.parse`` -> a typed ``Command``).
* **Fallback — deterministic** (``deterministic_route``): a tiny regex parser that
  covers the five exact demo lines + obvious variants, with no network and no
  dependencies. Used automatically when there is no API key, when the SDK is
  missing, or when the LLM call raises.

``ROUTER_MODE`` env selects behaviour: ``auto`` (default), ``llm``, ``deterministic``.
All matching happens on an ASCII-normalized transcript (``uL``, ``degrees C``).
"""

from __future__ import annotations

import os
import re
from typing import Optional

from .schema import Command

LAB_MODEL = os.getenv("LAB_MODEL", "claude-haiku-4-5")
ROUTER_MODE = os.getenv("ROUTER_MODE", "auto").lower()

SYSTEM_PROMPT = (
    "You convert a single spoken lab command into ONE structured Command. "
    "Choose exactly one intent from: load_protocol, next_step, log_entry, "
    "start_timer, find_inventory, unknown. "
    "If the intent is clear but a required parameter is missing or ambiguous, do "
    "NOT guess: leave that field null (or return intent='unknown') and put a "
    "one-line question in clarify_prompt. Never invent a protocol name, reagent, "
    "duration, or sample id that was not said. "
    "Use ASCII units: write 'uL' (not the micro sign) and 'degrees C'."
)


# --- ASCII normalization ----------------------------------------------------

_MICRO = "\u00b5\u03bc"  # MICRO SIGN, GREEK SMALL LETTER MU


def normalize_ascii(text: str) -> str:
    """Collapse unit spellings to one ASCII form so asserts/matches are stable.

    "200 microliters" / "200 uL" / "200 µL" -> "200 uL"; degree forms -> "degrees C".
    Display text may keep pretty symbols; machine-read text uses this.
    """
    if text is None:
        return ""
    s = text
    for ch in _MICRO:
        s = s.replace(ch + "l", "uL").replace(ch + "L", "uL").replace(ch, "u")
    s = re.sub(r"\bmicrolit(?:er|re)s?\b", "uL", s, flags=re.IGNORECASE)
    s = re.sub(r"\bul\b", "uL", s)
    s = re.sub(r"\b(\d+)\s*uL", r"\1 uL", s)
    s = s.replace("\u2103", " degrees C")  # ℃
    s = re.sub(r"\u00b0\s*C\b", " degrees C", s)  # °C
    s = re.sub(r"\bdegrees?\s+celsius\b", "degrees C", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# --- deterministic fallback (the five demo lines + obvious variants) --------

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30,
    "forty": 40, "forty-five": 45, "sixty": 60,
}


def _to_int(token: str) -> Optional[int]:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _NUM_WORDS.get(token)


def _parse_duration(text: str) -> Optional[int]:
    m = re.search(
        r"(\d+|[a-z\-]+)\s*[- ]?\s*(minute|minutes|min|second|seconds|sec)\b",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    n = _to_int(m.group(1))
    if n is None:
        return None
    unit = m.group(2).lower()
    return n * 60 if unit.startswith("min") else n


def _parse_sample_id(text: str) -> Optional[str]:
    m = re.search(r"\bsample\s+([A-Za-z0-9]+)\b", text, flags=re.IGNORECASE)
    return m.group(1).upper() if m else None


def _parse_timer_label(text: str) -> str:
    m = re.search(r"\b(?:a|an)?\s*([A-Za-z]+)\s+timer\b", text, flags=re.IGNORECASE)
    if m and m.group(1).lower() not in {"minute", "second", "the", "new", "start", "stop"}:
        return m.group(1).lower()
    return "timer"


def deterministic_route(transcript: str) -> Command:
    """Network-free parser for the demo set. Never raises; worst case -> unknown."""
    raw = normalize_ascii(transcript)
    t = raw.lower()

    if not t:
        return Command(intent="unknown", clarify_prompt="I didn't catch that. Could you repeat?")

    # log_entry — "log: ...", "note: ...", "log that ..."
    m = re.match(r"^\s*(?:log|note)\b[:,]?\s*(?:that\s+)?(.*)$", raw, flags=re.IGNORECASE)
    if m:
        body = m.group(1).strip()
        if not body:
            return Command(intent="log_entry", clarify_prompt="What would you like to log?")
        sample = _parse_sample_id(body)
        return Command(intent="log_entry", log_text=body, sample_id=sample)

    # start_timer — must come before find/guide so "start a timer" wins
    if "timer" in t or re.search(r"\bstart (?:a |an )?\d", t):
        dur = _parse_duration(raw)
        if dur is None:
            # No spoken duration -> let the handler start the current step's
            # declared timer (only clarifies if there's no timed step).
            lbl = _parse_timer_label(raw)
            return Command(intent="start_timer", timer_label=None if lbl == "timer" else lbl)
        return Command(intent="start_timer", duration_s=dur, timer_label=_parse_timer_label(raw))

    # find_inventory — "where is/where's X", "location of X", "find X"
    m = re.search(
        r"(?:where(?:'s| is| are)|location of|find(?: the)?|do we have)\s+(?:the\s+)?(.+?)\s*\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if m:
        reagent = m.group(1).strip().rstrip("?.")
        if not reagent:
            return Command(intent="find_inventory", clarify_prompt="Which reagent are you looking for?")
        return Command(intent="find_inventory", reagent_name=reagent)

    # next_step — "what's next", "next step", "next"
    if re.search(r"\b(what'?s next|next step|move on|advance)\b", t) or t in {"next", "next."}:
        return Command(intent="next_step")

    # load_protocol — "load <name> protocol" / "load protocol <name>" / "load <name>"
    m = re.match(r"^\s*(?:load|open|start)\s+(.*)$", raw, flags=re.IGNORECASE)
    if m:
        rest = m.group(1).strip().rstrip(".")
        rest = re.sub(r"^the\s+", "", rest, flags=re.IGNORECASE)
        name = re.sub(r"\bprotocol\b", "", rest, flags=re.IGNORECASE).strip()
        # "load a protocol" with no actual name -> do not guess
        if not name or name.lower() in {"a", "the", "protocol", ""}:
            # Handler fills the authoritative "Available: …" list from state.
            return Command(
                intent="load_protocol",
                clarify_prompt="Which protocol would you like to load?",
            )
        return Command(intent="load_protocol", protocol_name=name)

    # load_protocol with the verb dropped by STT — "<name> protocol" still names
    # the protocol explicitly (not a guess: the name is literally spoken).
    m = re.match(r"^\s*(.+?)\s+protocol\b\.?$", raw, flags=re.IGNORECASE)
    if m:
        name = re.sub(r"^(?:the|a|an)\s+", "", m.group(1).strip(), flags=re.IGNORECASE)
        if name and name.lower() not in {"a", "the", "this", "that", "next", "another"}:
            return Command(intent="load_protocol", protocol_name=name)

    return Command(
        intent="unknown",
        clarify_prompt="Sorry, I didn't understand that. Try 'load DNA extraction protocol'.",
    )


# --- LLM primary ------------------------------------------------------------


def _llm_route(transcript: str) -> Command:
    """Anthropic structured-output call. Raises on any problem so route() can fall back."""
    import anthropic  # imported lazily so the deterministic path needs no dependency

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    resp = client.messages.parse(
        model=LAB_MODEL,
        max_tokens=512,
        system=SYSTEM_PROMPT,
        output_format=Command,
        messages=[{"role": "user", "content": normalize_ascii(transcript)}],
    )
    parsed = resp.parsed_output
    if not isinstance(parsed, Command):
        raise ValueError("router: LLM did not return a Command")
    if parsed.log_text:
        parsed.log_text = normalize_ascii(parsed.log_text)
    return parsed


def _llm_available() -> bool:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except Exception:
        return False
    return True


def route(transcript: str) -> Command:
    """transcript -> Command. LLM primary, deterministic fallback. Never raises."""
    mode = ROUTER_MODE
    if mode == "deterministic" or (mode == "auto" and not _llm_available()):
        return deterministic_route(transcript)
    try:
        return _llm_route(transcript)
    except Exception:
        # API weirdness must not kill the pitch — fall back to the demo parser.
        return deterministic_route(transcript)
