"""API checks — FrontendTest serving + read snapshots + deterministic load."""

import os
import shutil
from pathlib import Path

os.environ["LAB_DB_PATH"] = ":memory:"  # set before importing the app module

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.state import SessionState

SHIPPED_DATA = Path(__file__).resolve().parents[1] / "backend" / "data"


def _scratch_data_dir(tmp_path: Path) -> Path:
    """Copy the shipped protocols + inventory into a throwaway dir so tests get
    realistic data without mutating the repo's files (imports write here)."""
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


def test_frontendtest_is_served(client):
    assert client.get("/").status_code == 200
    # Root serves the dashboard; assert a stable structural marker rather than a
    # branding/title string (those are polish-churned).
    assert 'id="side-nav"' in client.get("/").text
    assert client.get("/protocols.html").status_code == 200
    assert client.get("/api/health").json()["ok"] is True


def test_catalog_inventory_and_log_shapes(client):
    protocols = client.get("/api/protocols").json()
    assert "protocols" in protocols
    assert protocols["protocols"]
    p0 = protocols["protocols"][0]
    assert p0["status"] in {"READY", "LOW_REAGENTS", "ARCHIVED"}
    assert {"id", "name", "duration_label", "step_count", "reagents"} <= set(p0)

    items = client.get("/api/inventory").json()
    assert "items" in items
    assert items["items"]
    assert {"name", "code", "category", "status"} <= set(items["items"][0])

    assert "log" in client.get("/api/log").json()


def test_load_protocol_by_id_advances_tracker(client):
    catalog = client.get("/api/protocols").json()["protocols"]
    pid = next(p["id"] for p in catalog if p["id"] == "dna_extraction")
    r = client.post(f"/api/protocols/{pid}/load")
    assert r.status_code == 200
    payload = r.json()["events"][0]["payload"]
    assert payload["kind"] == "step_change"
    assert payload["all_steps"]
    assert payload["current_index"] == 0
    assert payload["protocol_name"] == "DNA Extraction"


def test_step_next_endpoint_logs_by_default(client):
    # Confirm Action (log=True, the default) advances and writes a note.
    client.post("/api/protocols/dna_extraction/load")
    r = client.post("/api/step/next")
    assert r.status_code == 200
    kinds = [e["payload"].get("kind") for e in r.json()["events"]]
    assert "step_change" in kinds
    assert "log_entry" in kinds
    log = client.get("/api/log").json()["log"]
    assert log[-1]["text"].startswith("Completed step 1")


def test_step_next_endpoint_skip_does_not_log(client):
    # Skip (log=False) advances without writing a note.
    client.post("/api/protocols/dna_extraction/load")
    r = client.post("/api/step/next", json={"log": False})
    assert r.status_code == 200
    kinds = [e["payload"].get("kind") for e in r.json()["events"]]
    assert "step_change" in kinds
    assert "log_entry" not in kinds
    assert client.get("/api/log").json()["log"] == []


def test_post_log_persists_and_lists(client):
    r = client.post("/api/log", json={"text": "added 200 uL", "category": "Note"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    log = client.get("/api/log").json()["log"]
    assert log[-1]["text"] == "added 200 uL"
    assert log[-1]["category"] == "Note"


def test_import_protocol_endpoint(client, monkeypatch):
    monkeypatch.setenv("IMPORT_MODE", "deterministic")
    r = client.post(
        "/api/protocols/import",
        json={"name": "Quick DNA Prep", "text": "1. Add buffer\n2. Incubate 10 minutes"},
    )
    data = r.json()
    assert r.status_code == 200
    assert data["ok"] is True
    assert data["protocol"]["id"] == "quick_dna_prep"
    assert data["protocol"]["status"] == "READY"
    assert "load_hint" in data
    catalog = client.get("/api/protocols").json()["protocols"]
    assert any(p["id"] == "quick_dna_prep" for p in catalog)
    # Imported protocol loads immediately via the deterministic endpoint.
    assert client.post("/api/protocols/quick_dna_prep/load").status_code == 200


def test_import_empty_input_is_controlled_error(client, monkeypatch):
    monkeypatch.setenv("IMPORT_MODE", "deterministic")
    r = client.post("/api/protocols/import", json={"text": "   "})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "error" in body


def test_state_step_has_tracker_fields(client):
    client.post("/api/protocols/dna_extraction/load")
    step = client.get("/api/state").json()["step"]
    assert step["all_steps"]
    assert step["current_index"] == 0
    assert step["protocol_name"] == "DNA Extraction"
