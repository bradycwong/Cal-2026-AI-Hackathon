"""state.py — in-memory SessionState + file loaders.

Holds everything deterministic handlers mutate: the active protocol + cursor, the
log, and active timers. The log is the one persisted organ: pass ``db_path`` and
appends are written to SQLite (``backend/db.py``) and reloaded on startup so the
feed survives refresh/restart. With ``db_path=None`` (tests) it's pure in-memory.
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from .db import NoteStore

DATA_DIR = Path(__file__).parent / "data"


@dataclass
class Step:
    id: int
    text: str
    duration_s: Optional[int] = None
    timer_label: Optional[str] = None  # label for the auto-timer on timed steps
    parameters: dict[str, Any] = field(default_factory=dict)

    def as_event(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text}


@dataclass
class Protocol:
    id: str
    name: str
    steps: list[Step]
    aliases: list[str] = field(default_factory=list)


@dataclass
class InventoryItem:
    name: str
    location: str
    quantity_approx: str
    notes: str = ""


@dataclass
class Timer:
    timer_id: str
    label: str
    duration_s: int
    started_at: float
    expired: bool = False

    def remaining_s(self) -> int:
        rem = self.duration_s - (time.monotonic() - self.started_at)
        return max(0, int(round(rem)))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_protocol_file(path: Path) -> Protocol:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    p = raw["protocol"]
    steps = [
        Step(
            id=int(s["id"]),
            text=str(s["text"]),
            duration_s=s.get("duration_s"),
            timer_label=s.get("timer_label"),
            parameters=s.get("parameters") or {},
        )
        for s in p["steps"]
    ]
    return Protocol(
        id=str(p["id"]),
        name=str(p["name"]),
        steps=steps,
        aliases=[a.lower() for a in (p.get("aliases") or [])],
    )


def load_inventory_file(path: Path) -> list[InventoryItem]:
    items: list[InventoryItem] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            items.append(
                InventoryItem(
                    name=row["name"].strip(),
                    location=row.get("location", "").strip(),
                    quantity_approx=row.get("quantity_approx", "").strip(),
                    notes=row.get("notes", "").strip(),
                )
            )
    return items


class SessionState:
    """Singleton-per-session state. One protocol loaded at a time (v1)."""

    def __init__(self, data_dir: Path = DATA_DIR, db_path: Optional[str] = None) -> None:
        self.data_dir = data_dir
        self.protocols: dict[str, Protocol] = {}
        self.inventory: list[InventoryItem] = []
        self.active_protocol: Optional[Protocol] = None
        self.current_step_index: int = -1
        self.log: list[dict[str, Any]] = []
        self.timers: list[Timer] = []
        self._log_seq = 0
        self._timer_seq = 0
        self.notes = NoteStore(db_path) if db_path else None

    def load_files(self) -> None:
        proto_dir = self.data_dir / "protocols"
        for path in sorted(proto_dir.glob("*.yaml")):
            proto = load_protocol_file(path)
            self.protocols[proto.id] = proto
        inv_path = self.data_dir / "inventory.csv"
        if inv_path.exists():
            self.inventory = load_inventory_file(inv_path)
        if self.notes is not None:
            self.log = self.notes.all_notes()
            self._log_seq = self.log[-1]["id"] if self.log else 0

    # --- protocol cursor ---------------------------------------------------
    def find_protocol(self, name: str) -> Optional[Protocol]:
        key = name.strip().lower()
        for proto in self.protocols.values():
            if key == proto.id.lower() or key == proto.name.lower():
                return proto
            if key in proto.aliases:
                return proto
        # loose contains-match so "dna extraction" matches "DNA Extraction"
        for proto in self.protocols.values():
            if key in proto.name.lower() or proto.name.lower() in key:
                return proto
        return None

    def current_step(self) -> Optional[Step]:
        if not self.active_protocol or self.current_step_index < 0:
            return None
        if self.current_step_index >= len(self.active_protocol.steps):
            return None
        return self.active_protocol.steps[self.current_step_index]

    def step_at(self, index: int) -> Optional[Step]:
        if not self.active_protocol:
            return None
        if 0 <= index < len(self.active_protocol.steps):
            return self.active_protocol.steps[index]
        return None

    # --- log ---------------------------------------------------------------
    def append_log(self, text: str, sample_id: Optional[str]) -> dict[str, Any]:
        step = self.current_step()
        timestamp = utc_now_iso()
        step_ref = step.id if step else None
        if self.notes is not None:
            entry = self.notes.add_note(text, timestamp, sample_id, step_ref)
            self._log_seq = entry["id"]
        else:
            self._log_seq += 1
            entry = {
                "id": self._log_seq,
                "text": text,
                "timestamp": timestamp,
                "sample_id": sample_id,
                "step_ref": step_ref,
            }
        self.log.append(entry)
        return entry

    # --- timers ------------------------------------------------------------
    def add_timer(self, duration_s: int, label: str) -> Timer:
        self._timer_seq += 1
        timer = Timer(
            timer_id=f"t{self._timer_seq}",
            label=label,
            duration_s=duration_s,
            started_at=time.monotonic(),
        )
        self.timers.append(timer)
        return timer

    def clear_timers(self) -> None:
        self.timers.clear()
