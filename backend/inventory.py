"""Inventory persistence — the file-driven CSV reagent store.

Extracted from ``state.py`` so ``SessionState`` no longer carries the reagent
concern. ``InventoryStore`` owns the in-memory list, stable per-session ids, and
CSV load/save; ``SessionState`` holds one store and delegates add/edit/delete/view
to it. Edit/delete are keyed by a stable id (not list position), so a concurrent
add/delete that reorders the list can't make a mutation land on the wrong row.
The id is in-memory only — the CSV schema is unchanged.
"""

from __future__ import annotations

import csv
import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_INVENTORY_STATUSES = {"ok", "low", "critical", "expiring"}

_INVENTORY_COLUMNS = [
    "name", "amount", "unit", "location", "quantity_approx", "notes", "code",
    "category", "date", "status",
]


@dataclass
class InventoryItem:
    name: str
    location: str
    quantity_approx: str
    # In-memory stable identity for edit/delete; assigned per session, NOT
    # persisted to the CSV (the UI always re-fetches fresh ids on hydrate).
    id: int = 0
    notes: str = ""
    code: str = ""
    category: str = "General"
    date: str = ""
    amount: str = ""
    unit: str = ""
    status: str = "ok"


def find_inventory_match(
    reagent_name: str, items: list[InventoryItem], *, cutoff: float = 0.6
) -> Optional[InventoryItem]:
    """Return the best inventory item for a reagent name.

    Mirrors the existing command handler behavior: fuzzy match first, then
    substring fallback. Returns None instead of guessing when there is no match.
    ``cutoff`` tunes the fuzzy threshold; callers that need fewer false positives
    (e.g. the reagent prep table) can pass a stricter value.
    """
    query = (reagent_name or "").strip()
    if not query:
        return None

    names = [item.name for item in items]
    matches = difflib.get_close_matches(
        query.lower(), [name.lower() for name in names], n=1, cutoff=cutoff
    )
    if matches:
        matched_name = matches[0]
        return next(item for item in items if item.name.lower() == matched_name)

    for item in items:
        if query.lower() in item.name.lower():
            return item

    return None


def _quantity_from_parts(quantity_approx: str, amount: str, unit: str) -> str:
    quantity = quantity_approx.strip()
    if quantity:
        return quantity
    amount = amount.strip()
    unit = unit.strip()
    if amount and unit:
        return f"{amount} {unit}"
    return amount


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
                    status=status,
                    amount=(row.get("amount") or "").strip(),
                    unit=(row.get("unit") or "").strip(),
                )
            )
    return items


def _inventory_row(item: InventoryItem) -> list[str]:
    """One CSV row for an item, in ``_INVENTORY_COLUMNS`` order."""
    return [
        item.name,
        item.amount,
        item.unit,
        item.location,
        item.quantity_approx,
        item.notes,
        item.code,
        item.category,
        item.date,
        item.status,
    ]


def _file_has_current_columns(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return True
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        return next(reader, []) == _INVENTORY_COLUMNS


def _write_inventory_file(path: Path, items: list[InventoryItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_INVENTORY_COLUMNS)
        writer.writerows(_inventory_row(item) for item in items)


class InventoryStore:
    """Owns the reagent list and its CSV persistence."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.items: list[InventoryItem] = []
        self._seq = 0

    @property
    def _path(self) -> Path:
        return self.data_dir / "inventory.csv"

    def _next_id(self) -> int:
        """Monotonic per-session id so edit/delete target an item by identity
        rather than list position (positions shift as items are added/removed)."""
        self._seq += 1
        return self._seq

    def load(self) -> None:
        path = self._path
        if path.exists():
            self.items = load_inventory_file(path)
            for item in self.items:
                item.id = self._next_id()

    def reload(self) -> None:
        """Re-read items from the (restored) CSV, restarting ids from 1.

        Used by the demo factory reset after the baseline CSV is copied back over
        ``inventory.csv``; replaces the live list so edits/adds/deletes are undone.
        """
        self.items = []
        self._seq = 0
        self.load()

    def _index_by_id(self, item_id: int) -> int:
        for i, item in enumerate(self.items):
            if item.id == item_id:
                return i
        raise IndexError(f"no inventory item with id {item_id}")

    def view(self) -> list[dict[str, Any]]:
        """Read-only view of inventory for ``GET /api/inventory``."""
        # Deferred: scaling imports inventory, so a top-level import would cycle.
        from .scaling import humanize_metric

        return [
            {
                "id": item.id,
                "name": item.name,
                "code": item.code,
                "location": item.location,
                "category": item.category,
                "quantity_approx": item.quantity_approx,
                "amount": item.amount,
                "unit": item.unit,
                # Readable metric form ("1000 mL" -> "1 L") for display; raw
                # amount/unit above stay authoritative for editing + math.
                "amount_display": humanize_metric(item.amount, item.unit),
                "date": item.date,
                "status": item.status,
                "notes": item.notes,
            }
            for item in self.items
        ]

    def add(
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
        """Append a manually-entered item to memory and persist it to the CSV.

        The CSV is the file-driven store; appending here mirrors how
        ``load_inventory_file`` reads it back, so the item survives a restart.
        """
        name = name.strip()
        if not name:
            raise ValueError("inventory item requires a name")
        status = (status or "ok").strip().lower()
        if status not in _INVENTORY_STATUSES:
            status = "ok"
        amount = amount.strip()
        unit = unit.strip()
        item = InventoryItem(
            name=name,
            location=location.strip(),
            quantity_approx=_quantity_from_parts(quantity_approx, amount, unit),
            notes=notes.strip(),
            code=code.strip(),
            category=(category or "General").strip() or "General",
            date=date.strip(),
            status=status,
            amount=amount,
            unit=unit,
        )
        item.id = self._next_id()
        self.items.append(item)
        path = self._path
        if not _file_has_current_columns(path):
            _write_inventory_file(path, self.items)
            return item
        write_header = not path.exists() or path.stat().st_size == 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if write_header:
                writer.writerow(_INVENTORY_COLUMNS)
            writer.writerow(_inventory_row(item))
        return item

    def _write(self) -> None:
        """Rewrite the whole CSV from memory (used by edit/delete)."""
        _write_inventory_file(self._path, self.items)

    def update(
        self,
        item_id: int,
        name: Optional[str] = None,
        location: Optional[str] = None,
        amount: Optional[str] = None,
        unit: Optional[str] = None,
        date: Optional[str] = None,
    ) -> InventoryItem:
        """Edit fields of the item with ``item_id`` and persist the whole CSV.

        Keyed by stable id (not list position). Only non-None fields change.
        """
        item = self.items[self._index_by_id(item_id)]
        if name is not None:
            new_name = name.strip()
            if not new_name:
                raise ValueError("inventory item requires a name")
            item.name = new_name
        if location is not None:
            item.location = location.strip()
        if amount is not None:
            item.amount = str(amount).strip()
        if unit is not None:
            item.unit = str(unit).strip()
        if amount is not None or unit is not None:
            item.quantity_approx = _quantity_from_parts("", item.amount, item.unit)
        if date is not None:
            item.date = date.strip()
        self._write()
        return item

    def delete(self, item_id: int) -> InventoryItem:
        """Remove the item with ``item_id`` (by stable id) and persist the CSV."""
        removed = self.items.pop(self._index_by_id(item_id))
        self._write()
        return removed
