"""Demo reset: stage-state primitives + the /api/demo/reset endpoint."""

import os
import shutil
from pathlib import Path

os.environ["LAB_DB_PATH"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.handlers import handle_command
from backend.schema import Command
from backend.state import SessionState

SHIPPED_DATA = Path(__file__).resolve().parents[1] / "backend" / "data"


def _baseline_dir(dst: Path) -> Path:
    """Stage the shipped protocols + inventory into ``dst`` (a scratch data or
    seed dir) so tests never touch the repo's real files."""
    (dst / "protocols").mkdir(parents=True)
    for yaml_file in (SHIPPED_DATA / "protocols").glob("*.yaml"):
        shutil.copy(yaml_file, dst / "protocols" / yaml_file.name)
    shutil.copy(SHIPPED_DATA / "inventory.csv", dst / "inventory.csv")
    return dst


@pytest.fixture
def client(tmp_path):
    main.state = SessionState(
        db_path=":memory:",
        data_dir=_baseline_dir(tmp_path / "data"),
        seed_dir=_baseline_dir(tmp_path / "seed"),
    )
    main.state.load_files()
    with TestClient(main.app) as c:
        yield c


def test_session_reset_clears_stage_state_but_not_log():
    state = SessionState()
    state.load_files()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    handle_command(Command(intent="next_step"), state)  # auto-logs "Completed step 1"
    handle_command(Command(intent="log_entry", log_text="added 200 uL", sample_id="A"), state)

    assert state.active_protocol is not None
    assert state.current_step_index >= 0
    assert len(state.log) == 2  # next_step note + manual log entry

    state.reset()

    assert state.active_protocol is None
    assert state.current_step_index == -1
    assert state.timers == []
    assert state._timer_seq == 0
    assert len(state.log) == 2  # reset clears stage state, never the log
    assert state.protocols
    assert state.inventory


def test_clear_log_wipes_memory_and_sqlite(tmp_path):
    db_path = str(tmp_path / "lab.db")
    state = SessionState(db_path=db_path)
    state.load_files()
    state.append_log(
        "added 250 uL", sample_id="A", category="DNA Extraction", flag={"status": "mismatch"}
    )
    assert state.log

    state.clear_log()

    assert state.log == []
    assert state._log_seq == 0

    state2 = SessionState(db_path=db_path)
    state2.load_files()
    assert state2.log == []


def test_reset_endpoint_clears_notes_regardless_of_demo_mode(client, monkeypatch):
    # Full factory reset always wipes notes now — the LAB_DEMO_MODE gate is gone.
    monkeypatch.delenv("LAB_DEMO_MODE", raising=False)
    client.post("/api/log", json={"text": "clear me", "category": "Note"})
    response = client.post("/api/demo/reset")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["notes_cleared"] is True
    assert client.get("/api/log").json()["log"] == []


def test_reset_restores_inventory_to_baseline(client):
    baseline = client.get("/api/inventory").json()["items"]
    client.post("/api/inventory", json={"name": "Mystery Reagent", "location": "Freezer Z"})
    assert len(client.get("/api/inventory").json()["items"]) == len(baseline) + 1

    client.post("/api/demo/reset")

    restored = client.get("/api/inventory").json()["items"]
    assert len(restored) == len(baseline)
    assert "Mystery Reagent" not in [item["name"] for item in restored]


def test_reset_restores_protocol_library(client, monkeypatch):
    # Deterministic import (no API key needed) so the test never hits the LLM.
    monkeypatch.setenv("IMPORT_MODE", "deterministic")
    baseline_ids = {p["id"] for p in client.get("/api/protocols").json()["protocols"]}
    assert baseline_ids

    # Drift the library both ways: delete a shipped protocol AND import a new one.
    removed = sorted(baseline_ids)[0]
    assert client.delete(f"/api/protocols/{removed}").status_code == 200
    client.post(
        "/api/protocols/import",
        json={"text": "Add 200 uL lysis buffer\nVortex for 30 seconds", "name": "Scratch Import"},
    )
    drifted = {p["id"] for p in client.get("/api/protocols").json()["protocols"]}
    assert drifted != baseline_ids

    client.post("/api/demo/reset")

    restored = {p["id"] for p in client.get("/api/protocols").json()["protocols"]}
    assert restored == baseline_ids


def test_reset_deletes_created_notebooks(client):
    baseline = client.get("/api/notebooks").json()["notebooks"]
    assert len(baseline) == 1  # ships with the single default "Lab Notebook"
    client.post("/api/notebooks", json={"name": "Run 2"})
    assert len(client.get("/api/notebooks").json()["notebooks"]) == 2

    client.post("/api/demo/reset")

    after = client.get("/api/notebooks").json()
    assert len(after["notebooks"]) == 1
    assert after["notebooks"][0]["name"] == "Lab Notebook"
    assert after["notebooks"][0]["id"] == after["active_id"]
