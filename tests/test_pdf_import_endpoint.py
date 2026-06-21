"""POST /api/protocols/import/file — PDF upload -> prose -> imported protocol."""

import os

os.environ["LAB_DB_PATH"] = ":memory:"  # set before importing the app module

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.state import SessionState

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Deterministic + offline parsing so the test needs no API key.
    monkeypatch.setenv("IMPORT_MODE", "deterministic")
    main.state = SessionState(db_path=":memory:", data_dir=tmp_path)
    main.state.load_files()
    with TestClient(main.app) as c:
        yield c


def _pdf(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_import_pdf_happy_path(client):
    r = client.post(
        "/api/protocols/import/file",
        files={"file": ("sample_protocol.pdf", _pdf("sample_protocol.pdf"), "application/pdf")},
        data={"name": "From PDF"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["protocol"]["name"] == "From PDF"
    proto = main.state.find_protocol("From PDF")
    assert proto is not None
    assert len(proto.steps) == 2
    assert proto.steps[1].duration_s == 600  # "Incubate 10 minutes" -> timer


def test_import_scanned_pdf_returns_friendly_error(client):
    r = client.post(
        "/api/protocols/import/file",
        files={"file": ("scan.pdf", _pdf("blank_scan.pdf"), "application/pdf")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "scan" in body["error"].lower() or "text" in body["error"].lower()
    # Nothing was registered from an unreadable PDF.
    assert main.state.protocols == {}


def test_import_non_pdf_file_422(client):
    r = client.post(
        "/api/protocols/import/file",
        files={"file": ("notes.txt", b"1. Add buffer\n2. Wait 2 minutes", "text/plain")},
    )
    assert r.status_code == 422
    assert main.state.protocols == {}


def test_import_pdf_reflows_wrapped_text_into_clean_steps(client, monkeypatch):
    # Simulate pypdf's layout-wrapped output: a numbered step split mid-sentence.
    wrapped = (
        "1. Add 200 uL of lysis buffer to the sample tube and mix\n"
        "thoroughly by pipetting.\n"
        "2. Incubate at 30 degrees C for 10 minutes."
    )
    monkeypatch.setattr(main, "extract_pdf_text", lambda raw: wrapped)
    r = client.post(
        "/api/protocols/import/file",
        files={"file": ("wrapped.pdf", b"%PDF-1.4 fake", "application/pdf")},
        data={"name": "Wrapped"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    proto = main.state.find_protocol("Wrapped")
    # Reflow rejoined the wrapped line, so two clean steps (not four fragments).
    assert len(proto.steps) == 2
    assert proto.steps[1].duration_s == 600
    assert proto.reagents == ["lysis buffer"]
