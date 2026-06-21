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


def test_protocols_page_has_import_modal():
    html = (FT / "protocols.html").read_text(encoding="utf-8")
    for token in ("import-protocol", "import-modal", "import-text", "import-submit"):
        assert token in html, f"protocols.html missing {token}"


def test_app_wires_protocol_import():
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "/api/protocols/import" in js
    assert "handleProtocolImport" in js
    assert "protocol_imported" in js
    assert "/api/protocols/${encodeURIComponent(id)}/load" in js


def test_app_renders_reproducibility_flag():
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "renderLogFlag" in js
    assert 'flag.status === "mismatch"' in js
    assert 'flag.status === "ok"' in js


def test_notebook_css_has_flag_classes():
    css = (FT / "notebook.css").read_text(encoding="utf-8")
    assert ".log-flag" in css
    assert ".log-ok" in css
    assert ".flagged" in css


def test_every_page_has_demo_reset_control():
    for page in ("dashboard", "protocols", "guide", "notebook", "inventory"):
        html = (FT / f"{page}.html").read_text(encoding="utf-8")
        assert 'id="demo-reset"' in html or 'data-action="demo-reset"' in html, page


def test_app_wires_demo_reset():
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "/api/demo/reset" in js
    assert "handleDemoReset" in js
    assert 'case "reset"' in js
    assert "clearTransientState" in js
    assert "notes_cleared" in js


def test_every_page_has_shared_sidebar_nav():
    nav_links = [
        'data-nav="dashboard.html"',
        'data-nav="protocols.html"',
        'data-nav="notebook.html"',
        'data-nav="guide.html"',
        'data-nav="inventory.html"',
    ]
    for page in PAGES:
        html = (FT / page).read_text(encoding="utf-8")
        assert 'id="side-nav"' in html, f"{page} missing shared sidebar"
        assert 'id="main-nav"' in html, f"{page} missing shared nav"
        for link in nav_links:
            assert link in html, f"{page} missing nav link {link}"


def test_every_page_has_voice_controls():
    for page in PAGES:
        html = (FT / page).read_text(encoding="utf-8")
        assert 'id="voice-toggle"' in html, f"{page} missing voice toggle"
        assert 'id="voice-mute"' in html, f"{page} missing voice mute"
        assert 'id="live-transcript"' in html, f"{page} missing transcript area"


def test_app_wires_voice_pipeline():
    js = (FT / "app.js").read_text(encoding="utf-8")
    for token in (
        "/ws/audio",
        "getUserMedia",
        "MediaRecorder",
        "startMic",
        "stopMic",
        "onTranscript",
        "is_final",
        "set_muted",
        "wireNav",
    ):
        assert token in js, f"app.js missing {token}"


def test_pages_expose_live_hooks():
    cards = (FT / "protocols.html").read_text(encoding="utf-8")
    assert 'id="protocol-cards"' in cards
    assert 'id="inventory-rows"' in (FT / "inventory.html").read_text(encoding="utf-8")
    assert 'id="log-rows"' in (FT / "notebook.html").read_text(encoding="utf-8")
    guide = (FT / "guide.html").read_text(encoding="utf-8")
    assert 'id="step-tracker"' in guide
    assert 'id="live-transcript"' in guide


# --- cleanup: de-duped shell, real branding, dead controls removed ----------

def test_pages_use_shared_head_assets():
    for page in PAGES:
        html = (FT / page).read_text(encoding="utf-8")
        assert "tailwind-config.js" in html, f"{page} not using shared tailwind-config.js"
        assert "shared.css" in html, f"{page} not linking shared.css"
        assert 'id="tailwind-config"' not in html, f"{page} still inlines the tailwind config"


def test_pages_have_real_branding():
    for page in PAGES:
        html = (FT / page).read_text(encoding="utf-8")
        assert "Placeholder" not in html, f"{page} still shows Placeholder branding"


def test_dead_controls_removed():
    for page in PAGES:
        html = (FT / page).read_text(encoding="utf-8")
        assert ">Support<" not in html, f"{page} still has the dead Support control"
        assert ">Logout<" not in html, f"{page} still has the dead Logout control"
    assert "Export PDF" not in (FT / "notebook.html").read_text(encoding="utf-8")


def test_manual_entry_and_add_item_are_wired():
    notebook = (FT / "notebook.html").read_text(encoding="utf-8")
    assert 'id="log-add"' in notebook       # Manual Entry opens the log modal
    assert 'id="log-form"' in notebook
    inventory = (FT / "inventory.html").read_text(encoding="utf-8")
    assert 'id="add-item"' in inventory      # Add Item opens the inventory modal
    assert 'id="additem-modal"' in inventory
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "wireLogModal" in js
    assert "wireAddItemModal" in js


def test_inventory_add_item_collects_structured_amount():
    inventory = (FT / "inventory.html").read_text(encoding="utf-8")
    assert 'id="additem-amount"' in inventory
    assert 'id="additem-unit"' in inventory
    assert 'for="additem-amount">Amount' in inventory
    assert 'for="additem-unit">Unit' in inventory


def test_inventory_renderer_uses_structured_amount():
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "INVENTORY_UNITS" in js
    assert "formatInventoryAmount" in js
    assert "populateInventoryUnits" in js
    assert "amount, unit" in js
    assert ">Amount</div>" in js


def test_guide_confirm_and_live_step_counters_wired():
    guide = (FT / "guide.html").read_text(encoding="utf-8")
    # Guide "Confirm Action" + breadcrumb + counters target real ids the renderer fills.
    for marker in ('id="confirm-step"', 'id="protocol-name"', 'id="step-counter"', 'id="step-phase"'):
        assert marker in guide, f"guide.html missing {marker}"
    js = (FT / "app.js").read_text(encoding="utf-8")
    # Clicking "Confirm Action" confirms via the same /api/ingest spine as voice.
    assert "wireGuideConfirm" in js
    assert "ingestCommand" in js
    assert "/api/ingest" in js


def test_dead_decoration_timers_removed():
    # The waveform / live-sync micro-interaction timers targeted elements that no
    # longer exist; both the JS loops and the orphaned CSS are gone.
    for css in ("dashboard.css", "protocols.css", "guide.css"):
        assert ".waveform-bar" not in (FT / css).read_text(encoding="utf-8"), css
    for page in ("dashboard.html", "protocols.html", "notebook.html"):
        assert "waveform-bar" not in (FT / page).read_text(encoding="utf-8"), page
    # dead notebook "live-sync" selector is gone too
    assert ".text-secondary.animate-pulse" not in (FT / "notebook.html").read_text(encoding="utf-8")


def test_hydration_errors_are_surfaced_not_swallowed():
    # P2a: hydrate() must report section failures (log + page banner) instead of
    # swallowing them, so a broken API contract isn't disguised as empty UI.
    js = (FT / "app.js").read_text(encoding="utf-8")
    for token in ("hydrateSection", "showHydrateError", "console.warn", '"hydrate-error"'):
        assert token in js, f"app.js missing {token}"
