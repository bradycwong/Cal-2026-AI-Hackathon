"""Paste-to-import protocol creation.

A deterministic side endpoint: pasted prose -> the canonical protocol YAML shape
-> written to ``data/protocols/`` -> re-loaded through the same loader the files
use -> registered in ``state.protocols`` so it shows up in ``GET /api/protocols``.

LLM-assisted when available, with a network-free deterministic fallback so the
feature works with no API key. The LLM is read-only: it only proposes structure,
the backend assigns ids and writes the file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from . import router
from .instrumentation import llm_span
from .state import Protocol, SessionState, load_protocol_file


class ParsedStep(BaseModel):
    text: str
    duration_s: Optional[int] = None
    timer_label: Optional[str] = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class ParsedProtocol(BaseModel):
    name: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    steps: list[ParsedStep] = Field(default_factory=list)


IMPORT_SYSTEM_PROMPT = (
    "You convert a pasted laboratory protocol into structured JSON.\n"
    "- Split the protocol into ATOMIC steps: one operator-visible action per step "
    "(add, mix, invert, centrifuge, incubate, transfer, wash, discard, elute, place, "
    "remove, heat, cool, wait, ...). When one sentence joins multiple actions with "
    "'and', 'then', 'before', 'after', 'followed by', 'while', or a semicolon, split "
    "them into separate steps whenever the operator would perform them separately.\n"
    "- Preserve the author's original order exactly; never invent, merge away, or drop "
    "an action.\n"
    "- Set duration_s (in seconds) ONLY on the specific action that needs a timer or "
    "deadline (e.g. centrifuge 45 seconds, incubate 10 minutes, wait 5 minutes). Do "
    "NOT attach a timer to a neighbouring prep action just because a later action is "
    "timed. Set timer_label (e.g. 'centrifuge', 'incubation') only when duration_s is "
    "set; otherwise leave both null.\n"
    "- Use only these parameter keys when a value is explicitly present: volume_ul, "
    "temp_c, speed_g, cycles, reagent. Never invent missing times, temperatures, "
    "speeds, volumes, or reagents.\n"
    "- Use ASCII units: write 'uL' (not the micro sign) and 'degrees C' (not the "
    "degree sign). Return a one-line description and 1 to 4 short lowercase aliases. "
    "Do not return an id; the backend assigns it.\n"
    "Return ONLY a single JSON object and nothing else -- no markdown, no code fences, "
    "no commentary. Shape: {\"name\": str, \"description\": str, \"aliases\": [str], "
    "\"steps\": [{\"text\": str, \"duration_s\": int|null, \"timer_label\": str|null, "
    "\"parameters\": {}}]}."
)


def _import_mode() -> str:
    return os.getenv("IMPORT_MODE", "auto").lower()


# --- deterministic fallback -------------------------------------------------
_NUM_MARKER = re.compile(r"^\s*(?:\d+[.)]|[-*\u2022])\s*")


def _fallback_parse(text: str, name: Optional[str]) -> ParsedProtocol:
    # Split on lines first: normalize_ascii() collapses newlines into spaces, so
    # normalizing the whole blob up front would fuse every step into one.
    steps: list[ParsedStep] = []
    for raw_line in text.splitlines():
        line = router.normalize_ascii(_NUM_MARKER.sub("", raw_line).strip())
        if not line:
            continue
        duration = router._parse_duration(line)
        timer_label = "timer" if duration else None
        steps.append(ParsedStep(text=line, duration_s=duration, timer_label=timer_label))
    final_name = (name or "").strip()
    if not final_name:
        final_name = steps[0].text if steps else "Imported Protocol"
    return ParsedProtocol(name=final_name, steps=steps)


# --- LLM path ---------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _assistant_text(resp: Any) -> str:
    """Concatenate the text blocks of an Anthropic response (mirrors the ask path)."""
    parts: list[str] = []
    for block in getattr(resp, "content", []):
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a model reply that may be fenced or prefixed."""
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


def _verb_label(text: str) -> str:
    """Derive a timer label from a step's leading verb, e.g. 'Centrifuge ...' -> 'centrifuge'."""
    match = re.search(r"[A-Za-z]+", text)
    return match.group(0).lower() if match else "timer"


def _validate_parsed(raw_json: str, name: Optional[str]) -> ParsedProtocol:
    """Validate the model's JSON into a ParsedProtocol and apply the safety layer.

    Raises on invalid JSON / schema (pydantic) or an empty step list, so ``auto``
    mode falls back to the deterministic parser instead of writing junk.
    """
    parsed = ParsedProtocol.model_validate_json(_extract_json(raw_json))
    if not parsed.steps:
        raise ValueError("LLM returned a protocol with no steps")
    for step in parsed.steps:
        step.text = router.normalize_ascii(step.text)
        if step.duration_s:
            step.timer_label = step.timer_label or _verb_label(step.text)
        else:
            # Keep the contract: a label is only meaningful on a timed step.
            step.timer_label = None
    if name and name.strip():
        parsed.name = name.strip()
    return parsed


def _llm_parse(text: str, name: Optional[str]) -> ParsedProtocol:
    import anthropic  # lazy: the deterministic path needs no dependency

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    model = os.getenv("IMPORT_MODEL", router.LAB_MODEL)
    hint = f"Suggested name: {name}\n\n" if name else ""
    messages = [{"role": "user", "content": hint + text}]
    # ``messages.parse`` does not exist on the pinned anthropic 0.40.0, so ask for
    # strict JSON via ``messages.create`` and validate it ourselves. Traced like the
    # ask path; the span is a no-op under pytest / when tracing is off.
    with llm_span(
        "import_protocol",
        model=model,
        system=IMPORT_SYSTEM_PROMPT,
        user_messages=messages,
    ) as span:
        resp = client.messages.create(
            model=model,
            max_tokens=1800,
            system=IMPORT_SYSTEM_PROMPT,
            messages=messages,
        )
        raw = _assistant_text(resp)
        span.record_response(raw, usage=getattr(resp, "usage", None))
    return _validate_parsed(raw, name)


def _parse(text: str, name: Optional[str]) -> ParsedProtocol:
    mode = _import_mode()
    if mode == "deterministic":
        return _fallback_parse(text, name)
    if mode == "llm":
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise ValueError("IMPORT_MODE=llm but no ANTHROPIC_API_KEY is configured")
        return _llm_parse(text, name)
    # auto: LLM when available, deterministic on any failure
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            return _llm_parse(text, name)
        except Exception:
            return _fallback_parse(text, name)
    return _fallback_parse(text, name)


# --- filesystem helpers -----------------------------------------------------
def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "imported_protocol"


def _unique_slug(slug: str, proto_dir: Path, state: SessionState) -> str:
    candidate = slug
    n = 2
    while (proto_dir / f"{candidate}.yaml").exists() or candidate in state.protocols:
        candidate = f"{slug}_{n}"
        n += 1
    return candidate


def _write_yaml(path: Path, document: dict[str, Any]) -> None:
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    tmp.replace(path)


# --- orchestrator -----------------------------------------------------------
def import_protocol(
    text: str, name: Optional[str], state: SessionState
) -> tuple[Protocol, Path]:
    if not text or not text.strip():
        raise ValueError("protocol text is empty")

    parsed = _parse(text, name)
    if not parsed.steps:
        raise ValueError("no steps could be parsed from the pasted protocol")

    proto_dir = state.data_dir / "protocols"
    proto_dir.mkdir(parents=True, exist_ok=True)
    slug = _unique_slug(_slugify(parsed.name), proto_dir, state)

    steps_doc = []
    for i, step in enumerate(parsed.steps, start=1):
        step_doc: dict[str, Any] = {
            "id": i,
            "text": step.text,
            "duration_s": step.duration_s,
            "parameters": step.parameters or {},
        }
        if step.timer_label:
            step_doc["timer_label"] = step.timer_label
        steps_doc.append(step_doc)

    document = {
        "protocol": {
            "id": slug,
            "name": parsed.name,
            "version": "1.0",
            "description": parsed.description,
            "category": "Imported",
            "status": "READY",
            "aliases": [a.strip().lower() for a in parsed.aliases if a.strip()],
            "steps": steps_doc,
        }
    }

    path = proto_dir / f"{slug}.yaml"
    _write_yaml(path, document)
    # Validate-by-loading through the canonical gate before registering.
    load_protocol_file(path)
    proto = state.register_protocol(path)
    return proto, path
