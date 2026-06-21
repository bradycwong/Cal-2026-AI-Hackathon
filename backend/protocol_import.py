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
    "You convert a pasted laboratory protocol into structured JSON. Rules: "
    "preserve the author's step order exactly; never invent, merge away, or drop "
    "steps; one action per step. Use only these parameter keys when a value is "
    "explicitly present: volume_ul, temp_c, speed_g, cycles, reagent. Use ASCII "
    "units (write uL, not the micro sign; write 'degrees C', not the degree sign). "
    "Set duration_s only when the step states a wait/incubation time, and set "
    "timer_label only when duration_s is set. Return a one-line description and 1 "
    "to 4 short lowercase aliases. Do not return an id; the backend assigns it."
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
def _llm_parse(text: str, name: Optional[str]) -> ParsedProtocol:
    import anthropic  # lazy: the deterministic path needs no dependency

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    hint = f"Suggested name: {name}\n\n" if name else ""
    resp = client.messages.parse(
        model=router.LAB_MODEL,
        max_tokens=1024,
        system=IMPORT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": hint + text}],
        response_format=ParsedProtocol,
    )
    parsed = resp.parsed
    if name and name.strip():
        parsed.name = name.strip()
    return parsed


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
