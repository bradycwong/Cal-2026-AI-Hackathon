"""Protocol import: registration, deterministic parser, YAML round-trip."""

from pathlib import Path

import pytest

from backend.protocol_import import import_protocol
from backend.state import SessionState, load_protocol_file


VALID_YAML = """\
protocol:
  id: sample_proto
  name: Sample Proto
  version: "1.0"
  description: A tiny protocol.
  category: Imported
  status: READY
  aliases: [sample]
  steps:
    - id: 1
      text: Add 200 uL buffer
      duration_s: null
      parameters: {}
    - id: 2
      text: Incubate 5 minutes
      duration_s: 300
      timer_label: timer
      parameters: {}
"""


def _state(tmp_path: Path) -> SessionState:
    state = SessionState(data_dir=tmp_path, db_path=":memory:")
    (tmp_path / "protocols").mkdir(exist_ok=True)
    return state


def test_register_protocol_makes_it_available(tmp_path):
    state = _state(tmp_path)
    path = tmp_path / "protocols" / "sample_proto.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    proto = state.register_protocol(path)
    assert proto.id == "sample_proto"
    assert state.protocols["sample_proto"] is proto


def test_fallback_import_splits_numbered_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("IMPORT_MODE", "deterministic")
    state = _state(tmp_path)
    proto, path = import_protocol(
        "1. Add 200 uL lysis buffer\n2. Incubate 10 minutes",
        "Quick DNA Prep",
        state,
    )
    assert path.exists()
    assert proto.id == "quick_dna_prep"
    assert len(proto.steps) == 2
    assert proto.steps[1].duration_s == 600
    assert proto.status == "READY"
    assert state.find_protocol("Quick DNA Prep") is proto


def test_import_empty_input_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("IMPORT_MODE", "deterministic")
    state = _state(tmp_path)
    with pytest.raises(ValueError):
        import_protocol("   \n  ", None, state)


def test_import_unnamed_uses_first_line(tmp_path, monkeypatch):
    monkeypatch.setenv("IMPORT_MODE", "deterministic")
    state = _state(tmp_path)
    proto, _ = import_protocol("Mix reagents\nSpin down", None, state)
    assert proto.name == "Mix reagents"
    assert len(proto.steps) == 2


def test_import_duplicate_names_get_unique_slugs(tmp_path, monkeypatch):
    monkeypatch.setenv("IMPORT_MODE", "deterministic")
    state = _state(tmp_path)
    p1, path1 = import_protocol("1. Add buffer", "Dupe", state)
    p2, path2 = import_protocol("1. Add buffer", "Dupe", state)
    assert p1.id != p2.id
    assert path1 != path2
    assert p1.id in state.protocols and p2.id in state.protocols


def test_imported_yaml_round_trips_through_loader(tmp_path, monkeypatch):
    monkeypatch.setenv("IMPORT_MODE", "deterministic")
    state = _state(tmp_path)
    proto, path = import_protocol("1. Add buffer\n2. Wait 2 minutes", "Round Trip", state)
    reloaded = load_protocol_file(path)
    assert reloaded.id == proto.id
    assert [s.text for s in reloaded.steps] == [s.text for s in proto.steps]
    assert reloaded.steps[1].duration_s == 120
