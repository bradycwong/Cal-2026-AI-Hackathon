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
    with TestClient(main.app) as c:
        yield c


def test_session_reset_clears_stage_state_but_not_log():
    state = SessionState()
    state.load_files()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    handle_command(Command(intent="next_step"), state)
    handle_command(Command(intent="log_entry", log_text="added 200 uL", sample_id="A"), state)

    assert state.active_protocol is not None
    assert state.current_step_index >= 0
    assert state.log

    state.reset()

    assert state.active_protocol is None
    assert state.current_step_index == -1
    assert state.timers == []
    assert state._timer_seq == 0
    assert len(state.log) == 1
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


def test_reset_endpoint_keeps_notes_when_demo_mode_false(client, monkeypatch):
    monkeypatch.delenv("LAB_DEMO_MODE", raising=False)
    client.post("/api/log", json={"text": "keep me", "category": "Note"})
    response = client.post("/api/demo/reset")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["notes_cleared"] is False
    assert client.get("/api/log").json()["log"]


def test_reset_endpoint_clears_notes_when_demo_mode_true(client, monkeypatch):
    monkeypatch.setenv("LAB_DEMO_MODE", "true")
    client.post("/api/log", json={"text": "clear me", "category": "Note"})
    response = client.post("/api/demo/reset")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["notes_cleared"] is True
    assert client.get("/api/log").json()["log"] == []
