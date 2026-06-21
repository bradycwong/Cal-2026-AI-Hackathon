"""Custom-command aliases — the ingestion spine expands a user-defined trigger
into its mapped built-in phrase BEFORE routing, so a spoken/typed trigger runs
the mapped command instead of falling through to "I didn't understand that"."""

import os

os.environ["LAB_DB_PATH"] = ":memory:"  # set before importing the app module

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.aliases import AliasStore, normalize
from backend.state import SessionState

SHIPPED_DATA = Path(__file__).resolve().parents[1] / "backend" / "data"


def _scratch_data_dir(tmp_path: Path) -> Path:
    dst = tmp_path / "data"
    (dst / "protocols").mkdir(parents=True)
    for yaml_file in (SHIPPED_DATA / "protocols").glob("*.yaml"):
        shutil.copy(yaml_file, dst / "protocols" / yaml_file.name)
    shutil.copy(SHIPPED_DATA / "inventory.csv", dst / "inventory.csv")
    return dst


@pytest.fixture
def client(tmp_path):
    main.state = SessionState(db_path=":memory:", data_dir=_scratch_data_dir(tmp_path))
    main.state.load_files()
    main.aliases.set_all([])  # isolate from other tests / prior state
    with TestClient(main.app) as c:
        yield c
    main.aliases.set_all([])


# --- unit: the store --------------------------------------------------------


def test_normalize_lowercases_drops_punctuation_collapses_space():
    assert normalize("  Fire  It Up! ") == "fire it up"
    assert normalize("Where's the EDTA?") == "wheres the edta"


def test_store_expands_only_exact_normalized_trigger():
    store = AliasStore()
    store.set_all([{"trigger": "Fire it up", "phrase": "load DNA extraction protocol"}])
    assert store.expand("FIRE IT UP!") == "load DNA extraction protocol"
    assert store.expand("fire") is None  # no partial / substring match
    assert len(store) == 1


def test_store_drops_blank_entries():
    store = AliasStore()
    n = store.set_all(
        [{"trigger": "", "phrase": "next step"}, {"trigger": "go", "phrase": ""}]
    )
    assert n == 0


# --- integration: the spine -------------------------------------------------


def _kinds(events):
    return [e["payload"].get("kind") for e in events if e["type"] == "command_result"]


def _clarify_msgs(events):
    return [
        e["payload"].get("message", "")
        for e in events
        if e["type"] == "command_result" and e["payload"].get("kind") == "clarify"
    ]


def test_unknown_trigger_without_alias_is_not_understood(client):
    """Baseline (the reported bug): a novel phrase with no alias is 'unknown'."""
    events = client.post("/api/ingest", json={"transcript": "fire it up"}).json()["events"]
    assert any("didn't understand" in m for m in _clarify_msgs(events))


def test_registered_alias_expands_and_routes(client):
    name = main.state.protocol_catalog()[0]["name"]
    resp = client.post(
        "/api/aliases",
        json={"aliases": [{"trigger": "fire it up", "phrase": f"load {name} protocol"}]},
    )
    assert resp.json() == {"ok": True, "count": 1}

    events = client.post("/api/ingest", json={"transcript": "Fire it up!"}).json()["events"]
    # routed to a real load (step_change), NOT the unknown clarify
    assert "step_change" in _kinds(events)
    assert not any("didn't understand" in m for m in _clarify_msgs(events))
    # the user's ORIGINAL words are echoed to the transcript, not the expansion
    echoes = [e["payload"]["text"] for e in events if e["type"] == "transcript_update"]
    assert "Fire it up!" in echoes


def test_aliases_endpoint_roundtrip(client):
    client.post("/api/aliases", json={"aliases": [{"trigger": "Go", "phrase": "next step"}]})
    assert client.get("/api/aliases").json()["aliases"] == [
        {"trigger": "go", "phrase": "next step"}
    ]
