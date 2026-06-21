"""Static checks for the served FrontendTest client.

After the migration, FrontendTest/ is the live UI; the legacy frontend/ app is
kept on disk but no longer targeted by tests.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FT = ROOT / "FrontendTest"

PAGES = ["dashboard.html", "protocols.html", "guide.html", "notebook.html", "inventory.html"]


def test_every_page_loads_shared_client():
    for page in PAGES:
        html = (FT / page).read_text(encoding="utf-8")
        assert '<script src="app.js" defer></script>' in html, f"{page} missing app.js"


def test_app_exposes_lab_client_api():
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "window.LabClient" in js
    for fn in (
        "fetchProtocols",
        "fetchInventory",
        "fetchLog",
        "renderProtocolCards",
        "renderInventory",
        "renderLog",
        "renderStep",
        "renderTimers",
        "clearTransientState",
    ):
        assert fn in js, f"app.js missing {fn}"


def test_app_uses_shared_contract_endpoints():
    js = (FT / "app.js").read_text(encoding="utf-8")
    for path in ("/api/protocols", "/api/inventory", "/api/log", "/api/state", "/ws/events"):
        assert path in js, f"app.js missing {path}"


def test_app_dispatches_command_kinds():
    js = (FT / "app.js").read_text(encoding="utf-8")
    for kind in ('"step_change"', '"log_entry"', '"log_removed"', '"log_update"',
                 '"clarify"', '"voice_state"'):
        assert kind in js, f"app.js missing dispatch for {kind}"


def test_pages_expose_live_hooks():
    cards = (FT / "protocols.html").read_text(encoding="utf-8")
    assert 'id="protocol-cards"' in cards
    assert 'id="inventory-rows"' in (FT / "inventory.html").read_text(encoding="utf-8")
    assert 'id="log-rows"' in (FT / "notebook.html").read_text(encoding="utf-8")
    guide = (FT / "guide.html").read_text(encoding="utf-8")
    assert 'id="step-tracker"' in guide
    assert 'id="live-transcript"' in guide
