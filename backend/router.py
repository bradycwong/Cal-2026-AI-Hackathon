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
    "Choose exactly one intent from: load_protocol, next_step, prev_step, "
    "repeat_step, log_entry, undo_log, correct_log, start_timer, stop_timer, "
    "find_inventory, ask, unknown. "
    "Map 'stop timer', 'cancel the timer', 'stop the alarm', or 'stop beeping' "
    "to stop_timer. "
    "Map 'go back' or 'previous step' to prev_step. "
    "Map 'repeat that', 'say that again', or 'what step am I on' to repeat_step. "
    "Map 'scratch that', 'delete that', or 'undo the last note' to undo_log. "
    "Map 'change the last note to X' or 'correct that to X' to correct_log "
    "with log_text set to X. "
    "Map open protocol questions to ask with question set to the user's words. "
    "Map natural note phrasings such as 'make a note that X', 'record X', "
    "'write down X', 'note down X', and 'remark X' to log_entry with log_text "
    "set to X. "
    "If the intent is clear but a required parameter is missing or ambiguous, do "
    "NOT guess: leave that field null (or return intent='unknown') and put a "
    "one-line question in clarify_prompt. Never invent a protocol name, reagent, "
    "duration, sample id, note text, or question that was not said. "
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

    # log_entry - "log X", "note X", "record X", "make a note that X"
    m = re.match(
        r"^\s*(?:"
        r"log|note|record|remark|write down|note down|"
        r"(?:make|add|take|jot)\s+(?:a\s+)?note"
        r")\b[:,]?\s*(?:that\s+)?(.*)$",
        raw,
        flags=re.IGNORECASE,
    )
    if m:
        body = m.group(1).strip()
        if not body:
            return Command(intent="log_entry", clarify_prompt="What would you like to log?")
        sample = _parse_sample_id(body)
        return Command(intent="log_entry", log_text=body, sample_id=sample)

    # undo/correct log - these start with amend verbs, so they do not collide with log_entry.
    if re.search(
        r"\b(scratch that|delete (that|the last|my last)( note| entry)?|"
        r"undo (that|the last)( note| entry)?)\b",
        t,
    ):
        return Command(intent="undo_log")

    m = re.match(
        r"^\s*(?:correct|change|make)\s+(?:that|the last (?:note|entry))\s+to\s+(.+)$",
        raw,
        flags=re.IGNORECASE,
    )
    if m:
        replacement = normalize_ascii(m.group(1).strip().rstrip("."))
        if not replacement:
            return Command(
                intent="correct_log",
                clarify_prompt="What should I change the last note to?",
            )
        return Command(intent="correct_log", log_text=replacement)

    # stop_timer — silence the alarm / cancel timers. MUST precede start_timer
    # because "stop timer" also contains "timer".
    if re.search(
        r"\b(?:stop|cancel|dismiss|silence|kill|end)\s+"
        r"(?:the\s+|that\s+|all\s+|this\s+|my\s+)*"
        r"(?:timers?|alarms?|beep(?:ing|s)?|countdowns?)\b",
        t,
    ):
        return Command(intent="stop_timer")

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

    # backward/repeat navigation - must run before the generic next_step check.
    if re.search(r"\b(go back|previous step|back a step|step back|previous)\b", t):
        return Command(intent="prev_step")

    if re.search(
        r"\b(repeat( that| the step| this)?|say that again|"
        r"what step (am i|are we) on|current step|read (it|that) again)\b",
        t,
    ):
        return Command(intent="repeat_step")

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

    # ask - last-resort question catch. Earlier command patterns win first.
    if re.match(r"^\s*(how|what|why|which|when|does|is|are|can)\b", t):
        return Command(intent="ask", question=raw)

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


def _step_context(protocol) -> str:
    rows: list[str] = []
    for step in protocol.steps:
        params = ""
        if step.parameters:
            pairs = [f"{key}={value}" for key, value in sorted(step.parameters.items())]
            params = " Parameters: " + ", ".join(pairs)
        rows.append(f"Step {step.id}: {step.text}{params}")
    return "\n".join(rows)


def _question_tokens(text: str) -> set[str]:
    stop = {
        "a", "an", "and", "are", "can", "does", "how", "i", "in", "is", "it",
        "much", "of", "on", "or", "step", "the", "to", "what", "when",
        "where", "which", "why",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", normalize_ascii(text).lower())
        if token not in stop and len(token) > 1
    }


def _fallback_answer_question(question: str, protocol) -> str:
    q_tokens = _question_tokens(question)
    best_step = None
    best_score = 0
    for step in protocol.steps:
        haystack = f"{step.id} {step.text}"
        if step.parameters:
            haystack += " " + " ".join(str(v) for v in step.parameters.values())
        score = len(q_tokens & _question_tokens(haystack))
        if score > best_score:
            best_score = score
            best_step = step
    if best_step and best_score > 0:
        return f"Step {best_step.id}: {best_step.text}"
    return "I can only answer detailed questions when the AI model is connected."


def answer_question(question: str, protocol) -> str:
    """Answer a read-only protocol question.

    This is a second model call, but it is gated behind an explicit `ask` command.
    With no key, missing SDK, or provider error, it falls back to deterministic
    step matching and stays free.
    """
    clean_question = normalize_ascii(question)
    if not _llm_available():
        return _fallback_answer_question(clean_question, protocol)
    try:
        import anthropic

        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=LAB_MODEL,
            max_tokens=160,
            system=(
                "Answer ONLY from the protocol steps below in 1-2 sentences. "
                "If the answer is not in the steps, say you do not see it.\n\n"
                f"{_step_context(protocol)}"
            ),
            messages=[{"role": "user", "content": clean_question}],
        )
        parts: list[str] = []
        for block in getattr(resp, "content", []):
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        answer = normalize_ascii(" ".join(parts))
        return answer or _fallback_answer_question(clean_question, protocol)
    except Exception:
        return _fallback_answer_question(clean_question, protocol)


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
