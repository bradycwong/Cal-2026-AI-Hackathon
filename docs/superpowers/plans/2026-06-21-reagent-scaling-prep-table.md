# Reagent Scaling Prep Table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic "Protocol Compiler" core: given a loaded protocol, sample count, and overage percent, produce a scaled reagent prep table with inventory availability checks.

**Architecture:** Keep this feature read-only and outside the locked command spine. The LLM may help protocol import create structured `parameters`, but scaling is pure deterministic Python: aggregate `parameters.reagent` plus `parameters.volume_ul`, scale quantities, compare against inventory, and expose the result through a plain REST endpoint. The served UI is `FrontendTest/`, so the dashboard adds a hook-guarded Reagent Prep panel that calls `/api/scale`.

**Tech Stack:** FastAPI, Pydantic, pure Python helpers, existing `SessionState`, existing CSV-backed inventory, vanilla `FrontendTest/app.js`, pytest.

## Global Constraints

- Do not change the locked ingest spine: `transcript -> route() -> handle_command() -> broadcast()`.
- Do not add a new `Command` intent.
- Do not add a websocket event type or websocket broadcast for scaling.
- Do not call an LLM from scaling or `/api/scale`.
- Scaling is read-only: no protocol, inventory, timer, or notebook mutation.
- Use structured step parameters only: `parameters.reagent` and numeric `parameters.volume_ul`.
- Treat all math as volume-only. Never convert mass to volume.
- Use ASCII units in code and docs: `uL`, `mL`, `L`.
- Served UI is `FrontendTest/`; legacy `frontend/` is not the target.
- There is an existing dirty worktree in this project. Do not reset, checkout, delete, or overwrite unrelated changes.

---

## File Structure

- `backend/scaling.py`: new pure module for volume conversion, reagent aggregation, scaling rows, and prep-table inventory verdicts.
- `backend/inventory.py`: add a pure `find_inventory_match()` helper next to `InventoryItem`.
- `backend/handlers.py`: refactor `_handle_find_inventory()` to use `find_inventory_match()` with no behavior change.
- `backend/main.py`: add `ScaleIn` and `POST /api/scale`.
- `FrontendTest/dashboard.html`: add the Reagent Prep panel hooks.
- `FrontendTest/app.js`: add `fetchScale()`, `renderPrepTable()`, `handlePrepCompute()`, and hook-guarded wiring.
- `FrontendTest/dashboard.css`: optional minimal styling for the prep table if existing Tailwind classes are not enough.
- `tests/test_scaling.py`: new pure-function tests.
- `tests/test_scale_endpoint.py`: new API tests.
- `tests/test_frontend_static.py`: add static hook and client-contract tests.
- `README.md`: add a short note for the demo flow and endpoint.

---

### Task 0: Baseline Guardrail

**Files:**
- Read: repository working tree
- Modify: none

**Interfaces:**
- Consumes: current repository state.
- Produces: a known baseline before feature work.

- [ ] **Step 1: Inspect current changes**

Run:

```bash
git status --short
```

Expected: note existing modified files. Do not reset or revert them.

- [ ] **Step 2: Run the current suite**

Run:

```bash
python -m pytest -q
```

Expected: if failures exist before this feature, record the failing test names and continue only if they are unrelated to scaling. Do not hide pre-existing failures by changing unrelated code.

---

### Task 1: Extract Inventory Matching Helper

**Files:**
- Modify: `backend/inventory.py`
- Modify: `backend/handlers.py`
- Test: `tests/test_handlers.py`

**Interfaces:**
- Produces: `find_inventory_match(reagent_name: str, items: list[InventoryItem]) -> Optional[InventoryItem]`
- Consumes: existing `InventoryItem` dataclass.
- Preserves: existing `find_inventory` command behavior.

- [ ] **Step 1: Add focused tests for the pure matcher**

Append these tests to `tests/test_handlers.py`:

```python
def test_inventory_match_helper_exact_and_fuzzy():
    from backend.inventory import find_inventory_match

    state = fresh_state()

    exact = find_inventory_match("Proteinase K", state.inventory)
    assert exact is not None
    assert exact.name == "Proteinase K"

    fuzzy = find_inventory_match("proteinase k", state.inventory)
    assert fuzzy is not None
    assert fuzzy.name == "Proteinase K"

    substring = find_inventory_match("master mix", state.inventory)
    assert substring is not None
    assert substring.name == "2X master mix"


def test_inventory_match_helper_miss():
    from backend.inventory import find_inventory_match

    state = fresh_state()
    assert find_inventory_match("unobtainium", state.inventory) is None
```

- [ ] **Step 2: Run the matcher tests and verify failure**

Run:

```bash
python -m pytest tests/test_handlers.py::test_inventory_match_helper_exact_and_fuzzy tests/test_handlers.py::test_inventory_match_helper_miss -q
```

Expected: FAIL because `find_inventory_match` does not exist.

- [ ] **Step 3: Implement `find_inventory_match`**

In `backend/inventory.py`, add this import with the other imports:

```python
import difflib
```

Add this helper after `InventoryItem`:

```python
def find_inventory_match(
    reagent_name: str, items: list[InventoryItem]
) -> Optional[InventoryItem]:
    """Return the best inventory item for a reagent name.

    Mirrors the existing command handler behavior: fuzzy match first, then
    substring fallback. Returns None instead of guessing when there is no match.
    """
    query = (reagent_name or "").strip()
    if not query:
        return None

    names = [item.name for item in items]
    matches = difflib.get_close_matches(
        query.lower(), [name.lower() for name in names], n=1, cutoff=0.6
    )
    if matches:
        matched_name = matches[0]
        return next(item for item in items if item.name.lower() == matched_name)

    for item in items:
        if query.lower() in item.name.lower():
            return item

    return None
```

- [ ] **Step 4: Refactor `backend/handlers.py`**

In `backend/handlers.py`, remove the top-level `import difflib` if no other code uses it.

Add this import:

```python
from .inventory import find_inventory_match
```

Replace `_handle_find_inventory()` with:

```python
def _handle_find_inventory(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    if not cmd.reagent_name:
        msg = cmd.clarify_prompt or "Which reagent are you looking for?"
        return [clarify_event(msg)]
    item = find_inventory_match(cmd.reagent_name, state.inventory)
    if item is None:
        return [clarify_event(f"I don't have a record for {cmd.reagent_name}.")]
    return [inventory_result_event(item.name, item.location, item.quantity_approx)]
```

- [ ] **Step 5: Run handler tests**

Run:

```bash
python -m pytest tests/test_handlers.py -q
```

Expected: PASS. Existing find-inventory tests still pass.

- [ ] **Step 6: Commit**

```bash
git add backend/inventory.py backend/handlers.py tests/test_handlers.py
git commit -m "feat: share inventory matching"
```

---

### Task 2: Add Pure Reagent Scaling Module

**Files:**
- Create: `backend/scaling.py`
- Create: `tests/test_scaling.py`

**Interfaces:**
- Produces: `convert_volume(amount: float, from_unit: str, to_unit: str) -> Optional[float]`
- Produces: `aggregate_reagents(protocol: Any) -> dict[str, float]`
- Produces: `scale_reagents(protocol: Any, n_samples: int, overage_pct: float) -> list[dict[str, Any]]`
- Produces: `build_prep_table(protocol: Any, n_samples: int, overage_pct: float, inventory: list[InventoryItem]) -> list[dict[str, Any]]`
- Consumes: `backend.router.normalize_ascii`, `backend.router._canon_unit`, `backend.inventory.find_inventory_match`, `backend.inventory.InventoryItem`

- [ ] **Step 1: Create failing pure-function tests**

Create `tests/test_scaling.py`:

```python
from backend.inventory import InventoryItem
from backend.scaling import (
    aggregate_reagents,
    build_prep_table,
    convert_volume,
    scale_reagents,
)
from backend.state import Protocol, Step


def protocol_with_steps(*steps: Step) -> Protocol:
    return Protocol(id="p", name="Protocol", steps=list(steps))


def test_convert_volume_between_volume_units():
    assert convert_volume(1000, "uL", "mL") == 1
    assert convert_volume(1.5, "mL", "uL") == 1500
    assert convert_volume(0.002, "L", "uL") == 2000
    assert convert_volume(1000, "microliters", "mL") == 1


def test_convert_volume_rejects_non_volume_units():
    assert convert_volume(5, "g", "uL") is None
    assert convert_volume(5, "uL", "g") is None
    assert convert_volume(5, "", "uL") is None
    assert convert_volume(5, "units", "mL") is None


def test_aggregate_reagents_sums_repeated_reagent():
    proto = protocol_with_steps(
        Step(id=1, text="add buffer", parameters={"reagent": "buffer", "volume_ul": 200}),
        Step(id=2, text="spin", parameters={"speed_g": 13000}),
        Step(id=3, text="add buffer again", parameters={"reagent": "buffer", "volume_ul": 50}),
        Step(id=4, text="add water", parameters={"reagent": "water", "volume_ul": 10.5}),
    )

    assert aggregate_reagents(proto) == {"buffer": 250.0, "water": 10.5}


def test_aggregate_reagents_ignores_missing_or_non_numeric_values():
    proto = protocol_with_steps(
        Step(id=1, text="missing reagent", parameters={"volume_ul": 200}),
        Step(id=2, text="missing volume", parameters={"reagent": "buffer"}),
        Step(id=3, text="string volume", parameters={"reagent": "buffer", "volume_ul": "200"}),
        Step(id=4, text="boolean volume", parameters={"reagent": "buffer", "volume_ul": True}),
    )

    assert aggregate_reagents(proto) == {}


def test_scale_reagents_for_dna_extraction_numbers():
    proto = protocol_with_steps(
        Step(id=1, text="lysis", parameters={"reagent": "lysis buffer", "volume_ul": 200}),
        Step(id=2, text="ethanol", parameters={"reagent": "ethanol", "volume_ul": 200}),
        Step(id=3, text="water", parameters={"reagent": "nuclease-free water", "volume_ul": 50}),
    )

    rows = scale_reagents(proto, n_samples=12, overage_pct=10)

    by_name = {row["reagent"]: row for row in rows}
    assert by_name["lysis buffer"]["total_ul"] == 2640
    assert by_name["lysis buffer"]["total_display"] == "2.64 mL"
    assert by_name["ethanol"]["total_ul"] == 2640
    assert by_name["nuclease-free water"]["total_ul"] == 660
    assert by_name["nuclease-free water"]["total_display"] == "660 uL"


def test_scale_reagents_rejects_bad_inputs():
    proto = protocol_with_steps(
        Step(id=1, text="lysis", parameters={"reagent": "lysis buffer", "volume_ul": 200})
    )

    try:
        scale_reagents(proto, n_samples=0, overage_pct=10)
    except ValueError as exc:
        assert "sample count" in str(exc).lower()
    else:
        raise AssertionError("sample_count=0 should fail")

    try:
        scale_reagents(proto, n_samples=1, overage_pct=-1)
    except ValueError as exc:
        assert "overage" in str(exc).lower()
    else:
        raise AssertionError("negative overage should fail")


def test_build_prep_table_inventory_verdicts():
    proto = protocol_with_steps(
        Step(id=1, text="ethanol", parameters={"reagent": "ethanol", "volume_ul": 200}),
        Step(id=2, text="water", parameters={"reagent": "water", "volume_ul": 50}),
        Step(id=3, text="proteinase", parameters={"reagent": "Proteinase K", "volume_ul": 200}),
        Step(id=4, text="agarose", parameters={"reagent": "agarose", "volume_ul": 100}),
        Step(id=5, text="lysis", parameters={"reagent": "lysis buffer", "volume_ul": 200}),
        Step(id=6, text="soc", parameters={"reagent": "SOC medium", "volume_ul": 100}),
    )
    inventory = [
        InventoryItem(name="Ethanol 70%", amount="1000", unit="mL", location="Cabinet", quantity_approx="", status="ok"),
        InventoryItem(name="Water", amount="500", unit="uL", location="Fridge", quantity_approx="", status="ok"),
        InventoryItem(name="Proteinase K", amount="2", unit="uL", location="Freezer", quantity_approx="", status="low"),
        InventoryItem(name="Agarose", amount="500", unit="g", location="Cabinet", quantity_approx="", status="ok"),
        InventoryItem(name="SOC medium", amount="100", unit="mL", location="Fridge", quantity_approx="", status="critical"),
    ]

    rows = build_prep_table(proto, n_samples=2, overage_pct=0, inventory=inventory)
    by_name = {row["reagent"]: row for row in rows}

    assert by_name["ethanol"]["verdict"] == "in_stock"
    assert by_name["ethanol"]["match_name"] == "Ethanol 70%"

    assert by_name["water"]["verdict"] == "in_stock"

    assert by_name["Proteinase K"]["verdict"] == "insufficient"
    assert by_name["Proteinase K"]["shortage_ul"] == 398
    assert by_name["Proteinase K"]["status"] == "low"

    assert by_name["agarose"]["verdict"] == "unknown_unit"
    assert by_name["agarose"]["available_unit"] == "g"

    assert by_name["lysis buffer"]["verdict"] == "missing"
    assert by_name["lysis buffer"]["match_name"] is None

    assert by_name["SOC medium"]["verdict"] == "critical"
```

- [ ] **Step 2: Run pure tests and verify failure**

Run:

```bash
python -m pytest tests/test_scaling.py -q
```

Expected: FAIL because `backend.scaling` does not exist.

- [ ] **Step 3: Implement `backend/scaling.py`**

Create `backend/scaling.py`:

```python
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
        item = find_inventory_match(str(row["reagent"]), inventory)
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
```

- [ ] **Step 4: Run pure tests**

Run:

```bash
python -m pytest tests/test_scaling.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scaling.py tests/test_scaling.py
git commit -m "feat: scale protocol reagents"
```

---

### Task 3: Add Read-Only Scale Endpoint

**Files:**
- Modify: `backend/main.py`
- Create: `tests/test_scale_endpoint.py`

**Interfaces:**
- Consumes: `build_prep_table(protocol, n_samples, overage_pct, inventory)`
- Produces: `POST /api/scale`
- Request shape: `{"sample_count": int, "overage_percent": float = 10.0, "protocol_id": str | None = None}`
- Response shape: `{"ok": True, "protocol_id": str, "protocol_name": str, "sample_count": int, "overage_percent": float, "reagents": list[dict]}`

- [ ] **Step 1: Create failing endpoint tests**

Create `tests/test_scale_endpoint.py`:

```python
import os
import shutil
from pathlib import Path

os.environ["LAB_DB_PATH"] = ":memory:"

from fastapi.testclient import TestClient

import backend.main as main
from backend.state import SessionState

SHIPPED_DATA = Path(__file__).resolve().parents[1] / "backend" / "data"


def _scratch_data_dir(tmp_path: Path) -> Path:
    dst = tmp_path / "data"
    (dst / "protocols").mkdir(parents=True)
    for yaml_file in (SHIPPED_DATA / "protocols").glob("*.yaml"):
        shutil.copy(yaml_file, dst / "protocols" / yaml_file.name)
    shutil.copy(SHIPPED_DATA / "inventory.csv", dst / "inventory.csv")
    return dst


def make_client(tmp_path):
    main.state = SessionState(db_path=":memory:", data_dir=_scratch_data_dir(tmp_path))
    main.state.load_files()
    return TestClient(main.app)


def test_scale_endpoint_uses_explicit_protocol_id(tmp_path):
    with make_client(tmp_path) as client:
        r = client.post(
            "/api/scale",
            json={"protocol_id": "dna_extraction", "sample_count": 12, "overage_percent": 10},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["protocol_id"] == "dna_extraction"
    assert body["protocol_name"] == "DNA Extraction"
    assert body["sample_count"] == 12
    assert body["overage_percent"] == 10

    rows = {row["reagent"]: row for row in body["reagents"]}
    assert rows["lysis buffer"]["total_ul"] == 2640
    assert rows["lysis buffer"]["verdict"] == "missing"
    assert rows["ethanol"]["total_display"] == "2.64 mL"
    assert rows["ethanol"]["verdict"] == "in_stock"
    assert rows["nuclease-free water"]["total_ul"] == 660
    assert rows["nuclease-free water"]["verdict"] == "in_stock"


def test_scale_endpoint_defaults_to_active_protocol(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/protocols/dna_extraction/load")
        r = client.post("/api/scale", json={"sample_count": 2, "overage_percent": 0})

    assert r.status_code == 200
    body = r.json()
    assert body["protocol_id"] == "dna_extraction"
    assert body["sample_count"] == 2


def test_scale_endpoint_rejects_bad_inputs(tmp_path):
    with make_client(tmp_path) as client:
        r1 = client.post("/api/scale", json={"protocol_id": "dna_extraction", "sample_count": 0})
        r2 = client.post(
            "/api/scale",
            json={"protocol_id": "dna_extraction", "sample_count": 1, "overage_percent": -1},
        )

    assert r1.status_code == 422
    assert "sample count" in r1.json()["detail"].lower()
    assert r2.status_code == 422
    assert "overage" in r2.json()["detail"].lower()


def test_scale_endpoint_404_without_protocol(tmp_path):
    with make_client(tmp_path) as client:
        r = client.post("/api/scale", json={"sample_count": 1})

    assert r.status_code == 404
    assert "load a protocol" in r.json()["detail"].lower()


def test_scale_endpoint_404_unknown_protocol_id(tmp_path):
    with make_client(tmp_path) as client:
        r = client.post(
            "/api/scale",
            json={"protocol_id": "does_not_exist", "sample_count": 1},
        )

    assert r.status_code == 404
    assert "unknown protocol" in r.json()["detail"].lower()
```

- [ ] **Step 2: Run endpoint tests and verify failure**

Run:

```bash
python -m pytest tests/test_scale_endpoint.py -q
```

Expected: FAIL because `/api/scale` does not exist.

- [ ] **Step 3: Modify imports in `backend/main.py`**

Add this import near the other backend imports:

```python
from .scaling import build_prep_table
```

- [ ] **Step 4: Add `ScaleIn` and endpoint in `backend/main.py`**

Place this after `list_inventory()` or near other read snapshot endpoints:

```python
class ScaleIn(BaseModel):
    sample_count: int
    overage_percent: float = 10.0
    protocol_id: str | None = None


@app.post("/api/scale")
async def scale_protocol(body: ScaleIn) -> dict[str, Any]:
    """Read-only reagent scaling for the active or selected protocol."""
    if body.sample_count < 1:
        raise HTTPException(status_code=422, detail="sample count must be at least 1")
    if body.overage_percent < 0:
        raise HTTPException(status_code=422, detail="overage percent must be non-negative")

    if body.protocol_id:
        proto = state.protocols.get(body.protocol_id)
        if proto is None:
            raise HTTPException(status_code=404, detail=f"unknown protocol: {body.protocol_id}")
    else:
        proto = state.active_protocol
        if proto is None:
            raise HTTPException(status_code=404, detail="load a protocol or provide protocol_id")

    rows = build_prep_table(
        proto,
        n_samples=body.sample_count,
        overage_pct=body.overage_percent,
        inventory=state.inventory,
    )
    return {
        "ok": True,
        "protocol_id": proto.id,
        "protocol_name": proto.name,
        "sample_count": body.sample_count,
        "overage_percent": body.overage_percent,
        "reagents": rows,
    }
```

- [ ] **Step 5: Run endpoint and pure tests**

Run:

```bash
python -m pytest tests/test_scaling.py tests/test_scale_endpoint.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/main.py tests/test_scale_endpoint.py
git commit -m "feat: expose reagent scale endpoint"
```

---

### Task 4: Add Dashboard Reagent Prep Panel

**Files:**
- Modify: `FrontendTest/dashboard.html`
- Modify: `FrontendTest/app.js`
- Modify: `FrontendTest/dashboard.css`
- Modify: `tests/test_frontend_static.py`

**Interfaces:**
- Consumes: `POST /api/scale`
- Produces DOM hooks: `prep-samples`, `prep-overage`, `prep-compute`, `prep-table`
- Produces client functions: `fetchScale`, `renderPrepTable`, `handlePrepCompute`
- Exposes optional API on `window.LabClient`: `fetchScale`, `renderPrepTable`

- [ ] **Step 1: Add failing static tests**

Append these tests to `tests/test_frontend_static.py`:

```python
def test_dashboard_has_reagent_prep_panel_hooks():
    html = (FT / "dashboard.html").read_text(encoding="utf-8")
    for token in ("prep-samples", "prep-overage", "prep-compute", "prep-table"):
        assert token in html, f"dashboard.html missing {token}"


def test_app_wires_reagent_prep_client():
    js = (FT / "app.js").read_text(encoding="utf-8")
    for token in (
        "/api/scale",
        "fetchScale",
        "renderPrepTable",
        "handlePrepCompute",
        "prep-compute",
        "prep-table",
    ):
        assert token in js, f"app.js missing {token}"
```

- [ ] **Step 2: Run static tests and verify failure**

Run:

```bash
python -m pytest tests/test_frontend_static.py::test_dashboard_has_reagent_prep_panel_hooks tests/test_frontend_static.py::test_app_wires_reagent_prep_client -q
```

Expected: FAIL because the panel is not wired yet.

- [ ] **Step 3: Add panel markup to `FrontendTest/dashboard.html`**

In `FrontendTest/dashboard.html`, inside the left `<section class="col-span-8 space-y-gutter">`, add this block after the protocol cards section:

```html
<section class="glass-panel rounded-lg overflow-hidden border border-outline-variant">
  <div class="bg-primary/5 p-4 border-b border-outline-variant flex flex-col gap-1">
    <h3 class="text-sm font-bold text-primary uppercase tracking-widest">Reagent Prep</h3>
    <p class="text-xs text-on-surface-variant">Scale protocol reagents from structured step parameters.</p>
  </div>
  <div class="p-4 space-y-4">
    <div class="grid grid-cols-1 md:grid-cols-[1fr_1fr_auto] gap-3 items-end">
      <label class="block">
        <span class="text-xs font-bold uppercase text-on-surface-variant">Samples</span>
        <input id="prep-samples" type="number" min="1" value="12" class="mt-1 w-full bg-surface-container-lowest border border-outline-variant rounded-lg px-3 py-2 text-on-surface" />
      </label>
      <label class="block">
        <span class="text-xs font-bold uppercase text-on-surface-variant">Overage %</span>
        <input id="prep-overage" type="number" min="0" value="10" class="mt-1 w-full bg-surface-container-lowest border border-outline-variant rounded-lg px-3 py-2 text-on-surface" />
      </label>
      <button id="prep-compute" type="button" class="bg-primary text-on-primary px-4 py-2 rounded-lg font-bold hover:bg-primary/90 transition-all">Compute</button>
    </div>
    <div id="prep-table" class="text-sm text-on-surface-variant" aria-live="polite">
      Load a protocol to calculate scaled reagents.
    </div>
  </div>
</section>
```

- [ ] **Step 4: Add scale client helpers in `FrontendTest/app.js`**

Add this after `fetchState()`:

```javascript
  async function fetchScale(body) {
    const res = await fetch("/api/scale", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Could not scale reagents");
    }
    return data;
  }
```

Add these render helpers near `renderInventory(items)`:

```javascript
  function prepVerdictClass(verdict) {
    if (verdict === "in_stock") return "text-secondary";
    if (verdict === "insufficient" || verdict === "critical" || verdict === "missing") return "text-error";
    return "text-tertiary";
  }

  function prepVerdictLabel(row) {
    if (row.verdict === "in_stock") return "In stock";
    if (row.verdict === "unknown_unit") return "Check units";
    if (row.verdict === "insufficient") {
      return `Short ${row.shortage_ul} uL`;
    }
    if (row.verdict === "missing") return "Missing";
    return row.verdict;
  }

  function renderPrepTable(data) {
    const mount = $("prep-table");
    if (!mount) return;
    const rows = data.reagents || [];
    if (!rows.length) {
      mount.innerHTML = `<div class="text-on-surface-variant">No scalable reagent volumes found in this protocol.</div>`;
      return;
    }
    mount.innerHTML = `
      <div class="overflow-x-auto">
        <table class="prep-table w-full text-left border-collapse">
          <thead>
            <tr class="text-xs uppercase text-on-surface-variant border-b border-outline-variant">
              <th class="py-2 pr-3">Reagent</th>
              <th class="py-2 pr-3">Per sample</th>
              <th class="py-2 pr-3">Run</th>
              <th class="py-2 pr-3">Total</th>
              <th class="py-2 pr-3">Availability</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map((row) => `
              <tr class="border-b border-outline-variant/60">
                <td class="py-3 pr-3 text-on-surface font-medium">${escapeHtml(row.reagent)}</td>
                <td class="py-3 pr-3 font-data-label">${escapeHtml(row.per_sample_ul)} uL</td>
                <td class="py-3 pr-3 font-data-label">${escapeHtml(row.n_samples)} + ${escapeHtml(row.overage_pct)}%</td>
                <td class="py-3 pr-3 font-data-label text-on-surface">${escapeHtml(row.total_display)}</td>
                <td class="py-3 pr-3">
                  <div class="${prepVerdictClass(row.verdict)} font-bold">${escapeHtml(prepVerdictLabel(row))}</div>
                  <div class="text-xs text-on-surface-variant">${escapeHtml(row.match_name || "No inventory match")}</div>
                </td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    `;
  }
```

Add this function near other UI handlers:

```javascript
  async function handlePrepCompute() {
    const table = $("prep-table");
    if (!table) return;
    const samples = Number($("prep-samples")?.value || 0);
    const overage = Number($("prep-overage")?.value || 0);
    table.textContent = "Calculating reagent prep...";
    try {
      const data = await fetchScale({
        sample_count: samples,
        overage_percent: overage
      });
      renderPrepTable(data);
    } catch (err) {
      table.innerHTML = `<div class="text-error">${escapeHtml(err.message || String(err))}</div>`;
    }
  }
```

- [ ] **Step 5: Wire the panel hook-guarded**

Find the DOMContentLoaded block or startup wiring in `FrontendTest/app.js` and add:

```javascript
      const prepButton = $("prep-compute");
      if (prepButton) {
        prepButton.addEventListener("click", handlePrepCompute);
      }
```

In `onCommandResult(p)`, replace the current `"step_change"` case:

```javascript
      case "step_change":
        return renderStep(p);
```

with:

```javascript
      case "step_change":
        renderStep(p);
        if ($("prep-table")) handlePrepCompute();
        return;
```

In `hydrate()`, inside the `"protocol state"` hydrate section, after the existing call that renders the current step state, add:

```javascript
      if ($("prep-table") && state.step) handlePrepCompute();
```

Keep both hooks guarded so pages without the panel are unaffected.

- [ ] **Step 6: Expose helpers on `window.LabClient`**

In the `window.LabClient = { ... }` object, add:

```javascript
    fetchScale,
    renderPrepTable,
```

- [ ] **Step 7: Add optional table styling**

If the table needs local polish, add this to `FrontendTest/dashboard.css`:

```css
.prep-table th,
.prep-table td {
  vertical-align: top;
}
```

- [ ] **Step 8: Run static frontend tests**

Run:

```bash
python -m pytest tests/test_frontend_static.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add FrontendTest/dashboard.html FrontendTest/app.js FrontendTest/dashboard.css tests/test_frontend_static.py
git commit -m "feat: show reagent prep table"
```

---

### Task 5: Docs and Full Verification

**Files:**
- Modify: `README.md`
- Read: all changed files

**Interfaces:**
- Consumes: `/api/scale`, Reagent Prep dashboard panel.
- Produces: documented demo flow.

- [ ] **Step 1: Update README demo section**

In `README.md`, add this paragraph after the protocol import section:

```markdown
Reagent prep scaling: `POST /api/scale` computes a deterministic prep table for
the active protocol or a provided `protocol_id`. It reads structured protocol
parameters (`reagent`, `volume_ul`), scales totals by sample count and overage,
and compares those totals against inventory without calling the LLM or mutating
lab state. In the dashboard, load DNA Extraction, enter `12` samples and `10`
percent overage, then compute: ethanol and nuclease-free water should show in
stock, while lysis buffer should show missing if it is not in inventory.
```

- [ ] **Step 2: Run targeted tests**

Run:

```bash
python -m pytest tests/test_scaling.py tests/test_scale_endpoint.py tests/test_handlers.py tests/test_frontend_static.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full suite**

Run:

```bash
python -m pytest -q
```

Expected: PASS or only known pre-existing failures recorded in Task 0.

- [ ] **Step 4: Verify no frontend API key references**

Run:

```bash
rg "ANTHROPIC_API_KEY|DEEPGRAM_API_KEY|LAB_MODEL" FrontendTest frontend
```

Expected: no matches in served frontend code.

- [ ] **Step 5: Manual endpoint smoke test**

Start the server:

```bash
uvicorn backend.main:app --reload
```

In a second terminal, run:

```bash
curl -X POST http://127.0.0.1:8000/api/scale -H "content-type: application/json" -d "{\"protocol_id\":\"dna_extraction\",\"sample_count\":12,\"overage_percent\":10}"
```

Expected JSON facts:

```text
ok = true
protocol_id = dna_extraction
lysis buffer total_ul = 2640 and verdict = missing
ethanol total_display = 2.64 mL and verdict = in_stock
nuclease-free water total_ul = 660 and verdict = in_stock
```

- [ ] **Step 6: Manual UI smoke test**

Open:

```text
http://127.0.0.1:8000
```

Steps:

1. Load DNA Extraction from a protocol card or by voice/typed command.
2. In Reagent Prep, set Samples to `12` and Overage to `10`.
3. Click Compute.
4. Confirm the table shows lysis buffer, ethanol, and nuclease-free water.
5. Confirm lysis buffer is `Missing`, ethanol is `In stock`, and nuclease-free water is `In stock`.
6. Run the existing demo log mismatch path to confirm reproducibility flags still work.

- [ ] **Step 7: Commit**

```bash
git add README.md
git commit -m "docs: document reagent scaling"
```

---

## Final Acceptance Criteria

- `python -m pytest -q` passes, except any pre-existing failures captured before Task 1.
- `/api/scale` works with `protocol_id`.
- `/api/scale` works with the active protocol when `protocol_id` is omitted.
- `/api/scale` returns 422 for `sample_count < 1`.
- `/api/scale` returns 422 for `overage_percent < 0`.
- `/api/scale` returns 404 when no active protocol exists and no `protocol_id` is provided.
- Scaling does not call `route()`, `handle_command()`, `manager.broadcast()`, or any LLM client.
- Existing voice, timer, notebook, import, inventory, and reproducibility tests remain green.
- The dashboard Reagent Prep panel is hook-guarded and does not break other `FrontendTest/` pages.
- The UI escapes table content with `escapeHtml`.
- Manual demo works for DNA Extraction at 12 samples and 10 percent overage.

## Notes for Devin

- The value proposition is "AI interprets, deterministic code computes." Keep that boundary obvious in names, comments, and demo behavior.
- If `advance_step()` or unrelated tests are already failing in the dirty worktree, do not repair that as part of this plan unless the failure blocks scaling tests.
- Do not add an extraction-review screen in this plan. That is a separate feature.
- Do not add a run-summary report in this plan. That is a separate feature.
- Do not parse free-text protocol steps in the scaler. The scaler only trusts structured `parameters`.
