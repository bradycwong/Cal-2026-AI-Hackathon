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


def test_prep_state_endpoint_tracks_gate_and_count(client):
    # The front end mirrors the prep modal's open/close + the determined sample
    # count to the backend so "start protocol"/"done" can gate on a real count.
    r = client.post("/api/prep/state", json={"open": True})
    assert r.status_code == 200
    assert r.json()["prep_open"] is True
    assert main.state.prep_open is True

    r = client.post("/api/prep/state", json={"sample_count": 24})
    assert r.status_code == 200
    assert main.state.prep_sample_count == 24

    client.post("/api/prep/state", json={"open": False})
    assert main.state.prep_open is False


def test_ingest_set_sample_count_emits_prep_control(client):
    r = client.post("/api/ingest", json={"transcript": "set samples to 30"})
    events = r.json()["events"]
    prep = next(e["payload"] for e in events if e["payload"].get("kind") == "prep_control")
    assert prep["action"] == "set_samples"
    assert prep["sample_count"] == 30
    assert main.state.prep_sample_count == 30


def _prep_actions(resp):
    return [
        e["payload"].get("action")
        for e in resp.json()["events"]
        if e["payload"].get("kind") == "prep_control"
    ]


def test_prep_gate_starts_with_default_one(client, monkeypatch):
    # The popup gates the run, but "start protocol" with no count set just
    # defaults to 1 sample instead of asking "how many samples?".
    monkeypatch.setattr(router, "ROUTER_MODE", "deterministic")
    client.post("/api/protocols/dna_extraction/load")
    client.post("/api/prep/state", json={"open": True})  # front end raises the gate

    r = client.post("/api/ingest", json={"transcript": "start protocol"})
    assert "close" in _prep_actions(r)
    assert main.state.prep_open is False
    assert main.state.prep_sample_count == 1


def test_prep_gate_uses_set_count_when_provided(client, monkeypatch):
    monkeypatch.setattr(router, "ROUTER_MODE", "deterministic")
    client.post("/api/protocols/dna_extraction/load")
    client.post("/api/prep/state", json={"open": True})
    client.post("/api/ingest", json={"transcript": "set samples to 24"})
    assert main.state.prep_sample_count == 24
    r = client.post("/api/ingest", json={"transcript": "start protocol"})
    assert "close" in _prep_actions(r)
    assert main.state.prep_open is False
    assert main.state.prep_sample_count == 24


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


def test_recent_marks_active_protocol(client):
    # The currently-loaded protocol is flagged active (newest -> first); the rest
    # are not. Mirrors the active-notebook flag the dashboard already highlights.
    client.post("/api/protocols/dna_extraction/load")
    client.post("/api/protocols/pcr_setup/load")  # pcr_setup is now active
    recent = _recent(client)
    assert recent[0]["id"] == "pcr_setup"
    assert recent[0].get("active") is True
    assert [p["id"] for p in recent if p.get("active")] == ["pcr_setup"]


def test_recent_cold_start_has_no_active_protocol(client):
    # Nothing loaded -> no active protocol -> every card is flagged active=False.
    assert all(p.get("active") is False for p in _recent(client))


def test_demo_reset_clears_recency(client):
    client.post("/api/protocols/dna_extraction/load")
    assert _recent(client)[0]["last_used_at"] is not None
    assert client.post("/api/demo/reset").status_code == 200
    # Back to cold-start fallback: timestamps cleared.
    assert all(p["last_used_at"] is None for p in _recent(client))


# --- editing protocols -----------------------------------------------------
def _full(client, pid="dna_extraction"):
    r = client.get(f"/api/protocols/{pid}")
    assert r.status_code == 200
    return r.json()["protocol"]


def _as_payload_steps(steps):
    return [
        {
            "text": s["text"],
            "duration_s": s["duration_s"],
            "timer_label": s["timer_label"],
            "parameters": s["parameters"],
        }
        for s in steps
    ]


def test_get_full_protocol_includes_steps(client):
    proto = _full(client)
    assert proto["id"] == "dna_extraction"
    assert proto["steps"], "full protocol must carry the steps, not just a count"
    s0 = proto["steps"][0]
    assert {"id", "text", "duration_s", "timer_label", "parameters"} <= set(s0)
    # params + timer_label survive to the editor
    assert any(s["parameters"].get("reagent") for s in proto["steps"])
    assert any(s["timer_label"] for s in proto["steps"] if s["duration_s"])


def test_get_unknown_protocol_404(client):
    assert client.get("/api/protocols/nope").status_code == 404


def test_patch_protocol_persists_name_desc_and_steps(client):
    steps = [
        {"text": "Add 200 uL lysis buffer", "duration_s": None,
         "timer_label": None, "parameters": {"reagent": "lysis buffer"}},
        {"text": "Incubate 5 minutes", "duration_s": 300,
         "timer_label": "incubation", "parameters": {}},
    ]
    r = client.patch(
        "/api/protocols/dna_extraction",
        json={"name": "DNA Prep v2", "description": "new desc", "steps": steps},
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    cat = {p["id"]: p for p in client.get("/api/protocols").json()["protocols"]}
    assert cat["dna_extraction"]["name"] == "DNA Prep v2"
    assert cat["dna_extraction"]["step_count"] == 2
    after = _full(client)
    assert after["description"] == "new desc"
    assert [s["text"] for s in after["steps"]] == ["Add 200 uL lysis buffer", "Incubate 5 minutes"]
    assert [s["id"] for s in after["steps"]] == [1, 2]  # re-IDed 1..N
    # round-trips through the YAML loader unchanged (guards the unicode escape path)
    assert after["steps"][1]["duration_s"] == 300


def test_patch_preserves_parameters_through_reorder(client):
    orig = _full(client)["steps"]
    want = {s["text"]: s["parameters"] for s in orig}
    payload = list(reversed(_as_payload_steps(orig)))
    r = client.patch(
        "/api/protocols/dna_extraction",
        json={"name": "DNA Extraction", "description": "d", "steps": payload},
    )
    assert r.status_code == 200
    after = _full(client)["steps"]
    assert [s["text"] for s in after] == [s["text"] for s in payload]
    for s in after:  # params traveled with the row, not the id
        assert s["parameters"] == want[s["text"]]


def test_patch_preserves_parameters_through_delete(client):
    orig = _full(client)["steps"]
    keep = orig[1:]  # drop the first step
    r = client.patch(
        "/api/protocols/dna_extraction",
        json={"name": "DNA Extraction", "description": "d", "steps": _as_payload_steps(keep)},
    )
    assert r.status_code == 200
    after = _full(client)["steps"]
    assert [s["id"] for s in after] == list(range(1, len(keep) + 1))  # re-IDed cleanly
    for s, k in zip(after, keep):
        assert s["parameters"] == k["parameters"]


def test_patch_drops_timer_label_when_duration_cleared(client):
    orig = _full(client)["steps"]
    cleared = [
        {"text": s["text"], "duration_s": None,
         "timer_label": s["timer_label"], "parameters": s["parameters"]}
        for s in orig
    ]
    client.patch(
        "/api/protocols/dna_extraction",
        json={"name": "DNA Extraction", "description": "d", "steps": cleared},
    )
    after = _full(client)["steps"]
    assert all(s["duration_s"] is None for s in after)
    assert all(s["timer_label"] is None for s in after)  # invariant enforced by the writer


def test_patch_invalid_does_not_corrupt_file(client):
    before = _full(client)["steps"]
    r = client.patch(
        "/api/protocols/dna_extraction",
        json={"name": "DNA Extraction", "description": "d", "steps": []},  # no steps -> invalid
    )
    assert r.status_code != 200 or r.json().get("ok") is False
    after = _full(client)["steps"]  # original protocol untouched
    assert [s["text"] for s in after] == [s["text"] for s in before]


def test_patch_unknown_protocol_404(client):
    r = client.patch(
        "/api/protocols/nope",
        json={"name": "X", "description": "", "steps": [
            {"text": "x", "duration_s": None, "timer_label": None, "parameters": {}}]},
    )
    assert r.status_code == 404


def test_patch_active_protocol_refreshes_guide(client):
    client.post("/api/protocols/dna_extraction/load")
    client.post("/api/step/next")
    client.post("/api/step/next")  # cursor at index 2
    orig = _full(client)["steps"]
    one_step = _as_payload_steps(orig[:1])  # shrink to a single step
    r = client.patch(
        "/api/protocols/dna_extraction",
        json={"name": "DNA Extraction", "description": "d", "steps": one_step},
    )
    assert r.status_code == 200
    sc = next(e["payload"] for e in r.json()["events"] if e["payload"].get("kind") == "step_change")
    assert sc["current_index"] == 0  # clamped to the new last index
    assert len(sc["all_steps"]) == 1
    step = client.get("/api/state").json()["step"]
    assert step["current_index"] == 0


def test_patch_rederives_duration_and_reagents(client):
    steps = [{"text": "Add 100 uL elution buffer", "duration_s": None,
              "timer_label": None, "parameters": {"reagent": "elution buffer"}}]
    client.patch(
        "/api/protocols/dna_extraction",
        json={"name": "DNA Extraction", "description": "d", "steps": steps},
    )
    p = {x["id"]: x for x in client.get("/api/protocols").json()["protocols"]}["dna_extraction"]
    assert p["duration_label"] == "-"  # no timed steps -> derived duration is none
    assert p["reagents"] == ["elution buffer"]
