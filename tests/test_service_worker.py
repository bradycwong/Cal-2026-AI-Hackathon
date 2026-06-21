"""Service worker removed. /sw.js now serves a self-unregistering "kill" stub
that clears stale caches from earlier visits; the live UI (FrontendTest/) is
served fresh from the network and no longer registers a worker."""

import os

os.environ["LAB_DB_PATH"] = ":memory:"  # set before importing the app module

from pathlib import Path

from fastapi.testclient import TestClient

import backend.main as main

ROOT = Path(__file__).resolve().parents[1]


def test_sw_served_from_root_as_javascript():
    # Still served from "/" so its scope is the whole origin and the stub can
    # unregister a worker an earlier visit registered at root scope.
    with TestClient(main.app) as client:
        r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_app_does_not_register_service_worker():
    # No SW caching: the live UI is served fresh from the network.
    js = (ROOT / "frontend" / "app.js").read_text()
    assert "navigator.serviceWorker.register" not in js


def test_sw_is_self_unregistering_kill_stub():
    sw = (ROOT / "frontend" / "sw.js").read_text()
    assert "self.registration.unregister()" in sw
    assert "caches.delete" in sw
    # The stub must NOT precache the old (now-nonexistent) legacy shell paths.
    assert "/static/styles.css" not in sw
    assert "/static/app.js" not in sw
