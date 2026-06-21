"""Protocol import: registration, deterministic parser, YAML round-trip."""

import types
from pathlib import Path

import pytest

from backend.protocol_import import (
    _extract_json,
    _step_params,
    _validate_parsed,
    import_protocol,
)
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


# --- deterministic parameter extraction (so offline imports list ingredients) -

def test_step_params_extracts_volume_and_reagent():
    p = _step_params("Add 200 uL lysis buffer to the tube")
    assert p["volume_ul"] == 200
    assert p["reagent"] == "lysis buffer"


def test_step_params_converts_ml_to_ul():
    p = _step_params("Add 1.5 mL of ethanol and mix by inversion")
    assert p["volume_ul"] == 1500
    assert p["reagent"] == "ethanol"


def test_step_params_extracts_temperature():
    assert _step_params("Incubate at 30 degrees C for 10 minutes")["temp_c"] == 30


def test_step_params_empty_when_nothing_explicit():
    assert _step_params("Mix gently and proceed") == {}


def test_deterministic_import_humanizes_inflated_volume(tmp_path, monkeypatch):
    # An inflated microliter mention in the source prose is shown as mL, while the
    # canonical volume_ul param stays the microliter number used for scaling math.
    monkeypatch.setenv("IMPORT_MODE", "deterministic")
    state = _state(tmp_path)
    proto, _ = import_protocol("1. Add 50000 uL buffer", "Inflated", state)
    assert proto.steps[0].text == "Add 50 mL buffer"
    assert proto.steps[0].parameters.get("volume_ul") == 50000


def test_deterministic_import_populates_reagents(tmp_path, monkeypatch):
    monkeypatch.setenv("IMPORT_MODE", "deterministic")
    state = _state(tmp_path)
    proto, _ = import_protocol(
        "1. Add 200 uL lysis buffer\n2. Add 50 uL ethanol\n3. Incubate 10 minutes",
        "Reagent Demo",
        state,
    )
    assert proto.reagents == ["lysis buffer", "ethanol"]
    assert proto.steps[0].parameters.get("volume_ul") == 200


# --- LLM JSON parsing (the messages.create path), exercised offline ----------

# A model response for the canonical compound example, split into atomic steps:
# one untimed prep step plus two independently-timed actions.
COMPOUND_JSON = """\
{
  "name": "Imported Protocol",
  "description": "",
  "aliases": ["imported protocol"],
  "steps": [
    {"text": "Add lysis buffer to tube.", "duration_s": null, "timer_label": null, "parameters": {}},
    {"text": "Centrifuge tube.", "duration_s": 45, "timer_label": "centrifuge", "parameters": {}},
    {"text": "Incubate at 30 degrees C.", "duration_s": 600, "timer_label": "incubation", "parameters": {"temp_c": 30}}
  ]
}"""


def test_validate_parsed_splits_compound_into_timed_steps():
    parsed = _validate_parsed(COMPOUND_JSON, name=None)
    assert [s.duration_s for s in parsed.steps] == [None, 45, 600]
    # timer_label only on the timed steps; the prep step stays unlabeled.
    assert [s.timer_label for s in parsed.steps] == [None, "centrifuge", "incubation"]
    assert parsed.steps[2].parameters == {"temp_c": 30}


def test_extract_json_strips_code_fences_and_prose():
    fenced = 'Here is the protocol:\n```json\n{"name": "X", "steps": []}\n```\nDone.'
    assert _extract_json(fenced).strip() == '{"name": "X", "steps": []}'


def test_validate_parsed_empty_steps_raises():
    with pytest.raises(ValueError):
        _validate_parsed('{"name": "Empty", "steps": []}', name=None)


def test_validate_parsed_autofills_and_strips_timer_label():
    raw = """\
    {"name": "T", "steps": [
      {"text": "Vortex the sample.", "duration_s": 30, "parameters": {}},
      {"text": "Place tube on ice.", "duration_s": null, "timer_label": "leftover", "parameters": {}}
    ]}"""
    parsed = _validate_parsed(raw, name=None)
    # duration set + no label -> filled from the first verb
    assert parsed.steps[0].timer_label == "vortex"
    # duration None -> any stray label is dropped
    assert parsed.steps[1].timer_label is None


def test_validate_parsed_applies_name_override():
    parsed = _validate_parsed(COMPOUND_JSON, name="My Protocol")
    assert parsed.name == "My Protocol"


class _FakeMessages:
    def __init__(self, text):
        self._text = text

    def create(self, **_kwargs):
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(text=self._text)], usage=None
        )


class _FakeAnthropic:
    """Stands in for anthropic.Anthropic so the LLM path runs with no network."""

    _text = COMPOUND_JSON

    def __init__(self, *args, **kwargs):
        self.messages = _FakeMessages(self._text)


def test_llm_import_splits_compound_end_to_end(tmp_path, monkeypatch):
    import anthropic

    monkeypatch.setenv("IMPORT_MODE", "llm")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)

    state = _state(tmp_path)
    proto, path = import_protocol(
        "Centrifuge tube 45 seconds before incubating at 30 C for 10 minutes.",
        "Compound Demo",
        state,
    )

    # Full wiring: prompt -> create -> extract -> validate -> ids -> file -> loader.
    reloaded = load_protocol_file(path)
    assert reloaded.id == proto.id
    assert [s.id for s in reloaded.steps] == [1, 2, 3]
    assert [s.duration_s for s in reloaded.steps] == [None, 45, 600]
    assert reloaded.steps[1].timer_label == "centrifuge"
    assert reloaded.steps[2].timer_label == "incubation"


# A model that (wrongly) inflated "50 mL" into "50000 uL" in the prose; the
# import safety net must humanize the displayed text back to mL.
INFLATED_JSON = """\
{
  "name": "Inflated",
  "description": "Pour 50000 uL into the tank.",
  "aliases": ["inflated"],
  "steps": [
    {"text": "Add 50000 uL buffer.", "duration_s": null, "timer_label": null, "parameters": {"volume_ul": 50000}}
  ]
}"""


class _FakeAnthropicInflated(_FakeAnthropic):
    _text = INFLATED_JSON


def test_llm_import_humanizes_inflated_volume(tmp_path, monkeypatch):
    import anthropic

    monkeypatch.setenv("IMPORT_MODE", "llm")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicInflated)

    state = _state(tmp_path)
    proto, _ = import_protocol("Add 50 mL buffer.", "Inflated", state)
    assert proto.steps[0].text == "Add 50 mL buffer."
    assert proto.steps[0].parameters.get("volume_ul") == 50000
    # the description prose is humanized too
    assert "50 mL" in proto.description and "50000" not in proto.description
