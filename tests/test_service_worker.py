"""Service-worker shell cache: served at root scope, registered, precaches shell."""

import os

os.environ["LAB_DB_PATH"] = ":memory:"  # set before importing the app module

from pathlib import Path

from fastapi.testclient import TestClient

import backend.main as main

ROOT = Path(__file__).resolve().parents[1]


def test_sw_served_from_root_as_javascript():
    # Must be served from "/" so its default scope is the whole origin; a worker
    # under /static could only control /static.
    with TestClient(main.app) as client:
        r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "lab-shell" in r.text


def test_app_registers_service_worker():
    js = (ROOT / "frontend" / "app.js").read_text()
    assert 'navigator.serviceWorker.register("/sw.js")' in js


def test_sw_precaches_shell_and_skips_dynamic_traffic():
    sw = (ROOT / "frontend" / "sw.js").read_text()
    for asset in ('"/"', '"/static/styles.css"', '"/static/app.js"'):
        assert asset in sw, f"shell missing {asset}"
    # Live data + sockets must never be cached.
    assert '"/api/"' in sw
    assert '"/ws/"' in sw
