"""Deterministic reagent scaling for protocol prep tables.

This module is intentionally pure: no database, no network, no LLM, and no
state mutation. AI may structure protocol parameters upstream; this module does
the arithmetic and inventory comparison deterministically.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .inventory import InventoryItem, find_inventory_match
from .router import _canon_unit, normalize_ascii

FACTORS_TO_UL = {"uL": 1.0, "mL": 1000.0, "L": 1_000_000.0}

# Status precedence when several bottles of the same reagent disagree: the most
# alarming one wins so the prep verdict never looks rosier than the worst stock.
_STATUS_RANK = {"ok": 0, "low": 1, "expiring": 2, "critical": 3}

# Aliases declared inline in the inventory ``notes`` (e.g. the shipped EDTA row
# carries "see also: Ethylenediaminetetraacetic acid"). Lets two bottles that
# spell the same reagent differently still group and pool their volumes.
_ALIAS_NOTE_RE = re.compile(
    r"(?:see also|aka|also known as|alias(?:es)?)\s*:?\s*(.+)", re.IGNORECASE
)

# Prep tables need precision over recall: a wrong reagent match (e.g. "lysis
# buffer" -> "Elution buffer") would misreport availability. Match stricter than
# the voice find_inventory command so generic shared words ("buffer") don't pair
# unrelated reagents.
_PREP_MATCH_CUTOFF = 0.75


def _volume_unit(unit: str) -> Optional[str]:
    canon = _canon_unit(normalize_ascii(unit or ""))
    return canon if canon in FACTORS_TO_UL else None


def _round_number(value: float) -> float | int:
    rounded = round(float(value), 6)
    return int(rounded) if rounded.is_integer() else rounded


def _format_amount(value: float, unit: str) -> str:
    value = _round_number(value)
    return f"{value:g} {unit}" if isinstance(value, float) else f"{value} {unit}"


# Metric ladders for humanizing a structured amount+unit: each ordered small->large
# with the unit's factor in the ladder's smallest unit. NOTE: "g" here is grams (a
# structured inventory unit), NOT centrifuge g-force (RCF) -- which is exactly why
# humanize_metric is applied to amount+unit fields ONLY, never to free step text.
_METRIC_LADDERS = (
    (("uL", 1.0), ("mL", 1e3), ("L", 1e6)),
    (("ug", 1.0), ("mg", 1e3), ("g", 1e6), ("kg", 1e9)),
    (("nmol", 1.0), ("umol", 1e3), ("mmol", 1e6), ("mol", 1e9)),
)
_LADDER_BY_UNIT = {unit: ladder for ladder in _METRIC_LADDERS for unit, _ in ladder}


def humanize_metric(value: Any, unit: str) -> str:
    """Render a structured amount+unit at its most readable metric step.

    Steps up the ladder once a value reaches 1000 of a unit: 50000 uL -> "50 mL",
    1000 mg -> "1 g", 1000 g -> "1 kg". Units outside a known metric ladder (e.g.
    "units") and non-numeric amounts (ranges, "TBD", blank) are returned as written.
    Applied to STRUCTURED amount+unit fields only, so "g" means grams (not g-force).
    """
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return f"{str(value).strip()} {(unit or '').strip()}".strip()
    raw_unit = (unit or "").strip()
    canon = _canon_unit(normalize_ascii(raw_unit))
    ladder = _LADDER_BY_UNIT.get(canon)
    if ladder is None or amount <= 0:
        return _format_amount(amount, raw_unit) if raw_unit else f"{_round_number(amount):g}"
    base = amount * dict(ladder)[canon]
    chosen_unit, chosen_factor = ladder[0]
    for unit_name, factor in ladder:
        if base >= factor:
            chosen_unit, chosen_factor = unit_name, factor
    # Keep the author's unit spelling (e.g. the pretty "µL") when the scale is
    # unchanged; only switch to the canonical unit on an actual step up/down.
    out_unit = raw_unit if chosen_unit == canon else chosen_unit
    return _format_amount(base / chosen_factor, out_unit)


def _display_volume(total_ul: float) -> str:
    # Volume-specialized name kept for readability; the generic engine does the walk.
    return humanize_metric(total_ul, "uL")


# Matches a microliter quantity in normalize_ascii'd prose (the unit is always
# "uL" by then). Volume-only and unit-pinned, so it never touches "65 degrees C",
# "13,000 g", "30 cycles", "100 mL", or reagent names.
_HUMANIZE_VOL_RE = re.compile(r"(\d+(?:\.\d+)?)\s*uL\b")


def humanize_volume_text(text: str) -> str:
    """Rewrite inflated microliter mentions (>=1000 uL) in prose to mL/L.

    Reuses ``_display_volume`` so "50000 uL" -> "50 mL", "2000 uL" -> "2 mL",
    "1500.0 uL" -> "1.5 mL". Leaves "<1000 uL" ("950 uL", "200 uL") byte-for-byte
    untouched. Idempotent; safe on empty / volume-free text. Used by the import
    paths so a step's displayed text never reads "50000 uL" for what is "50 mL".
    """
    if not text:
        return text

    def _sub(match: "re.Match[str]") -> str:
        ul = float(match.group(1))
        return match.group(0) if ul < FACTORS_TO_UL["mL"] else _display_volume(ul)

    return _HUMANIZE_VOL_RE.sub(_sub, text)


def convert_volume(amount: float, from_unit: str, to_unit: str) -> Optional[float]:
    """Convert volume units only. Return None for non-volume units."""
    src = _volume_unit(from_unit)
    dst = _volume_unit(to_unit)
    if src is None or dst is None:
        return None
    amount_ul = float(amount) * FACTORS_TO_UL[src]
    return float(_round_number(amount_ul / FACTORS_TO_UL[dst]))


def _numeric_volume(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def aggregate_reagents(protocol: Any) -> dict[str, float]:
    """Sum per-sample uL volumes by reagent across all protocol steps."""
    totals: dict[str, float] = {}
    for step in getattr(protocol, "steps", []):
        params = getattr(step, "parameters", {}) or {}
        reagent = params.get("reagent")
        volume = _numeric_volume(params.get("volume_ul"))
        if not reagent or volume is None:
            continue
        name = str(reagent)
        totals[name] = totals.get(name, 0.0) + volume
    return totals


def protocol_ingredients(protocol: Any) -> list[dict[str, Any]]:
    """Per-sample reagent amounts as written in the protocol (name + display).

    The base amounts shown before a run is configured — no sample scaling. Used by
    ``protocol_catalog`` so the protocol card can list ingredients before load.
    """
    return [
        {
            "reagent": reagent,
            "volume_ul": _round_number(volume_ul),
            "display": _display_volume(volume_ul),
        }
        for reagent, volume_ul in aggregate_reagents(protocol).items()
    ]


def scale_reagents(
    protocol: Any, n_samples: int, overage_pct: float
) -> list[dict[str, Any]]:
    """Scale aggregated per-sample reagent volumes for a run."""
    if n_samples < 1:
        raise ValueError("sample count must be at least 1")
    if overage_pct < 0:
        raise ValueError("overage percent must be non-negative")

    factor = n_samples * (1 + (overage_pct / 100.0))
    rows: list[dict[str, Any]] = []
    for reagent, per_sample_ul in aggregate_reagents(protocol).items():
        total_ul = per_sample_ul * factor
        rows.append(
            {
                "reagent": reagent,
                "per_sample_ul": _round_number(per_sample_ul),
                "n_samples": n_samples,
                "overage_pct": _round_number(overage_pct),
                "total_ul": _round_number(total_ul),
                "total_display": _display_volume(total_ul),
            }
        )
    return rows


def _canonical_reagent_key(name: str) -> str:
    """Collapse a reagent name to a comparison key for grouping bottles.

    Strips concentration/grade noise that varies between bottles of the same
    reagent ("EDTA 0.5 M", "Ethanol 70%", "2X master mix") so they map to one
    key. Deliberately conservative: it normalizes formatting, not chemistry —
    distinct reagents that merely share a word ("Elution buffer" vs
    "Resuspension buffer") keep different keys.
    """
    s = normalize_ascii(name or "").lower()
    s = re.sub(r"\([^)]*\)", " ", s)  # drop parenthetical qualifiers
    # drop concentration-like tokens: a number with an optional unit suffix.
    s = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:%|x|m|mm|um|nm|mg/ml|mg|ml|ul|l|u/ul|units?)?\b",
        " ",
        s,
    )
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _reagent_keys(item: InventoryItem) -> set[str]:
    """All canonical keys a bottle answers to: its name plus any note aliases."""
    keys = {_canonical_reagent_key(item.name)}
    match = _ALIAS_NOTE_RE.search(getattr(item, "notes", "") or "")
    if match:
        for part in re.split(r"[;,/]", match.group(1)):
            key = _canonical_reagent_key(part)
            if key:
                keys.add(key)
    keys.discard("")
    return keys


def find_inventory_group(
    reagent_name: str, items: list[InventoryItem], *, cutoff: float
) -> tuple[Optional[InventoryItem], list[InventoryItem]]:
    """Find every bottle that is the same reagent as ``reagent_name``.

    Anchors on the single best fuzzy/substring match (so precision matches the
    voice/prep behavior), then expands to all bottles whose canonical key (or a
    note alias) intersects the anchor's. Returns ``(anchor, group)``; ``group``
    leads with the anchor. ``(None, [])`` when nothing matches.
    """
    anchor = find_inventory_match(reagent_name, items, cutoff=cutoff)
    if anchor is None:
        return None, []
    target_keys = _reagent_keys(anchor)
    query_key = _canonical_reagent_key(reagent_name)
    if query_key:
        target_keys.add(query_key)
    group = [item for item in items if _reagent_keys(item) & target_keys]
    group.sort(key=lambda item: item is not anchor)  # anchor first, stable order
    return anchor, group


def _worst_status(statuses: list[str]) -> str:
    return max(
        (s for s in statuses if s),
        key=lambda s: _STATUS_RANK.get(s, 0),
        default="ok",
    )


def _inventory_amount(item: InventoryItem) -> tuple[Optional[float], str]:
    try:
        amount = float(str(item.amount).strip())
    except (TypeError, ValueError):
        return None, (item.unit or "").strip()
    return amount, (item.unit or "").strip()


def build_prep_table(
    protocol: Any,
    n_samples: int,
    overage_pct: float,
    inventory: list[InventoryItem],
) -> list[dict[str, Any]]:
    """Attach inventory availability verdicts to scaled reagent rows."""
    rows = scale_reagents(protocol, n_samples, overage_pct)
    for row in rows:
        anchor, group = find_inventory_group(
            str(row["reagent"]), inventory, cutoff=_PREP_MATCH_CUTOFF
        )
        row.update(
            {
                "match_name": None,
                "match_count": 0,
                "sources": [],
                "available": None,
                "available_unit": None,
                "available_display": None,
                "status": None,
                "verdict": "missing",
                "shortage_ul": None,
            }
        )
        if anchor is None:
            continue

        # Pool every bottle of this reagent: a protocol's need is met if the
        # COMBINED volume across bottles covers it, even when no single bottle
        # does. Only volume-unit bottles contribute to the sum.
        total_ul = 0.0
        has_volume = False
        nonvolume_unit = ""
        for item in group:
            amount, unit = _inventory_amount(item)
            if amount is None:
                continue
            converted = (
                convert_volume(amount, unit, "uL")
                if _volume_unit(unit) is not None
                else None
            )
            if converted is None:
                nonvolume_unit = nonvolume_unit or unit
                continue
            total_ul += converted
            has_volume = True

        row["match_count"] = len(group)
        row["sources"] = [item.name for item in group]
        row["source_details"] = [
            {
                "id": item.id,
                "name": item.name,
                "amount_display": (
                    f"{item.amount} {item.unit}".strip()
                    if item.amount
                    else (item.quantity_approx or "?")
                ),
                "location": item.location or "",
                "status": (item.status or "ok").strip().lower(),
            }
            for item in group
        ]
        row["match_name"] = (
            anchor.name
            if len(group) == 1
            else f"{anchor.name} (+{len(group) - 1} more, pooled)"
        )
        row["status"] = _worst_status(
            [(item.status or "ok").strip().lower() for item in group]
        )

        if not has_volume:
            row["available_unit"] = nonvolume_unit or None
            row["verdict"] = "unknown_unit"
            continue

        row["available"] = _round_number(total_ul)
        row["available_unit"] = "uL"
        row["available_display"] = _display_volume(total_ul)

        need_ul = float(row["total_ul"])
        if total_ul < need_ul:
            row["verdict"] = "insufficient"
            row["shortage_ul"] = _round_number(need_ul - total_ul)
            continue

        status = row["status"]
        row["verdict"] = status if status in {"low", "expiring", "critical"} else "in_stock"

    return rows


def apply_reagent_deductions(
    rows: list[dict[str, Any]],
    priority_order: dict[str, list[int]],
    inventory: list[InventoryItem],
) -> list[dict[str, Any]]:
    """Compute per-bottle volume deductions from a scaled prep table.

    ``priority_order`` maps reagent name (as it appears in ``row["reagent"]``)
    to an ordered list of item ids — first id is consumed first, down to the
    last id. Bottles not listed keep their existing order (anchor first).

    Returns a list of ``{item_id, name, deduct_ul, new_amount, new_unit}``
    dicts — one per bottle that will be reduced. Bottles that are exhausted
    by the deduction get ``new_amount == 0``. No state is mutated here; the
    caller applies the deductions via the inventory store.
    """
    id_to_item: dict[int, InventoryItem] = {item.id: item for item in inventory}
    deductions: list[dict[str, Any]] = []

    for row in rows:
        need_ul = float(row.get("total_ul") or 0)
        if need_ul <= 0:
            continue

        # Build the ordered bottle list for this reagent from the prep-table
        # sources, then re-sort by user priority if provided.
        source_names: list[str] = row.get("sources") or []
        if not source_names:
            continue

        # Map source names → items, preserving anchor-first order.
        name_to_item = {item.name: item for item in inventory}
        bottles: list[InventoryItem] = [
            name_to_item[n] for n in source_names if n in name_to_item
        ]

        user_order: list[int] = priority_order.get(row["reagent"], [])
        if user_order:
            id_pos = {item_id: i for i, item_id in enumerate(user_order)}
            bottles.sort(
                key=lambda b: id_pos.get(b.id, len(user_order))
            )

        remaining_ul = need_ul
        for bottle in bottles:
            if remaining_ul <= 0:
                break
            amount, unit = _inventory_amount(bottle)
            if amount is None or _volume_unit(unit) is None:
                continue
            available_ul = convert_volume(amount, unit, "uL")
            if available_ul is None or available_ul <= 0:
                continue

            take_ul = min(remaining_ul, available_ul)
            remaining_ul -= take_ul

            leftover_ul = available_ul - take_ul
            new_amount = _round_number(
                convert_volume(leftover_ul, "uL", unit) or 0.0
            )
            deductions.append(
                {
                    "item_id": bottle.id,
                    "name": bottle.name,
                    "deduct_ul": _round_number(take_ul),
                    "new_amount": new_amount,
                    "new_unit": unit,
                }
            )

    return deductions
