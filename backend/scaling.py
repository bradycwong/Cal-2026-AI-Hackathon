"""Deterministic reagent scaling for protocol prep tables.

This module is intentionally pure: no database, no network, no LLM, and no
state mutation. AI may structure protocol parameters upstream; this module does
the arithmetic and inventory comparison deterministically.
"""

from __future__ import annotations

from typing import Any, Optional

from .inventory import InventoryItem, find_inventory_match
from .router import _canon_unit, normalize_ascii

FACTORS_TO_UL = {"uL": 1.0, "mL": 1000.0, "L": 1_000_000.0}

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


def _display_volume(total_ul: float) -> str:
    if total_ul >= FACTORS_TO_UL["L"]:
        return _format_amount(total_ul / FACTORS_TO_UL["L"], "L")
    if total_ul >= FACTORS_TO_UL["mL"]:
        return _format_amount(total_ul / FACTORS_TO_UL["mL"], "mL")
    return _format_amount(total_ul, "uL")


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
        item = find_inventory_match(
            str(row["reagent"]), inventory, cutoff=_PREP_MATCH_CUTOFF
        )
        row.update(
            {
                "match_name": None,
                "available": None,
                "available_unit": None,
                "available_display": None,
                "status": None,
                "verdict": "missing",
                "shortage_ul": None,
            }
        )
        if item is None:
            continue

        amount, unit = _inventory_amount(item)
        row["match_name"] = item.name
        row["available"] = _round_number(amount) if amount is not None else None
        row["available_unit"] = unit
        row["available_display"] = (
            f"{row['available']} {unit}" if amount is not None and unit else None
        )
        row["status"] = item.status

        if amount is None or _volume_unit(unit) is None:
            row["verdict"] = "unknown_unit"
            continue

        available_ul = convert_volume(amount, unit, "uL")
        if available_ul is None:
            row["verdict"] = "unknown_unit"
            continue

        need_ul = float(row["total_ul"])
        if available_ul < need_ul:
            row["verdict"] = "insufficient"
            row["shortage_ul"] = _round_number(need_ul - available_ul)
            continue

        status = (item.status or "ok").strip().lower()
        row["verdict"] = status if status in {"low", "expiring", "critical"} else "in_stock"

    return rows
