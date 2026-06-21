"""Protocol edit: state.update_protocol id-stability + validation guard."""

import shutil
from pathlib import Path

import pytest

from backend.state import ProtocolParseError, SessionState, load_protocol_file

SHIPPED_DATA = Path(__file__).resolve().parents[1] / "backend" / "data"


def _state(tmp_path: Path) -> SessionState:
    """A SessionState over a scratch copy of the shipped protocols."""
    data = tmp_path / "data"
    (data / "protocols").mkdir(parents=True)
    for y in (SHIPPED_DATA / "protocols").glob("*.yaml"):
        shutil.copy(y, data / "protocols" / y.name)
    shutil.copy(SHIPPED_DATA / "inventory.csv", data / "inventory.csv")
    state = SessionState(data_dir=data, db_path=":memory:")
    state.load_files()
    return state


def _steps(state, pid="dna_extraction"):
    return [
        {"text": s.text, "duration_s": s.duration_s,
         "timer_label": s.timer_label, "parameters": s.parameters}
        for s in state.protocols[pid].steps
    ]


def test_update_protocol_keeps_id_stable(tmp_path):
    state = _state(tmp_path)
    proto = state.update_protocol("dna_extraction", "Renamed DNA", "desc", _steps(state))
    assert proto.id == "dna_extraction"  # id frozen across rename
    assert (tmp_path / "data" / "protocols" / "dna_extraction.yaml").exists()
    # the new name is voice-loadable, the new object is the registered one
    assert state.find_protocol("Renamed DNA") is proto
    assert state.protocols["dna_extraction"] is proto


def test_update_protocol_persists_to_disk(tmp_path):
    state = _state(tmp_path)
    steps = [{"text": "Only step", "duration_s": 120,
              "timer_label": "wait", "parameters": {}}]
    state.update_protocol("dna_extraction", "Solo", "", steps)
    reloaded = load_protocol_file(tmp_path / "data" / "protocols" / "dna_extraction.yaml")
    assert reloaded.name == "Solo"
    assert [s.text for s in reloaded.steps] == ["Only step"]
    assert reloaded.steps[0].duration_s == 120
    assert reloaded.steps[0].timer_label == "wait"


def test_update_protocol_invalid_raises_without_corrupting(tmp_path):
    state = _state(tmp_path)
    before = load_protocol_file(tmp_path / "data" / "protocols" / "dna_extraction.yaml")
    with pytest.raises(ProtocolParseError):
        state.update_protocol("dna_extraction", "X", "", [])  # no steps
    after = load_protocol_file(tmp_path / "data" / "protocols" / "dna_extraction.yaml")
    assert [s.text for s in after.steps] == [s.text for s in before.steps]
