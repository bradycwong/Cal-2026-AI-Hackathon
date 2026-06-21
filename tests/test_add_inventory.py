"""Voice/typed add-to-inventory: deterministic routing + handler defaults.

Forces the deterministic router (no network) and isolates every write to a
``tmp_path`` data dir so the shipped inventory.csv is never touched.
"""

import os

os.environ["ROUTER_MODE"] = "deterministic"

from backend.handlers import handle_command
from backend.router import route
from backend.schema import Command
from backend.state import SessionState


def fresh_state(tmp_path) -> SessionState:
    state = SessionState(data_dir=tmp_path)
    state.load_files()
    return state


# --- router: phrase -> add_inventory Command --------------------------------

def test_route_add_inventory_full_phrase():
    cmd = route("Add 5g of EDTA on shelf 4 to the inventory.")
    assert cmd.intent == "add_inventory"
    assert cmd.reagent_name == "EDTA"
    assert cmd.amount == "5"
    assert cmd.unit == "g"
    assert cmd.location == "shelf 4"


def test_route_add_inventory_spelled_out_unit():
    cmd = route("Add 250 milliliters of lysis buffer in Fridge 1 to inventory.")
    assert cmd.intent == "add_inventory"
    assert cmd.reagent_name.lower() == "lysis buffer"
    assert cmd.amount == "250"
    assert cmd.unit == "mL"
    assert cmd.location == "Fridge 1"


def test_route_add_inventory_ignores_expiration_phrase():
    # The "expires ..." phrase is stripped so it can't pollute location/name;
    # expiration itself is no longer parsed or stored.
    cmd = route("Add 100 uL of Taq polymerase on shelf 2 expires 2026-12-01 to the inventory.")
    assert cmd.intent == "add_inventory"
    assert cmd.reagent_name.lower() == "taq polymerase"
    assert cmd.amount == "100"
    assert cmd.unit == "uL"
    assert cmd.location == "shelf 2"


def test_route_add_inventory_name_only():
    cmd = route("Add EDTA to the inventory.")
    assert cmd.intent == "add_inventory"
    assert cmd.reagent_name == "EDTA"
    assert cmd.amount is None
    assert cmd.unit is None
    assert cmd.location is None


def test_route_add_inventory_no_name_does_not_add():
    # No reagent named -> never invent one; ask instead of adding.
    cmd = route("Add to the inventory.")
    assert cmd.intent != "add_inventory"
    assert cmd.clarify_prompt


# --- handler: defaults + persistence ----------------------------------------

def test_handle_add_inventory_persists_and_emits_event(tmp_path):
    state = fresh_state(tmp_path)
    events = handle_command(
        Command(
            intent="add_inventory",
            reagent_name="EDTA",
            amount="5",
            unit="g",
            location="shelf 4",
        ),
        state,
    )
    assert len(events) == 1
    p = events[0]["payload"]
    assert p["kind"] == "inventory_added"
    assert p["name"] == "EDTA"
    assert p["amount"] == "5"
    assert p["unit"] == "g"
    assert p["location"] == "shelf 4"

    # Persisted: a fresh state reading the same dir sees the row.
    reloaded = SessionState(data_dir=tmp_path)
    reloaded.load_files()
    assert [i.name for i in reloaded.inventory] == ["EDTA"]
    assert reloaded.inventory[0].location == "shelf 4"


def test_handle_add_inventory_missing_fields_default_to_tbd(tmp_path):
    state = fresh_state(tmp_path)
    events = handle_command(
        Command(intent="add_inventory", reagent_name="Mystery reagent"), state
    )
    p = events[0]["payload"]
    assert p["kind"] == "inventory_added"
    # Every unspecified field defaults to TBD.
    assert p["amount"] == "TBD"
    assert p["unit"] == "TBD"
    assert p["location"] == "TBD"


def test_handle_add_inventory_no_name_adds_nothing(tmp_path):
    state = fresh_state(tmp_path)
    events = handle_command(Command(intent="add_inventory"), state)
    assert events[0]["payload"]["kind"] == "clarify"
    assert state.inventory == []  # nothing was added


def test_route_then_handle_end_to_end(tmp_path):
    # The full spine: transcript -> Command -> handler -> persisted row.
    state = fresh_state(tmp_path)
    cmd = route("Add 5g of EDTA on shelf 4 to the inventory.")
    events = handle_command(cmd, state)
    p = events[0]["payload"]
    assert p["kind"] == "inventory_added"
    assert p["name"] == "EDTA"
    assert state.inventory[-1].name == "EDTA"
    assert state.inventory[-1].amount == "5"
    assert state.inventory[-1].unit == "g"
    assert state.inventory[-1].location == "shelf 4"
