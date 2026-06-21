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
    started_at: Optional[float] = None        # monotonic while running; None while paused
    remaining_at_pause: Optional[int] = None  # frozen remaining while paused
    expired: bool = False

    @property
    def paused(self) -> bool:
        return self.started_at is None and not self.expired

    def remaining_s(self) -> int:
        if self.started_at is None:  # paused / not yet started -> frozen value
            return self.remaining_at_pause if self.remaining_at_pause is not None else self.duration_s
        rem = self.duration_s - (time.monotonic() - self.started_at)
        return max(0, int(round(rem)))

    def start(self) -> None:
        """Begin (or resume) the countdown from the current frozen remaining."""
        if self.started_at is not None or self.expired:
            return
        if self.remaining_at_pause is not None:
            self.duration_s = self.remaining_at_pause
            self.remaining_at_pause = None
        self.started_at = time.monotonic()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ProtocolParseError(ValueError):
    """Raised when an uploaded/loaded protocol document is malformed."""


def parse_protocol_text(text: str) -> Protocol:
    """Validate a protocol YAML *document* (the same shape files use) -> Protocol.

    Shared by the on-disk loader and the upload endpoint so a file dropped in
    ``data/protocols`` and a file POSTed to ``/api/protocols`` are validated by
    exactly one code path. Raises ``ProtocolParseError`` on any malformed input.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProtocolParseError(f"not valid YAML: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("protocol"), dict):
        raise ProtocolParseError("missing top-level 'protocol:' mapping")
    p = raw["protocol"]
    for key in ("id", "name", "steps"):
        if not p.get(key):
            raise ProtocolParseError(f"protocol is missing required field '{key}'")
    if not isinstance(p["steps"], list):
        raise ProtocolParseError("'steps' must be a list")
    steps: list[Step] = []
    for s in p["steps"]:
        if not isinstance(s, dict) or s.get("id") is None or not s.get("text"):
            raise ProtocolParseError("each step needs an 'id' and 'text'")
        try:
            step_id = int(s["id"])
        except (TypeError, ValueError) as exc:
            raise ProtocolParseError(f"step id {s.get('id')!r} is not an integer") from exc
        steps.append(
            Step(
                id=step_id,
                text=str(s["text"]),
                duration_s=s.get("duration_s"),
                timer_label=s.get("timer_label"),
                parameters=s.get("parameters") or {},
            )
        )
    return Protocol(
        id=str(p["id"]),
        name=str(p["name"]),
        steps=steps,
        aliases=[str(a).lower() for a in (p.get("aliases") or [])],
    )


def load_protocol_file(path: Path) -> Protocol:
    return parse_protocol_text(path.read_text(encoding="utf-8"))


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

    # --- mutating writers (upload protocol / add inventory item) -----------
    def add_inventory_item(
        self, name: str, location: str = "", quantity_approx: str = "", notes: str = ""
    ) -> InventoryItem:
        """Append a manually-entered item to memory and persist it to the CSV.

        The CSV is the file-driven inventory store; appending here mirrors how
        ``load_inventory_file`` reads it back, so the item survives a restart.
        """
        name = name.strip()
        if not name:
            raise ValueError("inventory item requires a name")
        item = InventoryItem(
            name=name,
            location=location.strip(),
            quantity_approx=quantity_approx.strip(),
            notes=notes.strip(),
        )
        self.inventory.append(item)
        inv_path = self.data_dir / "inventory.csv"
        write_header = not inv_path.exists() or inv_path.stat().st_size == 0
        inv_path.parent.mkdir(parents=True, exist_ok=True)
        with inv_path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if write_header:
                writer.writerow(["name", "location", "quantity_approx", "notes"])
            writer.writerow([item.name, item.location, item.quantity_approx, item.notes])
        return item

    def add_protocol_from_text(self, text: str) -> Protocol:
        """Validate an uploaded protocol document, register it, and persist it.

        Saved as ``data/protocols/<id>.yaml`` (same dir ``load_files`` globs) so
        it is loadable immediately and after a restart. Re-uploading the same id
        overwrites it. Raises ``ProtocolParseError`` for malformed input.
        """
        proto = parse_protocol_text(text)
        proto_dir = self.data_dir / "protocols"
        proto_dir.mkdir(parents=True, exist_ok=True)
        (proto_dir / f"{proto.id}.yaml").write_text(text, encoding="utf-8")
        self.protocols[proto.id] = proto
        return proto

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

    def pop_log(self) -> Optional[dict[str, Any]]:
        if not self.log:
            return None
        entry = self.log.pop()
        if self.notes is not None:
            self.notes.delete_note(int(entry["id"]))
        return entry

    def update_last_log(self, text: str) -> Optional[dict[str, Any]]:
        if not self.log:
            return None
        entry = self.log[-1]
        entry["text"] = text
        if self.notes is not None:
            self.notes.update_text(int(entry["id"]), text)
        return entry

    # --- timers ------------------------------------------------------------
    def add_timer(self, duration_s: int, label: str, *, paused: bool = False) -> Timer:
        self._timer_seq += 1
        timer = Timer(
            timer_id=f"t{self._timer_seq}",
            label=label,
            duration_s=duration_s,
            started_at=None if paused else time.monotonic(),
            remaining_at_pause=duration_s if paused else None,
        )
        self.timers.append(timer)
        return timer

    def start_pending_timer(self) -> Optional[Timer]:
        """Resume the most recently added paused timer, if one is waiting."""
        for timer in reversed(self.timers):
            if timer.paused:
                timer.start()
                return timer
        return None

    def clear_timers(self) -> None:
        self.timers.clear()
