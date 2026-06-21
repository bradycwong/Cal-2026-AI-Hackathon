"""API checks — FrontendTest serving + read snapshots + deterministic load."""

import os
import shutil
from pathlib import Path

os.environ["LAB_DB_PATH"] = ":memory:"  # set before importing the app module

import pytest
from fastapi.testclient import TestClient

import backend.main as main
import backend.router as router
from backend.schema import Command
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


def test_step_next_endpoint_skip_logs_skipped(client):
    # Skip (log=False) advances AND writes a "Skipped step N" note.
    client.post("/api/protocols/dna_extraction/load")
    r = client.post("/api/step/next", json={"log": False})
    assert r.status_code == 200
    events = r.json()["events"]
    kinds = [e["payload"].get("kind") for e in events]
    assert "step_change" in kinds
    assert "log_entry" in kinds
    log = client.get("/api/log").json()["log"]
    assert log[-1]["text"].startswith("Skipped step 1")
    step_change = next(e["payload"] for e in events if e["payload"]["kind"] == "step_change")
    assert step_change["skipped_indices"] == [0]


def test_ingest_timer_commands_bypass_llm(client, monkeypatch):
    # Force the LLM seam "live" but stub it to a wrong intent: if /api/ingest
    # depended on the LLM for timers, no timer event would ever appear.
    monkeypatch.setattr(router, "ROUTER_MODE", "llm")
    monkeypatch.setattr(router, "_llm_route", lambda t: Command(intent="unknown"))

    r = client.post("/api/ingest", json={"transcript": "start a 10 minute timer"})
    events = r.json()["events"]
    timer = next(e["payload"] for e in events if e["type"] == "timer_update")
    assert timer["paused"] is False  # explicit duration -> runs immediately

    r = client.post("/api/ingest", json={"transcript": "stop timer"})
    kinds = [e["payload"].get("kind") for e in r.json()["events"]]
    assert "timer_removed" in kinds


def test_ingest_start_timer_resumes_paused_step_timer(client, monkeypatch):
    monkeypatch.setattr(router, "ROUTER_MODE", "llm")
    monkeypatch.setattr(router, "_llm_route", lambda t: Command(intent="unknown"))
    client.post("/api/protocols/dna_extraction/load")
    client.post("/api/step/next")  # advance onto the timed step -> paused timer
    r = client.post("/api/ingest", json={"transcript": "start timer"})
    timers = [e["payload"] for e in r.json()["events"] if e["type"] == "timer_update"]
    assert timers and timers[-1]["paused"] is False  # resumed, now running


def test_post_log_persists_and_lists(client):
    r = client.post("/api/log", json={"text": "added 200 uL", "category": "Note"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    log = client.get("/api/log").json()["log"]
    assert log[-1]["text"] == "added 200 uL"
    assert log[-1]["category"] == "Note"


def test_post_log_is_tagged_manual(client):
    r = client.post("/api/log", json={"text": "cells looked healthy"})
    entry = r.json()["entry"]
    assert entry["entry_type"] == "manual"
    assert entry["edited"] is False
    assert client.get("/api/log").json()["log"][-1]["entry_type"] == "manual"


def test_step_note_is_tagged_automatic(client):
    client.post("/api/protocols/dna_extraction/load")
    client.post("/api/step/next")
    last = client.get("/api/log").json()["log"][-1]
    assert last["entry_type"] == "automatic"
    assert last["edited"] is False


def test_patch_log_edits_and_retags_manual_edited(client):
    # An automatic step note, edited by id, becomes manual + edited.
    client.post("/api/protocols/dna_extraction/load")
    client.post("/api/step/next")
    entry = client.get("/api/log").json()["log"][-1]
    assert entry["entry_type"] == "automatic"

    r = client.patch(f"/api/log/{entry['id']}", json={"text": "Completed step 1 (amended)"})
    assert r.status_code == 200
    updated = r.json()["entry"]
    assert updated["text"] == "Completed step 1 (amended)"
    assert updated["entry_type"] == "manual"
    assert updated["edited"] is True
    # GET reflects the edit
    last = client.get("/api/log").json()["log"][-1]
    assert last["text"] == "Completed step 1 (amended)"
    assert last["entry_type"] == "manual"
    assert last["edited"] is True


def test_patch_log_unknown_id_returns_404(client):
    r = client.patch("/api/log/9999", json={"text": "nope"})
    assert r.status_code == 404


def test_log_snapshot_carries_entry_type(client):
    client.post("/api/log", json={"text": "note one"})
    log = client.get("/api/log").json()["log"]
    assert {"entry_type", "edited"} <= set(log[-1])


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
    assert step["skipped_indices"] == []
    assert step["finished"] is False


def test_state_reports_finished_after_completing_protocol(client):
    client.post("/api/protocols/dna_extraction/load")
    # A fresh load is not finished.
    assert client.get("/api/state").json()["step"]["finished"] is False
    # Confirm through all five steps; the fifth completes the protocol.
    for _ in range(5):
        client.post("/api/step/next")
    step = client.get("/api/state").json()["step"]
    assert step["finished"] is True
    assert step["current_step"]["id"] == 5
    assert step["current_index"] == 4


def test_state_step_reflects_skipped_indices(client):
    # A refresh (/api/state) keeps a skipped step marked so the tracker stays yellow.
    client.post("/api/protocols/dna_extraction/load")
    client.post("/api/step/next", json={"log": False})  # skip step 1 (index 0)
    step = client.get("/api/state").json()["step"]
    assert step["skipped_indices"] == [0]
    assert step["current_index"] == 1


# --- recently-used protocols (dashboard) -----------------------------------
def _recent(client):
    r = client.get("/api/protocols/recent")
    assert r.status_code == 200
    return r.json()["recent"]


def test_recent_cold_start_falls_back_to_catalog(client):
    # Nothing loaded yet: surface up to 3 library protocols (catalog order) with
    # no timestamp, so the dashboard is never blank on a fresh demo.
    catalog = client.get("/api/protocols").json()["protocols"]
    recent = _recent(client)
    assert len(recent) == min(3, len(catalog))
    assert [p["id"] for p in recent] == [p["id"] for p in catalog[:3]]
    assert all(p["last_used_at"] is None for p in recent)


def test_recent_lists_loaded_newest_first_and_caps_at_three(client):
    order = ["bacterial_transformation", "dna_extraction", "pcr_setup", "plasmid_miniprep"]
    for pid in order:
        assert client.post(f"/api/protocols/{pid}/load").status_code == 200
    recent = _recent(client)
    # Capped at 3, newest first; the oldest load drops off the cap.
    assert [p["id"] for p in recent] == ["plasmid_miniprep", "pcr_setup", "dna_extraction"]
    assert all(
        isinstance(p["last_used_at"], str) and p["last_used_at"].endswith("Z")
        for p in recent
    )
    # Reduced cards still carry the catalog fields the renderer reads.
    assert {"id", "name", "description", "last_used_at"} <= set(recent[0])


def test_recent_reload_moves_protocol_to_front(client):
    client.post("/api/protocols/dna_extraction/load")
    client.post("/api/protocols/pcr_setup/load")
    client.post("/api/protocols/dna_extraction/load")  # re-load -> back to front, no dupe
    recent = _recent(client)
    assert [p["id"] for p in recent] == ["dna_extraction", "pcr_setup"]


def test_recent_excludes_deleted_protocol(client):
    client.post("/api/protocols/dna_extraction/load")
    client.post("/api/protocols/pcr_setup/load")
    assert client.delete("/api/protocols/pcr_setup").status_code == 200
    recent = _recent(client)
    assert [p["id"] for p in recent] == ["dna_extraction"]


def test_demo_reset_clears_recency(client):
    client.post("/api/protocols/dna_extraction/load")
    assert _recent(client)[0]["last_used_at"] is not None
    assert client.post("/api/demo/reset").status_code == 200
    # Back to cold-start fallback: timestamps cleared.
    assert all(p["last_used_at"] is None for p in _recent(client))
