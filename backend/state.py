"""state.py — in-memory SessionState + file loaders.

Holds everything deterministic handlers mutate: the active protocol + cursor, the
in-memory log, and active timers. SQLite is a deferred swappable organ — the log
already uses the persisted field names (id, text, timestamp, sample_id, step_ref)
so a DB is a drop-in later.
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

DATA_DIR = Path(__file__).parent / "data"


@dataclass
class Step:
    id: int
    text: str
    duration_s: Optional[int] = None
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
    raw = yaml.safe_load(path.read_text())
    p = raw["protocol"]
    steps = [
        Step(
            id=int(s["id"]),
            text=str(s["text"]),
            duration_s=s.get("duration_s"),
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
    with path.open(newline="") as fh:
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

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = data_dir
        self.protocols: dict[str, Protocol] = {}
        self.inventory: list[InventoryItem] = []
        self.active_protocol: Optional[Protocol] = None
        self.current_step_index: int = -1
        self.log: list[dict[str, Any]] = []
        self.timers: list[Timer] = []
        self._log_seq = 0
        self._timer_seq = 0

    def load_files(self) -> None:
        proto_dir = self.data_dir / "protocols"
        for path in sorted(proto_dir.glob("*.yaml")):
            proto = load_protocol_file(path)
            self.protocols[proto.id] = proto
        inv_path = self.data_dir / "inventory.csv"
        if inv_path.exists():
            self.inventory = load_inventory_file(inv_path)

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
        self._log_seq += 1
        step = self.current_step()
        entry = {
            "id": self._log_seq,
            "text": text,
            "timestamp": utc_now_iso(),
            "sample_id": sample_id,
            "step_ref": step.id if step else None,
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
