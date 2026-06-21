import os
import shutil
from pathlib import Path

os.environ["LAB_DB_PATH"] = ":memory:"

from fastapi.testclient import TestClient

import backend.main as main
from backend.state import SessionState

SHIPPED_DATA = Path(__file__).resolve().parents[1] / "backend" / "data"


def _scratch_data_dir(tmp_path: Path) -> Path:
    dst = tmp_path / "data"
    (dst / "protocols").mkdir(parents=True)
    for yaml_file in (SHIPPED_DATA / "protocols").glob("*.yaml"):
        shutil.copy(yaml_file, dst / "protocols" / yaml_file.name)
    shutil.copy(SHIPPED_DATA / "inventory.csv", dst / "inventory.csv")
    return dst


def make_client(tmp_path):
    main.state = SessionState(db_path=":memory:", data_dir=_scratch_data_dir(tmp_path))
    main.state.load_files()
    return TestClient(main.app)


def test_scale_endpoint_uses_explicit_protocol_id(tmp_path):
    with make_client(tmp_path) as client:
        r = client.post(
            "/api/scale",
            json={"protocol_id": "dna_extraction", "sample_count": 12, "overage_percent": 10},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["protocol_id"] == "dna_extraction"
    assert body["protocol_name"] == "DNA Extraction"
    assert body["sample_count"] == 12
    assert body["overage_percent"] == 10

    rows = {row["reagent"]: row for row in body["reagents"]}
    assert rows["lysis buffer"]["total_ul"] == 2640
    assert rows["lysis buffer"]["verdict"] == "missing"
    assert rows["ethanol"]["total_display"] == "2.64 mL"
    assert rows["ethanol"]["verdict"] == "in_stock"
    assert rows["nuclease-free water"]["total_ul"] == 660
    assert rows["nuclease-free water"]["verdict"] == "in_stock"

    # Matched reagents carry their inventory location (so the prep table can show
    # where to grab them); an unmatched reagent has no location.
    assert rows["ethanol"]["location"] == "Cabinet 3"
    assert rows["nuclease-free water"]["location"] == "Fridge 1 shelf C"
    assert rows["lysis buffer"]["location"] is None


def test_scale_endpoint_defaults_to_active_protocol(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/protocols/dna_extraction/load")
        r = client.post("/api/scale", json={"sample_count": 2, "overage_percent": 0})

    assert r.status_code == 200
    body = r.json()
    assert body["protocol_id"] == "dna_extraction"
    assert body["sample_count"] == 2


def test_scale_endpoint_rejects_bad_inputs(tmp_path):
    with make_client(tmp_path) as client:
        r1 = client.post("/api/scale", json={"protocol_id": "dna_extraction", "sample_count": 0})
        r2 = client.post(
            "/api/scale",
            json={"protocol_id": "dna_extraction", "sample_count": 1, "overage_percent": -1},
        )

    assert r1.status_code == 422
    assert "sample count" in r1.json()["detail"].lower()
    assert r2.status_code == 422
    assert "overage" in r2.json()["detail"].lower()


def test_scale_endpoint_404_without_protocol(tmp_path):
    with make_client(tmp_path) as client:
        r = client.post("/api/scale", json={"sample_count": 1})

    assert r.status_code == 404
    assert "load a protocol" in r.json()["detail"].lower()


def test_scale_endpoint_404_unknown_protocol_id(tmp_path):
    with make_client(tmp_path) as client:
        r = client.post(
            "/api/scale",
            json={"protocol_id": "does_not_exist", "sample_count": 1},
        )

    assert r.status_code == 404
    assert "unknown protocol" in r.json()["detail"].lower()
