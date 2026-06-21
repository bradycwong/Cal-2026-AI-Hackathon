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

from .db import _DEFAULT_NOTEBOOK_NAME, NoteStore

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
    description: str = ""
    category: str = "General"
    status: str = "READY"
    est_duration_min: Optional[int] = None
    reagents: list[str] = field(default_factory=list)


@dataclass
class InventoryItem:
    name: str
    location: str
    quantity_approx: str
    notes: str = ""
    code: str = ""
    category: str = "General"
    date: str = ""
    expiration: str = ""
    status: str = "ok"


_PROTOCOL_STATUSES = {"READY", "LOW_REAGENTS", "ARCHIVED"}
_INVENTORY_STATUSES = {"ok", "low", "critical", "expiring"}


def _duration_label(seconds: int) -> str:
    if seconds <= 0:
        return "-"
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, rem = divmod(minutes, 60)
    return f"{hours}h" if rem == 0 else f"{hours}h {rem}m"


def _derive_reagents(steps: list[Step]) -> list[str]:
    """Unique, author-ordered reagents pulled from step parameters."""
    seen: list[str] = []
    for step in steps:
        reagent = step.parameters.get("reagent")
        if reagent and str(reagent) not in seen:
            seen.append(str(reagent))
    return seen


def _derive_est_duration_min(steps: list[Step]) -> Optional[int]:
    total = sum(s.duration_s for s in steps if s.duration_s)
    return max(1, round(total / 60)) if total else None


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
    status = str(p.get("status", "READY")).upper()
    if status not in _PROTOCOL_STATUSES:
        status = "READY"
    reagents = [str(r) for r in (p.get("reagents") or [])] or _derive_reagents(steps)
    est_duration_min = p.get("est_duration_min")
    if est_duration_min is None:
        est_duration_min = _derive_est_duration_min(steps)
    else:
        est_duration_min = int(est_duration_min)
    return Protocol(
        id=str(p["id"]),
        name=str(p["name"]),
        steps=steps,
        aliases=[str(a).lower() for a in (p.get("aliases") or [])],
        description=str(p.get("description", "")),
        category=str(p.get("category", "General")),
        status=status,
        est_duration_min=est_duration_min,
        reagents=reagents,
    )


def load_protocol_file(path: Path) -> Protocol:
    return parse_protocol_text(path.read_text(encoding="utf-8"))


def load_inventory_file(path: Path) -> list[InventoryItem]:
    items: list[InventoryItem] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            status = (row.get("status") or "ok").strip().lower()
            if status not in _INVENTORY_STATUSES:
                status = "ok"
            items.append(
                InventoryItem(
                    name=row["name"].strip(),
                    location=row.get("location", "").strip(),
                    quantity_approx=row.get("quantity_approx", "").strip(),
                    notes=row.get("notes", "").strip(),
                    code=(row.get("code") or "").strip(),
                    category=(row.get("category") or "General").strip() or "General",
                    date=(row.get("date") or "").strip(),
                    expiration=(row.get("expiration") or "").strip(),
                    status=status,
                )
            )
    return items


_INVENTORY_COLUMNS = [
    "name", "location", "quantity_approx", "notes", "code", "category", "date",
    "expiration", "status",
]


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
        # Multi-notebook log. DB-backed: the NoteStore owns notebooks + the
        # active pointer. In-memory (tests): mirror that with plain dicts/lists,
        # and ``self.log`` always *is* the active notebook's list.
        self.active_notebook_id: int = 0
        self._notebooks: list[dict[str, Any]] = []          # in-memory only
        self._notebook_notes: dict[int, list[dict[str, Any]]] = {}  # in-memory only
        self._notebook_seq = 0
        if self.notes is not None:
            self.active_notebook_id = int(self.notes.get_active_notebook_id())
            self.log = self.notes.all_notes(self.active_notebook_id)
        else:
            default = self._create_notebook_inmem(_DEFAULT_NOTEBOOK_NAME)
            self.active_notebook_id = default["id"]
            self.log = self._notebook_notes[default["id"]]

    def load_files(self) -> None:
        proto_dir = self.data_dir / "protocols"
        for path in sorted(proto_dir.glob("*.yaml")):
            proto = load_protocol_file(path)
            self.protocols[proto.id] = proto
        inv_path = self.data_dir / "inventory.csv"
        if inv_path.exists():
            self.inventory = load_inventory_file(inv_path)
        if self.notes is not None:
            self.active_notebook_id = int(self.notes.get_active_notebook_id())
            self.log = self.notes.all_notes(self.active_notebook_id)
            last = self.notes.all_notes()  # global max id keeps ids monotonic
            self._log_seq = last[-1]["id"] if last else 0

    # --- snapshot helpers (read-only views for the REST endpoints) ---------
    def protocol_catalog(self) -> list[dict[str, Any]]:
        """Read-only summary of every loaded protocol for ``GET /api/protocols``."""
        catalog: list[dict[str, Any]] = []
        for proto in self.protocols.values():
            est_min = proto.est_duration_min
            duration_s = est_min * 60 if est_min else _derive_est_duration_min(proto.steps)
            duration_s = (duration_s or 0) if isinstance(duration_s, int) else 0
            if est_min is None and duration_s:
                est_min = max(1, round(duration_s / 60))
            reagents = proto.reagents or _derive_reagents(proto.steps)
            catalog.append(
                {
                    "id": proto.id,
                    "name": proto.name,
                    "description": proto.description,
                    "category": proto.category,
                    "status": proto.status,
                    "est_duration_min": est_min,
                    "duration_s": duration_s,
                    "duration_label": _duration_label(duration_s),
                    "step_count": len(proto.steps),
                    "reagents": reagents,
                    "aliases": proto.aliases,
                }
            )
        return catalog

    def inventory_view(self) -> list[dict[str, Any]]:
        """Read-only view of inventory for ``GET /api/inventory``."""
        return [
            {
                "name": item.name,
                "code": item.code,
                "location": item.location,
                "category": item.category,
                "quantity_approx": item.quantity_approx,
                "date": item.date,
                "expiration": item.expiration,
                "status": item.status,
                "notes": item.notes,
            }
            for item in self.inventory
        ]

    # --- mutating writers (upload protocol / add inventory item) -----------
    def add_inventory_item(
        self,
        name: str,
        location: str = "",
        quantity_approx: str = "",
        notes: str = "",
        code: str = "",
        category: str = "General",
        date: str = "",
        expiration: str = "",
        status: str = "ok",
    ) -> InventoryItem:
        """Append a manually-entered item to memory and persist it to the CSV.

        The CSV is the file-driven inventory store; appending here mirrors how
        ``load_inventory_file`` reads it back, so the item survives a restart.
        """
        name = name.strip()
        if not name:
            raise ValueError("inventory item requires a name")
        status = (status or "ok").strip().lower()
        if status not in _INVENTORY_STATUSES:
            status = "ok"
        item = InventoryItem(
            name=name,
            location=location.strip(),
            quantity_approx=quantity_approx.strip(),
            notes=notes.strip(),
            code=code.strip(),
            category=(category or "General").strip() or "General",
            date=date.strip(),
            expiration=expiration.strip(),
            status=status,
        )
        self.inventory.append(item)
        inv_path = self.data_dir / "inventory.csv"
        write_header = not inv_path.exists() or inv_path.stat().st_size == 0
        inv_path.parent.mkdir(parents=True, exist_ok=True)
        with inv_path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if write_header:
                writer.writerow(_INVENTORY_COLUMNS)
            writer.writerow([
                item.name, item.location, item.quantity_approx, item.notes,
                item.code, item.category, item.date, item.expiration, item.status,
            ])
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

    def register_protocol(self, path: Path) -> Protocol:
        """Validate a protocol file through the canonical loader and register it."""
        proto = load_protocol_file(path)
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

    # --- notebooks ---------------------------------------------------------
    def _create_notebook_inmem(self, name: str) -> dict[str, Any]:
        self._notebook_seq += 1
        nb = {"id": self._notebook_seq, "name": name, "created_at": utc_now_iso()}
        self._notebooks.append(nb)
        self._notebook_notes[nb["id"]] = []
        return nb

    def _notebook_id_set(self) -> set[int]:
        if self.notes is not None:
            return self.notes.notebook_ids()
        return {nb["id"] for nb in self._notebooks}

    def notebooks_view(self) -> list[dict[str, Any]]:
        """Read-only list of notebooks (with entry counts + active flag)."""
        if self.notes is not None:
            nbs = self.notes.list_notebooks()
        else:
            nbs = [
                {**nb, "entry_count": len(self._notebook_notes[nb["id"]])}
                for nb in self._notebooks
            ]
        for nb in nbs:
            nb["active"] = nb["id"] == self.active_notebook_id
        return nbs

    def create_notebook(self, name: str) -> dict[str, Any]:
        """Create a notebook and make it active (so new logs land in it)."""
        name = (name or "").strip() or "Untitled Notebook"
        if self.notes is not None:
            nb = self.notes.create_notebook(name)
        else:
            nb = self._create_notebook_inmem(name)
        self.select_notebook(nb["id"])
        return nb

    def select_notebook(self, notebook_id: int) -> bool:
        """Switch the active notebook; the log feed follows it. False if unknown."""
        notebook_id = int(notebook_id)
        if notebook_id not in self._notebook_id_set():
            return False
        self.active_notebook_id = notebook_id
        if self.notes is not None:
            self.notes.set_active_notebook(notebook_id)
            self.log = self.notes.all_notes(notebook_id)
        else:
            self.log = self._notebook_notes[notebook_id]
        return True

    # --- log ---------------------------------------------------------------
    def append_log(
        self,
        text: str,
        sample_id: Optional[str],
        category: Optional[str] = None,
        flag: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        step = self.current_step()
        timestamp = utc_now_iso()
        step_ref = step.id if step else None
        if self.notes is not None:
            entry = self.notes.add_note(
                text, timestamp, sample_id, step_ref, category, flag,
                notebook_id=self.active_notebook_id,
            )
            self._log_seq = entry["id"]
        else:
            self._log_seq += 1
            entry = {
                "id": self._log_seq,
                "text": text,
                "timestamp": timestamp,
                "sample_id": sample_id,
                "step_ref": step_ref,
                "category": category,
                "flag": flag,
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

    def update_last_log(
        self, text: str, flag: Optional[dict[str, Any]] = None
    ) -> Optional[dict[str, Any]]:
        if not self.log:
            return None
        entry = self.log[-1]
        entry["text"] = text
        entry["flag"] = flag
        if self.notes is not None:
            self.notes.update_text(int(entry["id"]), text, flag)
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

    def remove_timer(self, timer_id: str) -> bool:
        """Drop one timer (early stop / dismiss). True if it existed."""
        for i, timer in enumerate(self.timers):
            if timer.timer_id == timer_id:
                del self.timers[i]
                return True
        return False

    def remove_all_timers(self) -> list[str]:
        """Drop every timer and return the ids removed (for "stop timer")."""
        ids = [t.timer_id for t in self.timers]
        self.timers.clear()
        return ids

    def clear_timers(self) -> None:
        self.timers.clear()

    # --- demo reset --------------------------------------------------------
    def reset(self) -> None:
        """Clear stage state (protocol cursor + timers) for a fresh demo run.
        Leaves the log, protocols, and inventory untouched."""
        self.active_protocol = None
        self.current_step_index = -1
        self.clear_timers()
        self._timer_seq = 0

    def clear_log(self) -> None:
        """Wipe the lab notes for every notebook, in memory and on disk
        (LAB_DEMO_MODE only)."""
        self._log_seq = 0
        if self.notes is not None:
            self.notes.clear_all()
            self.log = self.notes.all_notes(self.active_notebook_id)
        else:
            for notes in self._notebook_notes.values():
                notes.clear()
            self.log = self._notebook_notes[self.active_notebook_id]
