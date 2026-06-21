"""Endpoint checks — POST /api/inventory (manual add) + /api/protocols (upload)."""

import os

os.environ["LAB_DB_PATH"] = ":memory:"  # set before importing the app module

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.state import SessionState

VALID_PROTOCOL = """\
protocol:
  id: bca_protein_assay
  name: BCA Protein Assay
  aliases: ["bca", "protein assay"]
  steps:
    - id: 1
      text: "Prepare BSA standards."
    - id: 2
      text: "Incubate at 37 C."
      duration_s: 1800
      timer_label: incubation
"""


@pytest.fixture
def client(tmp_path):
    # Isolate the process-wide state on a throwaway data dir per test.
    main.state = SessionState(db_path=":memory:", data_dir=tmp_path)
    main.state.load_files()
    with TestClient(main.app) as c:
        yield c


def test_add_inventory_item(client):
    r = client.post(
        "/api/inventory",
        json={"name": "Agarose", "location": "Cabinet 2", "quantity_approx": "~500 g"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["ok"] is True
    assert body["item"]["name"] == "Agarose"
    assert body["inventory_count"] == 1
    # Now findable through the normal voice/typed lookup path.
    assert any(i.name == "Agarose" for i in main.state.inventory)


def test_add_inventory_item_blank_name_422(client):
    r = client.post("/api/inventory", json={"name": "   "})
    assert r.status_code == 422


def test_upload_protocol(client):
    r = client.post(
        "/api/protocols",
        files={"file": ("bca.yaml", VALID_PROTOCOL, "application/x-yaml")},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["protocol"] == {"id": "bca_protein_assay", "name": "BCA Protein Assay", "steps": 2}
    assert "bca_protein_assay" in body["protocols"]
    # Immediately loadable by name/alias.
    assert main.state.find_protocol("protein assay") is not None


def test_upload_protocol_malformed_422(client):
    r = client.post(
        "/api/protocols",
        files={"file": ("bad.yaml", "not a protocol", "application/x-yaml")},
    )
    assert r.status_code == 422
    assert main.state.protocols == {}


def test_upload_protocol_non_utf8_422(client):
    r = client.post(
        "/api/protocols",
        files={"file": ("bad.yaml", b"\xff\xfe\x00bad", "application/x-yaml")},
    )
    assert r.status_code == 422


def test_stop_timer_endpoint_removes_one(client):
    timer = main.state.add_timer(600, "incubation")
    r = client.post(f"/api/timers/{timer.timer_id}/stop")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["events"][0]["payload"] == {"kind": "timer_removed", "timer_id": timer.timer_id}
    assert main.state.timers == []


def test_stop_timer_endpoint_unknown_id_404(client):
    r = client.post("/api/timers/does-not-exist/stop")
    assert r.status_code == 404
