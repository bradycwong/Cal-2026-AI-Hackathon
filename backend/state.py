"""state.py — in-memory SessionState + file loaders.

Holds everything deterministic handlers mutate: the active protocol + cursor, the
log, and active timers. The log is the one persisted organ: pass ``db_path`` and
appends are written to SQLite (``backend/db.py``) and reloaded on startup so the
feed survives refresh/restart. With ``db_path=None`` (tests) it's pure in-memory.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from .db import _DEFAULT_NOTEBOOK_NAME, NoteStore
from .inventory import InventoryItem, InventoryStore

DATA_DIR = Path(__file__).parent / "data"
# Read-only "golden image" of the shipped protocols + inventory. The demo only
# ever writes to DATA_DIR; the factory reset copies SEED_DIR back over it.
SEED_DIR = Path(__file__).parent / "seed"


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


_PROTOCOL_STATUSES = {"READY", "LOW_REAGENTS", "ARCHIVED"}


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


def _build_step_doc(
    idx: int,
    text: str,
    duration_s: Optional[int],
    timer_label: Optional[str],
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """One step's YAML dict for an edit. Enforces the invariant the parser does
    NOT: a ``timer_label`` is kept only when the step is actually timed."""
    doc: dict[str, Any] = {
        "id": idx,
        "text": text,
        "duration_s": duration_s or None,
        "parameters": parameters or {},
    }
    if duration_s and timer_label:
        doc["timer_label"] = timer_label
    return doc


@dataclass
class Timer:
    timer_id: str
    label: str
    duration_s: int
    started_at: Optional[float] = None        # monotonic while running; None while paused
    remaining_at_pause: Optional[int] = None  # frozen remaining while paused
    expired: bool = False
    step_id: Optional[int] = None             # owning protocol step; None = ad-hoc user timer

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


# Dashboard "Recently Used Protocols" surfaces at most this many cards.
RECENT_LIMIT = 3


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


def restore_seed_files(seed_dir: Path, data_dir: Path) -> None:
    """Copy the seed baseline back over the live data dir (demo factory reset).

    Protocols and inventory are file-driven, so restoring them means replacing the
    working files. Each restore is guarded: a missing/empty seed leaves the live
    files untouched rather than wiping the library out from under the operator.
    """
    seed_protocols = seed_dir / "protocols"
    seed_yaml = sorted(seed_protocols.glob("*.yaml")) if seed_protocols.exists() else []
    if seed_yaml:
        proto_dst = data_dir / "protocols"
        proto_dst.mkdir(parents=True, exist_ok=True)
        for stale in proto_dst.glob("*.yaml"):
            stale.unlink()
        for src in seed_yaml:
            shutil.copy(src, proto_dst / src.name)

    seed_inventory = seed_dir / "inventory.csv"
    if seed_inventory.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(seed_inventory, data_dir / "inventory.csv")


class SessionState:
    """Singleton-per-session state. One protocol loaded at a time (v1)."""

    def __init__(
        self,
        data_dir: Path = DATA_DIR,
        db_path: Optional[str] = None,
        seed_dir: Path = SEED_DIR,
    ) -> None:
        self.data_dir = data_dir
        self.seed_dir = seed_dir
        self.protocols: dict[str, Protocol] = {}
        self._inventory = InventoryStore(data_dir)
        self.active_protocol: Optional[Protocol] = None
        self.current_step_index: int = -1
        self.protocol_complete: bool = False
        # Recently-used protocols for the dashboard. Maps protocol id -> last-used
        # ISO timestamp, insertion-ordered oldest->newest. In-memory like the
        # cursor; insertion order (not the seconds-resolution timestamp) is the
        # authoritative sort, so same-second loads still order correctly.
        self.recent_protocols: dict[str, str] = {}
        # Indices of steps the user skipped (advanced past without confirming).
        # In-memory like the cursor; surfaced on step_change so the tracker can
        # render them yellow instead of green.
        self.skipped_steps: set[int] = set()
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

    @property
    def inventory(self) -> list[InventoryItem]:
        """Reagent list, owned by the InventoryStore. Read-only view kept so
        handlers/tests can still iterate ``state.inventory`` directly."""
        return self._inventory.items

    def _reload_protocols(self) -> None:
        """(Re)load the protocol library from ``data/protocols/*.yaml``."""
        self.protocols.clear()
        proto_dir = self.data_dir / "protocols"
        for path in sorted(proto_dir.glob("*.yaml")):
            proto = load_protocol_file(path)
            self.protocols[proto.id] = proto

    def load_files(self) -> None:
        self._reload_protocols()
        self._inventory.load()
        if self.notes is not None:
            self.active_notebook_id = int(self.notes.get_active_notebook_id())
            self.log = self.notes.all_notes(self.active_notebook_id)
            last = self.notes.all_notes()  # global max id keeps ids monotonic
            self._log_seq = last[-1]["id"] if last else 0

    # --- snapshot helpers (read-only views for the REST endpoints) ---------
    def protocol_catalog(self) -> list[dict[str, Any]]:
        """Read-only summary of every loaded protocol for ``GET /api/protocols``."""
        # Lazy import: scaling depends on router/inventory; importing it at module
        # load would risk an import cycle through state.
        from .scaling import protocol_ingredients

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
                    "ingredients": protocol_ingredients(proto),
                    "aliases": proto.aliases,
                }
            )
        return catalog

    def mark_protocol_used(self, protocol_id: str) -> None:
        """Record a successful protocol load for the dashboard's recent list.
        Pop-then-reinsert moves the id to the newest position."""
        self.recent_protocols.pop(protocol_id, None)
        self.recent_protocols[protocol_id] = utc_now_iso()

    def recent_protocols_view(self, limit: int = RECENT_LIMIT) -> list[dict[str, Any]]:
        """The N most-recently-used protocols (newest first) for the dashboard,
        each catalog entry tagged with ``last_used_at``. Deleted protocols are
        dropped. Cold start (nothing used yet) falls back to the first ``limit``
        catalog entries with ``last_used_at=None`` so the panel is never blank."""
        by_id = {p["id"]: p for p in self.protocol_catalog()}
        if not self.recent_protocols:
            cold = list(by_id.values())[:limit]
            recent = [{**p, "last_used_at": None} for p in cold]
        else:
            recent = []
            for pid in reversed(self.recent_protocols):  # newest first
                meta = by_id.get(pid)
                if meta is None:
                    continue  # protocol was deleted after use
                recent.append({**meta, "last_used_at": self.recent_protocols[pid]})
                if len(recent) >= limit:
                    break
        # Tag the currently-loaded protocol so the dashboard can highlight it, the
        # same way ``notebooks_view`` flags the active notebook. A loaded protocol
        # is always the newest "used", so it sits first in this list.
        active_id = self.active_protocol.id if self.active_protocol else None
        for p in recent:
            p["active"] = p["id"] == active_id
        return recent

    def protocol_detail(self, protocol_id: str) -> Optional[dict[str, Any]]:
        """Full editable view of one protocol (step text + params + timer labels)
        for the editor, or None if unknown. Unlike ``protocol_catalog`` this
        carries the steps, not just a count."""
        proto = self.protocols.get(protocol_id)
        if proto is None:
            return None
        return {
            "id": proto.id,
            "name": proto.name,
            "description": proto.description,
            "category": proto.category,
            "status": proto.status,
            "aliases": proto.aliases,
            "steps": [
                {
                    "id": s.id,
                    "text": s.text,
                    "duration_s": s.duration_s,
                    "timer_label": s.timer_label,
                    "parameters": s.parameters or {},
                }
                for s in proto.steps
            ],
        }

    def inventory_view(self) -> list[dict[str, Any]]:
        """Read-only view of inventory for ``GET /api/inventory``."""
        return self._inventory.view()

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
        status: str = "ok",
        amount: str = "",
        unit: str = "",
    ) -> InventoryItem:
        """Add a reagent (delegates to the InventoryStore)."""
        return self._inventory.add(
            name,
            location,
            quantity_approx,
            notes,
            code,
            category,
            date,
            status,
            amount,
            unit,
        )

    def update_inventory_item(
        self,
        item_id: int,
        name: Optional[str] = None,
        location: Optional[str] = None,
        amount: Optional[str] = None,
        unit: Optional[str] = None,
        date: Optional[str] = None,
    ) -> InventoryItem:
        """Edit an item by stable id (delegates to the InventoryStore)."""
        return self._inventory.update(
            item_id,
            name=name,
            location=location,
            amount=amount,
            unit=unit,
            date=date,
        )

    def delete_inventory_item(self, item_id: int) -> InventoryItem:
        """Delete an item by stable id (delegates to the InventoryStore)."""
        return self._inventory.delete(item_id)

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

    def update_protocol(
        self,
        protocol_id: str,
        name: str,
        description: str,
        steps: list[dict[str, Any]],
    ) -> Protocol:
        """Edit an existing protocol's name/description/steps in place.

        The ``id`` (and its ``<id>.yaml`` filename), ``category``, ``status`` and
        ``aliases`` are FROZEN — only the editable fields change, so active/recent
        references and the backing file path stay valid. ``reagents`` and
        ``est_duration_min`` are omitted so they re-derive from the edited steps.
        Validates the assembled document in memory BEFORE writing, so a malformed
        edit raises ``ProtocolParseError`` without ever touching the file.
        """
        from .protocol_import import _write_yaml  # lazy: avoid an import cycle

        existing = self.protocols[protocol_id]  # caller guards existence (404)
        steps_doc = [
            _build_step_doc(
                i,
                (s.get("text") or "").strip(),
                s.get("duration_s"),
                s.get("timer_label"),
                s.get("parameters") or {},
            )
            for i, s in enumerate(steps, start=1)
        ]
        document = {
            "protocol": {
                "id": existing.id,
                "name": name,
                "version": "1.0",
                "description": description or "",
                "category": existing.category,
                "status": existing.status,
                "aliases": existing.aliases,
                "steps": steps_doc,
            }
        }
        # Validate-by-parse before any write so a bad edit can't corrupt the file.
        parse_protocol_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=False))
        path = self.data_dir / "protocols" / f"{existing.id}.yaml"
        _write_yaml(path, document)
        return self.register_protocol(path)

    def remove_protocol(self, protocol_id: str) -> bool:
        """Drop a protocol from memory and delete its YAML so it does not reload.

        Accepts an id, name, or alias (same resolver ``load`` uses). If the removed
        protocol is the active one, the stage state is reset so the guide stops
        pointing at a protocol that is gone. Returns True if one was removed.
        """
        proto = self.protocols.get(protocol_id.strip()) or self.find_protocol(protocol_id)
        if proto is None:
            return False
        self.protocols.pop(proto.id, None)
        self.recent_protocols.pop(proto.id, None)  # don't keep a stale recent id
        # Delete the backing file. Convention is "<id>.yaml"; fall back to matching
        # by loaded id so a filename that differs from the id is still removed.
        proto_dir = self.data_dir / "protocols"
        direct = proto_dir / f"{proto.id}.yaml"
        if direct.exists():
            direct.unlink()
        else:
            for path in proto_dir.glob("*.yaml"):
                try:
                    if load_protocol_file(path).id == proto.id:
                        path.unlink()
                        break
                except Exception:
                    continue
        if self.active_protocol is not None and self.active_protocol.id == proto.id:
            self.active_protocol = None
            self.current_step_index = -1
            self.protocol_complete = False
            self.skipped_steps.clear()
            self.clear_timers()
        return True

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
        entry_type: str = "manual",
    ) -> dict[str, Any]:
        # ``entry_type`` is caller-driven: step-advance notes pass "automatic";
        # every other writer (manual log command, /api/log) keeps the "manual"
        # default. New entries are never ``edited``.
        step = self.current_step()
        timestamp = utc_now_iso()
        step_ref = step.id if step else None
        if self.notes is not None:
            entry = self.notes.add_note(
                text, timestamp, sample_id, step_ref, category, flag,
                notebook_id=self.active_notebook_id, entry_type=entry_type, edited=False,
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
                "entry_type": entry_type,
                "edited": False,
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
        return self._apply_log_edit(self.log[-1], text, flag)

    def update_log_by_id(
        self, id: int, text: str, flag: Optional[dict[str, Any]] = None
    ) -> Optional[dict[str, Any]]:
        """Edit any entry in the active notebook by id (None if absent)."""
        entry = next((e for e in self.log if int(e["id"]) == int(id)), None)
        if entry is None:
            return None
        return self._apply_log_edit(entry, text, flag)

    def _apply_log_edit(
        self, entry: dict[str, Any], text: str, flag: Optional[dict[str, Any]]
    ) -> dict[str, Any]:
        # A human edit re-tags the entry manual + edited (mirrors db.update_text).
        entry["text"] = text
        entry["flag"] = flag
        entry["entry_type"] = "manual"
        entry["edited"] = True
        if self.notes is not None:
            self.notes.update_text(int(entry["id"]), text, flag)
        return entry

    # --- timers ------------------------------------------------------------
    def add_timer(
        self, duration_s: int, label: str, *, paused: bool = False,
        step_id: Optional[int] = None,
    ) -> Timer:
        self._timer_seq += 1
        timer = Timer(
            timer_id=f"t{self._timer_seq}",
            label=label,
            duration_s=duration_s,
            started_at=None if paused else time.monotonic(),
            remaining_at_pause=duration_s if paused else None,
            step_id=step_id,
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

    def remove_expired_timers(self) -> list[str]:
        """Drop only the finished (expired) timers and return the ids removed.
        Running/paused timers stay (this is "clear done timers", NOT "stop timer")."""
        removed = [t.timer_id for t in self.timers if t.expired]
        self.timers = [t for t in self.timers if not t.expired]
        return removed

    def remove_timers_for_step(self, step_id: int) -> list[str]:
        """Drop timers owned by the given protocol step and return the ids removed
        (the step's timer is cleared when the step is completed/skipped). Ad-hoc
        user timers (step_id is None) survive, since None != step_id."""
        removed = [t.timer_id for t in self.timers if t.step_id == step_id]
        self.timers = [t for t in self.timers if t.step_id != step_id]
        return removed

    def clear_timers(self) -> None:
        self.timers.clear()

    def unload_protocol(self) -> None:
        """Cancel the active run: clear the protocol cursor and its timers. Unlike
        ``reset()``, this KEEPS the log, inventory, protocol library, and
        recent-protocol history — it only stops the run in progress."""
        self.active_protocol = None
        self.current_step_index = -1
        self.protocol_complete = False
        self.skipped_steps.clear()
        self.clear_timers()

    # --- demo reset --------------------------------------------------------
    def reset(self) -> None:
        """Clear stage state (protocol cursor + timers) for a fresh demo run.
        Leaves the log, protocols, and inventory untouched."""
        self.active_protocol = None
        self.current_step_index = -1
        self.protocol_complete = False
        self.skipped_steps.clear()
        self.recent_protocols.clear()
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

    def restore_factory_state(self) -> None:
        """Full demo reset: run state, notes + notebooks, inventory, and the
        protocol library are all returned to the original (seed) baseline.

        Unlike ``reset()``/``clear_log()`` (which only touch transient state),
        this restores the file-driven stores from ``seed_dir`` and drops every
        created notebook back to the single default."""
        self.reset()
        restore_seed_files(self.seed_dir, self.data_dir)
        self._reload_protocols()
        self._inventory.reload()
        self._log_seq = 0
        if self.notes is not None:
            self.notes.factory_reset()
            self.active_notebook_id = int(self.notes.get_active_notebook_id())
            self.log = self.notes.all_notes(self.active_notebook_id)
        else:
            # In-memory (no-db) path — mirror the notebook setup in __init__.
            self._notebooks = []
            self._notebook_notes = {}
            self._notebook_seq = 0
            default = self._create_notebook_inmem(_DEFAULT_NOTEBOOK_NAME)
            self.active_notebook_id = default["id"]
            self.log = self._notebook_notes[default["id"]]
