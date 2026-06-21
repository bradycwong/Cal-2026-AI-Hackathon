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


INGREDIENT_PROTOCOL = """\
protocol:
  id: ing_demo
  name: Ing Demo
  steps:
    - id: 1
      text: "Add 200 uL lysis buffer."
      parameters: { volume_ul: 200, reagent: "lysis buffer" }
    - id: 2
      text: "Incubate 10 minutes."
      duration_s: 600
"""


def test_protocol_catalog_includes_ingredient_amounts(tmp_path):
    state = fresh_state(tmp_path)
    state.add_protocol_from_text(INGREDIENT_PROTOCOL)
    entry = next(p for p in state.protocol_catalog() if p["id"] == "ing_demo")
    # Additive field: names stay in `reagents`, name+amount lands in `ingredients`.
    assert entry["reagents"] == ["lysis buffer"]
    assert entry["ingredients"] == [
        {"reagent": "lysis buffer", "volume_ul": 200, "display": "200 uL"}
    ]


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


def test_add_inventory_item_persists_structured_amount(tmp_path):
    state = fresh_state(tmp_path)
    item = state.add_inventory_item(
        "Agarose",
        "Cabinet 2",
        amount="500",
        unit="g",
    )
    assert item.amount == "500"
    assert item.unit == "g"
    assert item.quantity_approx == "500 g"

    reloaded = SessionState(data_dir=tmp_path)
    reloaded.load_files()
    assert reloaded.inventory[0].amount == "500"
    assert reloaded.inventory[0].unit == "g"
    assert reloaded.inventory[0].quantity_approx == "500 g"


def test_add_inventory_item_migrates_legacy_csv_columns(tmp_path):
    # Legacy CSV still carries an `expiration` column with a value; the loader
    # tolerates it and the next write drops the column (and the value) entirely.
    (tmp_path / "inventory.csv").write_text(
        "name,location,quantity_approx,notes,code,category,date,expiration,status\n"
        "Proteinase K,Freezer 2 shelf B,~4 doses,10 mg/mL aliquots,PRK-2001-A,Enzyme,2024-02-10,2025-01-01,low\n",
        encoding="utf-8",
    )
    state = fresh_state(tmp_path)
    state.add_inventory_item("Agarose", "Cabinet 2", amount="500", unit="g")

    csv_text = (tmp_path / "inventory.csv").read_text(encoding="utf-8")
    assert csv_text.splitlines()[0] == (
        "name,amount,unit,location,quantity_approx,notes,code,category,date,status"
    )
    assert "2025-01-01" not in csv_text  # legacy expiration value was dropped

    reloaded = SessionState(data_dir=tmp_path)
    reloaded.load_files()
    assert reloaded.inventory[0].quantity_approx == "~4 doses"
    assert reloaded.inventory[0].amount == ""
    assert reloaded.inventory[0].unit == ""
    assert reloaded.inventory[1].quantity_approx == "500 g"
    assert reloaded.inventory[1].amount == "500"
    assert reloaded.inventory[1].unit == "g"


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
        "amount",
        "unit",
        "date",
        "status",
        "notes",
    } <= set(items[0])
    assert "expiration" not in items[0]
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
