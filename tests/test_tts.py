"""AI voice output (TTS) — offline checks for the no-key path.

No network: only the no-key behaviour is exercised. With DEEPGRAM_API_KEY unset,
synthesize() returns None, /api/tts answers 204 (browser falls back), and
/api/state advertises tts_available=False.
"""

import asyncio
import os
import shutil
from pathlib import Path

os.environ["LAB_DB_PATH"] = ":memory:"  # set before importing the app module

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend import deepgram_tts
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


def test_synthesize_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    assert asyncio.run(deepgram_tts.synthesize("hi")) is None


def test_tts_available_false_without_key(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    assert deepgram_tts.tts_available() is False


def test_api_tts_204_without_key(client, monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    resp = client.post("/api/tts", json={"text": "hi"})
    assert resp.status_code == 204
    assert not resp.content


def test_state_includes_tts_available_flag(client, monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    body = client.get("/api/state").json()
    assert "tts_available" in body
    assert isinstance(body["tts_available"], bool)
    assert body["tts_available"] is False
