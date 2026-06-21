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
from .instrumentation import llm_span

LAB_MODEL = os.getenv("LAB_MODEL", "claude-haiku-4-5")
ROUTER_MODE = os.getenv("ROUTER_MODE", "auto").lower()

SYSTEM_PROMPT = (
    "You convert a single spoken lab command into ONE structured Command. "
    "Choose exactly one intent from: load_protocol, next_step, skip_step, "
    "prev_step, repeat_step, log_entry, undo_log, correct_log, start_timer, "
    "stop_timer, clear_done_timers, find_inventory, add_inventory, ask, "
    "show_protocol, unknown. "
    "Map 'next', 'next step', 'what's next', 'move on', 'continue', 'proceed', "
    "'advance', 'confirm', 'confirm action', 'done', 'step done', 'complete', "
    "'mark complete', or 'finished' to next_step — the user has completed the "
    "current step and wants to advance (this marks the step done). "
    "IMPORTANT: question-phrased variants such as 'what is the next step', "
    "'what will the next step be', 'what does the next step say', or 'can you "
    "tell me the next step' are read-only questions — map them to ask with "
    "question set to the user's words, NOT to next_step. Only map to next_step "
    "when the user is signalling completion/readiness to advance, not when they "
    "are asking for information. "
    "Map 'skip', 'skip this step', 'skip step', or 'skip ahead' to skip_step "
    "(advances WITHOUT marking the step done). "
    "Map 'stop timer', 'cancel the timer', 'stop the alarm', or 'stop beeping' "
    "to stop_timer. "
    "Map 'clear done timers', 'delete all finished timers', 'remove expired "
    "timers', or 'clear the timers that are done' to clear_done_timers — this "
    "removes ONLY finished/expired timers and leaves running ones running. (Plain "
    "'stop timer' / 'cancel all timers' with NO done-qualifier stays stop_timer.) "
    "For start_timer, set duration_s to the TOTAL number of seconds in the "
    "spoken duration, summing every part. Examples: '5 minutes' -> 300; "
    "'30 seconds' -> 30; 'a minute' -> 60; 'a minute 30' / 'one minute thirty "
    "seconds' / '1:30' -> 90; 'two and a half minutes' -> 150; 'half an hour' "
    "-> 1800; 'an hour and a half' -> 5400. If no duration is spoken, leave "
    "duration_s null so the current step's declared time is used. "
    "Map 'go back' or 'previous step' to prev_step. "
    "Map 'repeat that' or 'say that again' to repeat_step. "
    "Map 'jump to guide', 'go to the protocol', 'back to the guide', 'show me the "
    "protocol', or 'what step am I on' to show_protocol (navigate to the "
    "running-protocol view). "
    "Map 'go to <page>', 'open the <page>', 'show <page>' (page in dashboard, "
    "protocols, notebook, inventory, commands), plus 'help' / 'what can I say' to "
    "navigate_page with the field 'page' set to that page key (e.g. 'go to the "
    "notebook' -> navigate_page, page='notebook'). 'help'/'what can I say' -> "
    "page='commands'. The guide itself stays show_protocol, not navigate_page. "
    "Map 'scratch that', 'delete that', or 'undo the last note' to undo_log. "
    "Map 'change the last note to X' or 'correct that to X' to correct_log "
    "with log_text set to X. "
    "Map open protocol questions to ask with question set to the user's words. "
    "Map natural note phrasings such as 'make a note that X', 'record X', "
    "'write down X', 'note down X', and 'remark X' to log_entry with log_text "
    "set to X. "
    "Map 'add X to the inventory', 'put X in inventory', 'register reagent X', or "
    "'add a new reagent X' to add_inventory. Extract these fields when spoken: "
    "reagent_name (the item's name, e.g. 'EDTA'), amount (the numeric quantity as "
    "a string with no unit, e.g. '5'), unit (the unit alone, e.g. 'g', 'mL', "
    "'uL'), and location (where it's stored, e.g. 'shelf 4', 'Fridge 1'). For "
    "add_inventory the item NAME is required: if no name is clearly stated, return "
    "intent='unknown' with a clarify_prompt asking for the reagent name. Treat "
    "'inv' as a synonym for 'inventory'. Leave any "
    "other missing field null (do NOT guess a quantity, unit, or location that was "
    "not said). "
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

# Spelled-out numbers for the duration parser (covers 0-99 via tens + ones).
_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9,
}
_TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}

# Duration units -> seconds. Listed longest-first in the regex so "minutes"
# wins over "min", etc.
_DURATION_UNITS = {
    "hours": 3600, "hour": 3600, "hrs": 3600, "hr": 3600,
    "minutes": 60, "minute": 60, "mins": 60, "min": 60,
    "seconds": 1, "second": 1, "secs": 1, "sec": 1,
}
_UNIT_RE = (
    r"hours?|hrs?|minutes?|mins?|seconds?|secs?"
)


def _unit_seconds(unit: str) -> int:
    return _DURATION_UNITS[unit.lower()]


def _num_token(token: str) -> Optional[float]:
    """A single digit/spelled number token -> float, else None."""
    t = token.strip(",.").lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", t):
        return float(t)
    if t in _ONES:
        return float(_ONES[t])
    if t in _TEENS:
        return float(_TEENS[t])
    if t in _TENS:
        return float(_TENS[t])
    return None


def _expand_halves(text: str) -> str:
    """Rewrite "half" phrases into explicit numeric amounts.

    Runs BEFORE number-word/"a"->1 substitution so it can see the original
    wording: "two and a half minutes" -> "2.5 minutes", "a minute and a half" ->
    "a minute 30 seconds", "half an hour" -> "0.5 hour".
    """
    def _and_a_half_num(m: re.Match) -> str:
        n = _num_token(m.group(1))
        if n is None:
            return m.group(0)
        return f"{n + 0.5:g} {m.group(2)}"

    def _and_a_half_unit(m: re.Match) -> str:
        secs = _unit_seconds(m.group(1))
        if secs == 3600:
            return f"{m.group(1)} 30 minutes"
        if secs == 60:
            return f"{m.group(1)} 30 seconds"
        return f"{m.group(1)} 0.5 seconds"

    # "<n> and a half <unit>" -> "<n.5> <unit>".
    text = re.sub(
        rf"\b([\w.]+)\s+and\s+(?:a\s+)?half\s+({_UNIT_RE})\b",
        _and_a_half_num,
        text,
        flags=re.IGNORECASE,
    )
    # "<unit> and a half" -> "<unit> <half-of-unit>".
    text = re.sub(
        rf"\b({_UNIT_RE})\s+and\s+(?:a\s+)?half\b",
        _and_a_half_unit,
        text,
        flags=re.IGNORECASE,
    )
    # "half a(n) <unit>" / "half <unit>" -> "0.5 <unit>".
    text = re.sub(
        rf"\bhalf\s+(?:an?\s+)?({_UNIT_RE})\b",
        r"0.5 \1",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _replace_number_words(text: str) -> str:
    """Turn spelled-out numbers into digits so the scanner only sees digits.

    Handles compound tens ("twenty five" -> "25") and treats "a"/"an" before a
    duration unit as 1 ("a minute" -> "1 minute"). Non-number tokens pass
    through untouched.
    """
    words = text.split()
    out: list[str] = []
    i = 0
    while i < len(words):
        raw = words[i]
        w = raw.strip(",.").lower()
        nxt = words[i + 1].strip(",.").lower() if i + 1 < len(words) else ""
        if w in _TENS:
            val = _TENS[w]
            if nxt in _ONES and _ONES[nxt] != 0:
                val += _ONES[nxt]
                i += 1
            out.append(str(val))
        elif w in _TEENS:
            out.append(str(_TEENS[w]))
        elif w in _ONES:
            out.append(str(_ONES[w]))
        elif w in {"a", "an"} and re.fullmatch(_UNIT_RE, nxt):
            out.append("1")
        else:
            out.append(raw)
        i += 1
    return " ".join(out)


def _parse_duration(text: str) -> Optional[int]:
    """Parse an arbitrary spoken/typed duration into whole seconds.

    Supports compound durations ("1 minute 30 seconds", "a minute thirty",
    "two and a half minutes"), clock notation ("1:30", "1:05:00"), fractions
    ("1.5 minutes", "half a minute") and bare trailing seconds after a minute
    ("a minute 30"). Returns None when no duration is present.
    """
    t = text.lower().strip()

    # Clock notation: mm:ss or hh:mm:ss.
    m = re.search(r"(?<!\d)(\d{1,3}):([0-5]?\d)(?::([0-5]?\d))?(?!\d)", t)
    if m:
        nums = [int(g) for g in m.groups() if g is not None]
        if len(nums) == 3:
            total = nums[0] * 3600 + nums[1] * 60 + nums[2]
        else:
            total = nums[0] * 60 + nums[1]
        return total or None

    t = _expand_halves(t)
    t = _replace_number_words(t)

    pat = re.compile(rf"(\d+(?:\.\d+)?)[\s-]*({_UNIT_RE})\b")
    total = 0.0
    found = False
    last_unit: Optional[str] = None
    last_end = 0
    for mm in pat.finditer(t):
        total += float(mm.group(1)) * _unit_seconds(mm.group(2))
        found = True
        last_unit = mm.group(2)
        last_end = mm.end()

    if not found:
        return None

    # Bare trailing number after the last unit names the next-smaller unit:
    # "1 minute 30" -> 30 seconds, "1 hour 30" -> 30 minutes.
    if last_unit is not None and _unit_seconds(last_unit) >= 60:
        tail = re.match(
            rf"\s+(?:and\s+)?(\d+(?:\.\d+)?)\b(?!\s*-?\s*(?:{_UNIT_RE}))",
            t[last_end:],
        )
        if tail:
            sub = 1 if _unit_seconds(last_unit) == 60 else 60
            total += float(tail.group(1)) * sub

    return int(round(total)) or None


def _parse_sample_id(text: str) -> Optional[str]:
    m = re.search(r"\bsample\s+([A-Za-z0-9]+)\b", text, flags=re.IGNORECASE)
    return m.group(1).upper() if m else None


def _parse_timer_label(text: str) -> str:
    m = re.search(r"\b(?:a|an)?\s*([A-Za-z]+)\s+timer\b", text, flags=re.IGNORECASE)
    if m and m.group(1).lower() not in {
        "a", "an", "the", "for", "minute", "minutes", "second", "seconds",
        "hour", "hours", "half", "new", "start", "set", "stop", "another",
        "begin", "launch", "restart", "run", "countdown", "now", "please",
        "this", "that", "my", "our",
    }:
        return m.group(1).lower()
    return "timer"


# Reagent-prep modal control. Verbs that introduce an absolute sample count
# ("set/scale/use ... N samples"); a bare "<N> samples" also counts.
_SAMPLE_VERB_RE = r"\b(?:set|change|adjust|make|use|run|scale|prep|prepare)\b"


def _parse_prep_command(text: str) -> Optional[Command]:
    """Recognize reagent-prep modal commands, else None.

    Two shapes: an absolute sample-count change ("set samples to 24", "scale to 8
    samples", "32 samples") or an explicit confirm/close ("close prep", "looks
    good"). Sample counts are absolute only and never guessed -- a set/scale
    phrase with no number returns a clarify Command instead of inventing a count.
    """
    t = text.lower().strip()

    # confirm_prep -- explicit "close the prep and begin" phrases that do NOT
    # already mean next_step ("done"/"confirm") or load_protocol ("start
    # protocol"); those close the modal at the handler layer when it is open.
    if re.search(
        r"\b(?:close|dismiss|hide)\s+(?:the\s+)?prep\b"
        r"|\bprep(?:\s+(?:is|looks))?\s+(?:good|done|ready|complete)\b"
        r"|\b(?:reagents?|they)\s+look\s+good\b"
        r"|\blooks\s+good\b"
        r"|\b(?:i'?m|i am)\s+ready\b|\bready\s+to\s+(?:start|go|begin)\b",
        t,
    ):
        return Command(intent="confirm_prep")

    # set_sample_count -- needs "sample(s)"/"sample count" plus either a set/scale
    # verb or a leading "<N> samples". Questions ("how many samples", "where are
    # the samples") are left for ask/find_inventory further down.
    s = _replace_number_words(t)
    if not re.search(r"\bsamples?\b|\bsample count\b", s):
        return None
    if re.match(
        r"^\s*(?:how|what|why|which|when|who|do|does|is|are|can|could|should|would|where)\b",
        s,
    ):
        return None
    is_set = re.search(_SAMPLE_VERB_RE, s) is not None
    is_bare = re.match(r"^\s*\d+\s+samples?\b", s) is not None
    if not (is_set or is_bare):
        return None
    m = re.search(r"\b(\d+)\b", s)
    if not m:
        return Command(
            intent="set_sample_count",
            clarify_prompt="How many samples? For example, 'set samples to 24'.",
        )
    return Command(intent="set_sample_count", sample_count=int(m.group(1)))


# Quantity + unit for add_inventory ("5g", "5 grams", "100 uL"). Spelled-out
# units are canonicalized to their short form so the stored row stays tidy.
_INV_UNIT_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*"
    r"(milligrams?|micrograms?|kilograms?|grams?|milliliters?|millilitres?|"
    r"microliters?|microlitres?|liters?|litres?|mg|kg|ug|ng|g|ml|ul|cl|dl|l|"
    r"units?|mol|mmol|nmol)\b",
    flags=re.IGNORECASE,
)

_UNIT_CANON = {
    "milligram": "mg", "milligrams": "mg",
    "microgram": "ug", "micrograms": "ug",
    "kilogram": "kg", "kilograms": "kg",
    "gram": "g", "grams": "g",
    "milliliter": "mL", "milliliters": "mL", "millilitre": "mL", "millilitres": "mL",
    "microliter": "uL", "microliters": "uL", "microlitre": "uL", "microlitres": "uL",
    "liter": "L", "liters": "L", "litre": "L", "litres": "L",
    "ml": "mL", "ul": "uL", "l": "L",
}


def _canon_unit(unit: str) -> str:
    return _UNIT_CANON.get(unit.lower(), unit)


def _parse_add_inventory(body: str) -> Command:
    """Pull reagent_name/amount/unit/location out of the phrase between 'add' and
    'to the inventory'. Name is required; anything not said stays null (the handler
    fills TBD). Never guesses a value that was not spoken."""
    text = body.strip().rstrip(".")
    amount = unit = location = None

    # Strip any "expires ..." phrase first so its trailing date isn't captured as
    # the location. Expiration is no longer parsed or stored, so the value is dropped.
    m = re.search(r"\b(?:that\s+)?(?:expir\w*|exp)\b\s*(?:on|date|in|:)?\s*(.+)$", text, flags=re.IGNORECASE)
    if m:
        text = text[: m.start()].strip()

    # Location: "on shelf 4", "in Fridge 1", "at bench 3".
    m = re.search(r"\b(?:on|in|at|inside|from)\s+(?:the\s+)?(.+)$", text, flags=re.IGNORECASE)
    if m:
        location = m.group(1).strip().rstrip(".") or None
        text = text[: m.start()].strip()

    # Amount + unit ("5g", "5 grams", "100 uL").
    m = _INV_UNIT_RE.search(text)
    if m:
        amount = m.group(1)
        unit = _canon_unit(m.group(2))
        text = (text[: m.start()] + " " + text[m.end():]).strip()

    # Whatever remains, minus filler, is the reagent name.
    text = re.sub(r"\b(?:of|a|an|the|new|some|reagent|item|called|named)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[,:]", " ", text)
    name = re.sub(r"\s+", " ", text).strip() or None

    if not name:
        return Command(
            intent="unknown",
            clarify_prompt="What's the name of the reagent to add to the inventory?",
        )
    return Command(
        intent="add_inventory",
        reagent_name=name,
        amount=amount,
        unit=unit,
        location=location,
    )


def deterministic_route(transcript: str) -> Command:
    """Network-free parser for the demo set. Never raises; worst case -> unknown."""
    raw = normalize_ascii(transcript)
    t = raw.lower()

    if not t:
        return Command(intent="unknown", clarify_prompt="I didn't catch that. Could you repeat?")

    # add_inventory - "add 5g of EDTA on shelf 4 to the inventory". Must precede
    # log_entry/find so the "inventory" verb wins; requires the word "inventory"
    # (or its common shorthand "inv").
    m = re.match(
        r"^\s*(?:add|create|register|put|store|stock)\s+(.*?)\s+"
        r"(?:to|into|in|on)\s+(?:the\s+|my\s+|our\s+)?(?:inventory|inv)\b.*$",
        raw,
        flags=re.IGNORECASE,
    )
    if m:
        return _parse_add_inventory(m.group(1))

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

    # reagent prep modal — "set samples to 24", "scale to 8 samples", "close
    # prep", "looks good". Runs after log/correct (so a note that mentions
    # samples is still logged) and before the timer/step blocks (so "set 24
    # samples" isn't read as a timer and "prep done" isn't read as next_step).
    prep = _parse_prep_command(raw)
    if prep is not None:
        return prep

    # clear_done_timers — dismiss ONLY finished/expired timers. MUST precede
    # stop_timer: stop_timer's verbs (stop/cancel/dismiss) + "all timers" would
    # otherwise steal "dismiss done timers" and wrongly remove RUNNING timers too.
    # The explicit done-qualifier gate lets qualifier-less "stop/cancel timers"
    # fall through to stop_timer below.
    _DONE = r"done|finished|complete|completed|expired|over|elapsed|ended"
    if re.search(
        r"\b(?:clear|delete|remove|dismiss|cancel|clean up|clear out|get rid of)\s+"
        r"(?:the\s+|all\s+(?:the\s+)?|my\s+|these\s+|those\s+|any\s+)*"
        r"(?:"
        rf"(?:{_DONE})\s+(?:timers?|alarms?|countdowns?)"          # "done timers"
        r"|"
        rf"(?:timers?|alarms?|countdowns?)\s+(?:that|which)\s+"     # "timers that are done"
        rf"(?:are\s+|have\s+|has\s+|is\s+)?(?:{_DONE})"
        r")\b",
        t,
    ):
        return Command(intent="clear_done_timers")

    # stop_timer — silence the alarm / cancel timers. MUST precede start_timer
    # because "stop timer" also contains "timer".
    if re.search(
        r"\b(?:stop|cancel|dismiss|silence|kill|end)\s+"
        r"(?:the\s+|that\s+|all\s+|this\s+|my\s+)*"
        r"(?:timers?|alarms?|beep(?:ing|s)?|countdowns?)\b",
        t,
    ):
        return Command(intent="stop_timer")

    # start_timer — must come before find/guide so "start a timer" wins. Also
    # accept "countdown" and begin/set verbs so STT variants ("start countdown",
    # "begin timer") aren't mis-read as a protocol load. ("stop ... countdown" is
    # caught by stop_timer above.)
    if (
        "timer" in t
        or re.search(r"\b(?:start|begin|set|launch|restart|run|new)\b[\w\s]*\bcountdown\b", t)
        or re.search(r"\b(?:start|begin|set)\s+(?:a |an )?\d", t)
    ):
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

    # cancel_protocol — "cancel/stop the protocol", "stop the run", "abort". MUST run
    # before the load_protocol fallback ("<name> protocol"), which would otherwise
    # read "cancel the protocol" as loading a protocol named "cancel". The timer
    # matchers above already claimed "stop/cancel ... timer" (they require a timer
    # noun), so only the protocol/run object reaches here.
    if re.search(
        r"\b(?:cancel|stop|abort|end|quit|exit|unload|halt)\s+"
        r"(?:the\s+|this\s+|current\s+|my\s+)*"
        r"(?:protocol|run)\b",
        t,
    ):
        return Command(intent="cancel_protocol")

    # navigate_page — hands-free "go to <page>" for the standalone pages. Runs
    # before show_protocol so plural "protocols"/"protocol library" land on the
    # library page (singular protocol/guide/run stays the guide jump below) and
    # before the `ask` catch so "what can I say"/"help" reach the Commands page.
    # Nav verbs deliberately exclude where/find/location and cancel/stop, so
    # find_inventory and cancel_protocol keep their own phrasings.
    nav_verb = (
        r"(?:(?:go|jump|navigate|switch|head|take\s+me|bring\s+me)"
        r"(?:\s+(?:back|over))?\s+to|open|show(?:\s+me)?|view)"
    )
    for page_key, noun in (
        ("dashboard", r"dashboard|home(?:\s*page|\s*screen)?"),
        ("protocols", r"protocols|protocol\s+library|library"),
        ("notebook", r"(?:lab\s+)?notebook|my\s+notes|notes"),
        ("inventory", r"inventory|stock(?:\s*room)?|reagent\s+list"),
        ("commands", r"(?:voice\s+)?commands?(?:\s+(?:page|list|reference))?|command\s+list"),
    ):
        if re.search(rf"\b{nav_verb}\s+(?:the\s+|my\s+)?(?:{noun})\b", t):
            return Command(intent="navigate_page", page=page_key)
    # Verb-less aliases people actually say.
    if re.search(r"\bgo\s+home\b|\bhome\s+page\b", t) or t.strip() in {"home", "dashboard"}:
        return Command(intent="navigate_page", page="dashboard")
    if re.search(r"\bprotocol\s+library\b", t):
        return Command(intent="navigate_page", page="protocols")
    if re.search(r"\b(?:help|what can i (?:say|do))\b", t) or t.strip() in {"commands", "command list"}:
        return Command(intent="navigate_page", page="commands")

    # show_protocol — hands-free "take me to the live run" (the guide view). Runs
    # before prev_step/repeat_step so "back to the guide" and "what step am i on"
    # navigate instead of being read as a step nav. Network-free control intent.
    if re.search(
        r"\b("
        r"jump to (?:the )?(?:run|guide|protocol)|"
        r"(?:go|back|take me|bring me|get) (?:back )?to (?:the )?(?:run|guide|protocol)|"
        r"show (?:me )?(?:the )?(?:current step|protocol|guide|run)|"
        r"what step (?:am i|are we) on"
        r")\b",
        t,
    ):
        return Command(intent="show_protocol")

    # backward/repeat navigation - must run before the generic next_step check.
    if re.search(r"\b(go back|previous step|back a step|step back|previous)\b", t):
        return Command(intent="prev_step")

    if re.search(
        r"\b(repeat( that| the step| this)?|say that again|"
        r"current step|read (it|that) again)\b",
        t,
    ):
        return Command(intent="repeat_step")

    # skip_step — "skip", "skip this step". Advances but logs the step Skipped
    # (not Completed). Must precede next_step so it isn't swallowed by "advance".
    if re.search(r"\bskip\b", t):
        return Command(intent="skip_step")

    # Question-phrased "next step" requests -> ask (read-only, cursor stays put).
    # "What IS the next step" asks for information; it must NOT advance the step
    # counter. This check runs before the next_step regex so "next step" inside
    # a question doesn't get swallowed by the nav pattern below.
    if re.search(
        r"\bwhat\s+(?:is|are|will|would|does|do)\s+(?:the\s+)?next\s+step\b"
        r"|\btell\s+me\s+(?:about\s+)?(?:the\s+)?next\s+step\b"
        r"|\bdescribe\s+(?:the\s+)?next\s+step\b"
        r"|\bwhat\s+(?:is|are)\s+(?:the\s+)?(?:step\s+)?(?:after|following)\s+this\b",
        t,
    ):
        return Command(intent="ask", question=raw)

    # next_step — "what's next", "next step", "next", plus confirmation/completion
    # phrasings that all mean "I'm done with this step, advance". This runs AFTER
    # log_entry/skip/etc., so a note that merely mentions "done"/"complete" is
    # logged (the leading verb wins) and "skip" still advances without marking done.
    if re.search(
        r"\b("
        r"what'?s next|next step|move on|moving on|advance|proceed|continue|"
        r"confirm(?:ed)?(?:\s+(?:action|step|this|that))?|"
        r"mark(?:\s+(?:it|this|the\s+step))?\s+(?:done|complete|completed)|"
        r"(?:step\s+)?(?:done|complete|completed|finished)"
        r")\b",
        t,
    ) or t in {"next", "next."}:
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
        clarify_prompt="Sorry, I didn't understand that. Check the Commands page to see what you can say.",
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
        system_prompt = (
            "Answer ONLY from the protocol steps below in 1-2 sentences. "
            "If the answer is not in the steps, say you do not see it.\n\n"
            f"{_step_context(protocol)}"
        )
        messages = [{"role": "user", "content": clean_question}]
        # Manual LLM span (the auto-instrumentor is unusable on the pinned SDK);
        # nests under the ingest CHAIN span. No-op when tracing is off.
        with llm_span(
            "answer_question", model=LAB_MODEL, system=system_prompt, user_messages=messages
        ) as span:
            resp = client.messages.create(
                model=LAB_MODEL,
                max_tokens=160,
                system=system_prompt,
                messages=messages,
            )
            parts: list[str] = []
            for block in getattr(resp, "content", []):
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
            answer = normalize_ascii(" ".join(parts))
            span.record_response(answer, usage=getattr(resp, "usage", None))
        return answer or _fallback_answer_question(clean_question, protocol)
    except Exception:
        return _fallback_answer_question(clean_question, protocol)


# Unambiguous lab controls: matched deterministically BEFORE any LLM call so
# timers + step navigation never depend on (or wait on) the model.
_CONTROL_INTENTS = frozenset(
    {"start_timer", "stop_timer", "clear_done_timers", "next_step", "prev_step",
     "repeat_step", "skip_step", "show_protocol", "cancel_protocol", "navigate_page",
     "set_sample_count", "confirm_prep"}
)


def control_route(transcript: str) -> Optional[Command]:
    """High-confidence control fast path: returns a Command for a reliable control
    intent, else None. Delegates to ``deterministic_route`` so the existing match
    priority holds — e.g. "log that I started the timer" matches log_entry first
    and returns None here (deferred to the LLM)."""
    cmd = deterministic_route(transcript)
    return cmd if cmd.intent in _CONTROL_INTENTS else None


def route(transcript: str) -> Command:
    """transcript -> Command. A deterministic control fast path runs first (timers
    + step navigation never touch the LLM); everything else is LLM-primary with a
    deterministic fallback. Never raises."""
    control = control_route(transcript)
    if control is not None:
        return control
    mode = ROUTER_MODE
    if mode == "deterministic" or (mode == "auto" and not _llm_available()):
        return deterministic_route(transcript)
    try:
        return _llm_route(transcript)
    except Exception:
        # API weirdness must not kill the pitch — fall back to the demo parser.
        return deterministic_route(transcript)
