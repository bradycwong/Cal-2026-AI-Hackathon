"""Demo-baseline edits: the heat-shock step split + the seeded lysis buffer.

These guard two demo-data changes:
  * Bacterial Transformation step 3 ("heat shock ... then return to ice") used to
    drive two timers from one step; it is now split into two single-timer steps.
  * "Lysis buffer" ships in the seed (factory) inventory, so a demo reset surfaces
    it at Freezer 3 even though the working copy starts without it.
"""

import os
import shutil
from pathlib import Path

os.environ["LAB_DB_PATH"] = ":memory:"

from backend.state import SessionState

SHIPPED_DATA = Path(__file__).resolve().parents[1] / "backend" / "data"


def _scratch_data_dir(tmp_path: Path) -> Path:
    dst = tmp_path / "data"
    (dst / "protocols").mkdir(parents=True)
    for yaml_file in (SHIPPED_DATA / "protocols").glob("*.yaml"):
        shutil.copy(yaml_file, dst / "protocols" / yaml_file.name)
    shutil.copy(SHIPPED_DATA / "inventory.csv", dst / "inventory.csv")
    return dst


def test_bacterial_transformation_heat_shock_split_into_single_timer_steps():
    state = SessionState()
    state.load_files()
    steps = state.protocols["bacterial_transformation"].steps

    assert len(steps) == 6  # was 5; step 3 split into heat-shock + back-on-ice

    heat = steps[2]
    assert "heat shock" in heat.text.lower()
    assert heat.duration_s == 45

    ice = steps[3]
    assert "ice" in ice.text.lower()
    assert ice.duration_s == 120  # the 2-minute return-to-ice now has its own timer

    # No remaining step bundles a second timed action into one step.
    for s in steps:
        assert not (s.duration_s and "then" in s.text.lower()), s.text


def test_factory_reset_surfaces_seeded_lysis_buffer(tmp_path):
    # A factory reset restores inventory from the seed baseline, wiping any
    # working-copy drift. The seed ships exactly one lysis buffer: 100 uL in
    # Freezer 3 (so "where is the lysis buffer" lands on the clean entry).
    state = SessionState(db_path=":memory:", data_dir=_scratch_data_dir(tmp_path))
    state.load_files()
    state.restore_factory_state()

    matches = [i for i in state.inventory if i.name.lower() == "lysis buffer"]
    assert len(matches) == 1
    item = matches[0]
    assert item.location == "Freezer 3"
    assert item.amount == "100"
    assert item.unit == "uL"
