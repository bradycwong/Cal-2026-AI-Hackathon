"""Writer checks — add_inventory_item / add_protocol_from_text persist + validate."""

import pytest

from backend.state import ProtocolParseError, SessionState, parse_protocol_text

VALID_PROTOCOL = """\
protocol:
  id: gel_electrophoresis
  name: Gel Electrophoresis
  aliases: ["run a gel", "gel"]
  steps:
    - id: 1
      text: "Pour the gel."
      duration_s: null
    - id: 2
      text: "Run at 100 V."
      duration_s: 2400
      timer_label: electrophoresis
"""


def fresh_state(tmp_path) -> SessionState:
    state = SessionState(data_dir=tmp_path)
    state.load_files()
    return state


def test_add_inventory_item_persists_and_is_in_memory(tmp_path):
    state = fresh_state(tmp_path)
    item = state.add_inventory_item("Agarose", "Cabinet 2", "~500 g", "mol bio grade")
    assert item.name == "Agarose"
    assert state.inventory[-1].name == "Agarose"

    # Survives a "restart": a fresh state reading the same dir sees the row.
    reloaded = SessionState(data_dir=tmp_path)
    reloaded.load_files()
    names = [i.name for i in reloaded.inventory]
    assert names == ["Agarose"]
    assert reloaded.inventory[0].location == "Cabinet 2"
    assert reloaded.inventory[0].notes == "mol bio grade"


def test_add_inventory_item_appends_to_existing_csv(tmp_path):
    state = fresh_state(tmp_path)
    state.add_inventory_item("Agarose", "Cabinet 2")
    state.add_inventory_item("SYBR Safe", "Fridge 2", "~1 mL")
    reloaded = SessionState(data_dir=tmp_path)
    reloaded.load_files()
    assert [i.name for i in reloaded.inventory] == ["Agarose", "SYBR Safe"]


def test_add_inventory_item_quotes_commas(tmp_path):
    state = fresh_state(tmp_path)
    state.add_inventory_item("EDTA", "Fridge 1", "~50 mL", "see also: x, y, z")
    reloaded = SessionState(data_dir=tmp_path)
    reloaded.load_files()
    assert reloaded.inventory[0].notes == "see also: x, y, z"


def test_add_inventory_item_rejects_blank_name(tmp_path):
    state = fresh_state(tmp_path)
    with pytest.raises(ValueError):
        state.add_inventory_item("   ")
    assert state.inventory == []


def test_add_protocol_from_text_registers_and_persists(tmp_path):
    state = fresh_state(tmp_path)
    proto = state.add_protocol_from_text(VALID_PROTOCOL)
    assert proto.id == "gel_electrophoresis"
    assert state.find_protocol("run a gel") is proto
    assert (tmp_path / "protocols" / "gel_electrophoresis.yaml").exists()

    reloaded = SessionState(data_dir=tmp_path)
    reloaded.load_files()
    assert "gel_electrophoresis" in reloaded.protocols
    assert len(reloaded.protocols["gel_electrophoresis"].steps) == 2


def test_re_upload_overwrites_same_id(tmp_path):
    state = fresh_state(tmp_path)
    state.add_protocol_from_text(VALID_PROTOCOL)
    state.add_protocol_from_text(VALID_PROTOCOL.replace("Gel Electrophoresis", "Gel v2"))
    assert len(state.protocols) == 1
    assert state.protocols["gel_electrophoresis"].name == "Gel v2"


def test_protocol_catalog_shape():
    state = SessionState()
    state.load_files()
    catalog = state.protocol_catalog()
    assert catalog
    assert {
        "id",
        "name",
        "description",
        "category",
        "status",
        "est_duration_min",
        "duration_s",
        "duration_label",
        "step_count",
        "reagents",
        "aliases",
    } <= set(catalog[0])
    assert catalog[0]["status"] in {"READY", "LOW_REAGENTS", "ARCHIVED"}


def test_inventory_view_shape():
    state = SessionState()
    state.load_files()
    items = state.inventory_view()
    assert items
    assert {
        "name",
        "code",
        "location",
        "category",
        "quantity_approx",
        "date",
        "status",
        "notes",
    } <= set(items[0])
    assert items[0]["status"] in {"ok", "low", "critical", "expiring"}


@pytest.mark.parametrize(
    "bad",
    [
        "just a string",
        "protocol: {}",
        "protocol:\n  id: x\n  name: y\n",  # no steps
        "protocol:\n  id: x\n  name: y\n  steps:\n    - text: no id\n",
        "protocol: [1, 2, 3",  # not valid YAML
    ],
)
def test_parse_protocol_text_rejects_malformed(bad):
    with pytest.raises(ProtocolParseError):
        parse_protocol_text(bad)
