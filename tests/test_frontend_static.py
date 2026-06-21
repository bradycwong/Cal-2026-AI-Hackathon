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


def test_protocols_page_has_edit_modal():
    html = (FT / "protocols.html").read_text(encoding="utf-8")
    for token in (
        "edit-protocol-modal",
        "edit-protocol-name",
        "edit-protocol-description",
        "edit-steps-list",
        "edit-step-add",
        "edit-protocol-submit",
    ):
        assert token in html, f"protocols.html missing {token}"


def test_app_wires_protocol_edit():
    js = (FT / "app.js").read_text(encoding="utf-8")
    for token in (
        "protocol-edit",          # the card button to the right of Load
        "flex gap-2",             # Load + Edit live in one row
        "openEditProtocolModal",
        "handleProtocolEdit",
        "wireEditProtocolModal",
        '"protocol_updated"',     # WS dispatch case
        '"PATCH"',
        "/api/protocols/",
    ):
        assert token in js, f"app.js missing {token}"


def test_protocols_import_modal_accepts_pdf():
    html = (FT / "protocols.html").read_text(encoding="utf-8")
    for token in ("import-file", "application/pdf"):
        assert token in html, f"protocols.html missing {token}"


def test_app_wires_pdf_import():
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "/api/protocols/import/file" in js
    assert "importProtocolFromFile" in js
    assert "FormData" in js


def test_protocol_card_renders_ingredient_amounts():
    # The card shows ingredient name + amount (from `ingredients`), falling back
    # to plain `reagents` name pills when amounts are absent.
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "p.ingredients" in js
    assert "ing.reagent" in js
    assert "ing.display" in js
    assert "p.reagents" in js  # name-only fallback preserved


def test_import_shows_indeterminate_progress_animation():
    # The LLM-backed import shows an animated indeterminate bar (no fake %) while
    # the request is in flight.
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "setImportLoading" in js
    assert "import-progress-bar" in js
    css = (FT / "protocols.css").read_text(encoding="utf-8")
    assert "import-progress-bar" in css
    assert "@keyframes" in css


def test_app_renders_reproducibility_flag():
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "renderLogFlag" in js
    assert 'flag.status === "mismatch"' in js
    assert 'flag.status === "ok"' in js


def test_app_renders_skipped_step_status():
    # A skipped step renders a yellow "Skipped" label driven by skipped_indices.
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "skipped_indices" in js
    assert '"Skipped"' in js
    assert "border-tertiary" in js  # yellow accent for the skipped tracker row


def test_app_renders_manual_automatic_tag():
    # Notebook entries carry a manual/automatic provenance tag, rendered + editable.
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "entry_type" in js
    assert "edited" in js
    assert "entry-manual" in js
    assert "entry-automatic" in js


def test_app_wires_log_entry_editing():
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "wireLogEditModal" in js
    assert "patchLog" in js
    assert "data-edit-id" in js
    assert '"PATCH"' in js
    assert "/api/log/" in js


def test_notebook_has_edit_modal():
    html = (FT / "notebook.html").read_text(encoding="utf-8")
    for token in ('id="log-edit-modal"', 'id="log-edit-form"', 'id="log-edit-text"'):
        assert token in html, f"notebook.html missing {token}"


def test_notebook_css_has_flag_classes():
    css = (FT / "notebook.css").read_text(encoding="utf-8")
    assert ".log-flag" in css
    assert ".log-ok" in css
    assert ".flagged" in css
    assert ".entry-manual" in css
    assert ".entry-automatic" in css


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


def test_voice_dock_is_click_through():
    # The fixed voice dock / live transcript must not intercept clicks meant for
    # buttons beneath it; only the mic/control bar stays interactive.
    css = (FT / "shared.css").read_text(encoding="utf-8")
    assert "#voice-dock" in css and "pointer-events: none" in css
    assert "#voice-dock > div:not(#live-transcript)" in css
    assert "pointer-events: auto" in css


def test_app_wires_voice_pipeline():
    # P2b: the mic/transport subsystem moved to voice.js; app.js keeps the
    # integration seams (forwards WS events + wires the dock via window.LabVoice).
    js = (FT / "app.js").read_text(encoding="utf-8")
    for token in ("startMic", "stopMic", "onTranscript", "is_final", "wireNav", "LabVoice"):
        assert token in js, f"app.js missing {token}"
    voice = (FT / "voice.js").read_text(encoding="utf-8")
    for token in (
        "/ws/audio",
        "getUserMedia",
        "MediaRecorder",
        "set_muted",
        "startMic",
        "stopMic",
        "window.LabVoice",
    ):
        assert token in voice, f"voice.js missing {token}"


def test_every_page_loads_voice_module():
    for page in PAGES:
        html = (FT / page).read_text(encoding="utf-8")
        assert '<script src="voice.js" defer></script>' in html, f"{page} missing voice.js"


def test_pages_expose_live_hooks():
    cards = (FT / "protocols.html").read_text(encoding="utf-8")
    assert 'id="protocol-cards"' in cards
    assert 'id="inventory-rows"' in (FT / "inventory.html").read_text(encoding="utf-8")
    assert 'id="log-rows"' in (FT / "notebook.html").read_text(encoding="utf-8")
    guide = (FT / "guide.html").read_text(encoding="utf-8")
    assert 'id="step-tracker"' in guide
    assert 'id="live-transcript"' in guide


def test_dashboard_has_recent_notebooks_section():
    # The dashboard surfaces a "Recent Notebooks" section beneath the protocols,
    # populated by the shared client from the same /api/notebooks feed.
    html = (FT / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="dashboard-notebooks"' in html, "dashboard.html missing notebooks mount"
    assert "Recent Notebooks" in html, "dashboard.html missing Recent Notebooks heading"
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "renderDashboardNotebooks" in js, "app.js missing renderDashboardNotebooks"
    assert "dashboard-notebooks" in js, "app.js does not target the dashboard notebooks mount"


def test_dashboard_recent_protocols_is_distinct_from_catalog():
    # The dashboard "Recently Used Protocols" panel has its own mount + renderer,
    # separate from the full-detail catalog (#protocol-cards) on the protocols page.
    html = (FT / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="recent-protocols"' in html, "dashboard.html missing recent-protocols mount"
    assert 'id="protocol-cards"' not in html, "dashboard must not reuse the full catalog mount"
    assert "Recently Used Protocols" in html
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "renderRecentProtocols" in js, "app.js missing renderRecentProtocols"
    assert "/api/protocols/recent" in js, "app.js missing the recent endpoint"
    assert "recent-protocols" in js, "app.js does not target the recent-protocols mount"


def test_dashboard_highlights_active_protocol():
    # The recent-protocols card highlights the currently-loaded protocol the same
    # way the active-notebook card does (active flag -> border-primary + badge).
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "p.active" in js, "renderRecentProtocols must read the active flag"
    assert "recent-card--active" in js, "active protocol card needs a distinct marker"


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
    notebook = (FT / "notebook.html").read_text(encoding="utf-8")
    assert "Export PDF" not in notebook
    # The fake "Live Sync: Active" status indicator served no purpose and was removed.
    assert "Live Sync" not in notebook


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


def test_new_notebook_uses_styled_modal_not_prompt():
    notebook = (FT / "notebook.html").read_text(encoding="utf-8")
    # The "New" notebook button opens a styled modal mirroring Manual Entry.
    assert 'id="notebook-modal"' in notebook
    assert 'id="notebook-form"' in notebook
    assert 'id="notebook-name"' in notebook
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "openNotebookModal" in js
    assert "submitNotebookForm" in js
    # The bare browser prompt() popup must be gone.
    assert "window.prompt" not in js


def test_notebook_page_allows_scrolling():
    notebook = (FT / "notebook.html").read_text(encoding="utf-8")
    assert "overflow: hidden" not in notebook
    assert "width: 1280px" not in notebook
    assert "height: 1227px" not in notebook


def test_notebook_manual_entry_has_related_protocol_field():
    notebook = (FT / "notebook.html").read_text(encoding="utf-8")
    assert 'for="log-text"' in notebook
    assert ">Observation</label>" in notebook
    # The optional field is the related protocol (a dropdown), not a sample/tube id.
    assert 'for="log-protocol"' in notebook
    assert ">Related protocol (optional)</label>" in notebook
    assert '<select id="log-protocol"' in notebook
    # The old sample/tube field is gone.
    assert 'id="log-sample"' not in notebook
    assert "Sample / tube" not in notebook


def test_notebook_renderer_shows_newest_entries_first_without_mutating_log():
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "const displayLog = [...log].reverse();" in js
    assert "host.innerHTML = displayLog" in js
    assert "log.reverse()" not in js


def test_notebook_manual_entry_attaches_related_protocol():
    js = (FT / "app.js").read_text(encoding="utf-8")
    # The dropdown is filled from the live protocol list and sent as the entry
    # category; the old sample-only guard is gone with the sample/tube field.
    assert "populateLogProtocols" in js
    assert "log-protocol" in js
    assert "looksLikeSampleOnly" not in js
    assert "Put sample/tube values" not in js


def test_inventory_add_item_collects_structured_amount():
    inventory = (FT / "inventory.html").read_text(encoding="utf-8")
    assert 'id="additem-amount"' in inventory
    assert 'id="additem-unit"' in inventory
    assert 'for="additem-amount">Amount' in inventory
    assert 'for="additem-unit">Unit' in inventory


def test_inventory_has_no_expiration_field():
    # Expiration removed end to end: no modal input, no table column.
    inventory = (FT / "inventory.html").read_text(encoding="utf-8")
    assert "additem-expiration" not in inventory
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert ">Expiration</div>" not in js


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


def test_app_handles_protocol_completion():
    js = (FT / "app.js").read_text(encoding="utf-8")
    for token in (
        "step.finished",
        "protocol finished.",
        "PROTOCOL COMPLETE",
    ):
        assert token in js, f"app.js missing {token}"
    # Both action buttons must be disabled when the protocol is finished.
    assert "confirmBtn.disabled = idx < 0 || finished" in js
    assert "skipBtn.disabled = idx < 0 || finished" in js


def test_guide_transcript_box_has_minimum_height():
    # The Guide page's Live Transcription panel is always visible (no `hidden`),
    # so it reserves a minimum height instead of collapsing to nothing when empty
    # (e.g. while muted, when the box shows no text).
    css = (FT / "guide.css").read_text(encoding="utf-8")
    assert "#live-transcript" in css, "guide.css must scope a rule to #live-transcript"
    assert "min-height" in css, "guide.css must give #live-transcript a min-height"


def test_app_clears_transcript_when_muted():
    # Muting hides the transcript everywhere: on voice_state with muted=true the
    # client wipes the box so no spoken text lingers while muted.
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert "clearTranscriptForMute" in js, "app.js must clear the transcript on mute"


def test_guide_has_reagent_prep_modal_hooks():
    # Reagent prep is a modal that pops up on protocol load (lands on the Guide),
    # not an always-on dashboard panel. Overage was removed: samples only.
    html = (FT / "guide.html").read_text(encoding="utf-8")
    for token in ("prep-modal", "prep-open", "prep-samples", "prep-compute", "prep-table"):
        assert token in html, f"guide.html missing {token}"
    assert "prep-overage" not in html, "overage input should be gone from guide.html"


def test_dashboard_no_longer_has_reagent_prep_panel():
    # The static prep panel was moved off the dashboard into the Guide modal.
    html = (FT / "dashboard.html").read_text(encoding="utf-8")
    for token in ("prep-samples", "prep-overage", "prep-compute", "prep-table"):
        assert token not in html, f"dashboard.html should no longer contain {token}"


def test_app_wires_reagent_prep_client():
    js = (FT / "app.js").read_text(encoding="utf-8")
    for token in (
        "/api/scale",
        "fetchScale",
        "renderPrepTable",
        "handlePrepCompute",
        "prep-compute",
        "prep-table",
        "openPrepModal",
        "prep-modal-on-load",
    ):
        assert token in js, f"app.js missing {token}"
    # The overage input no longer exists, so the client must not read it.
    assert "prep-overage" not in js, "app.js should no longer reference prep-overage"


def test_app_dispatches_navigate_kind():
    # The hands-free "jump to guide" voice command emits a `navigate` command_result;
    # the client routes it (and re-centers the current step via scrollToRun/#run).
    js = (FT / "app.js").read_text(encoding="utf-8")
    assert '"navigate"' in js, "app.js missing navigate dispatch"
    assert "scrollToRun" in js, "app.js missing scrollToRun helper"


def test_every_page_has_jump_to_guide_nav_button():
    # The "Jump to guide" nav shortcut lives on every page (shown only when a
    # protocol is loaded) and links to the guide view with the #run hash.
    for page in PAGES:
        html = (FT / page).read_text(encoding="utf-8")
        assert 'id="resume-run"' in html, f"{page} missing Jump to guide nav button"
        assert 'href="guide.html#run"' in html, f"{page} resume-run missing #run target"


def test_commands_page_documents_jump_to_guide():
    html = (FT / "commands.html").read_text(encoding="utf-8")
    assert "Jump to guide" in html, "commands.html missing Jump to guide reference"
    assert "show_protocol" in html, "commands.html missing show_protocol custom action"


def test_notebook_has_export_menu():
    # The Activity Stream header has an Export button (right of Manual Entry) that
    # opens a small menu offering Markdown / CSV / PDF(print). The label must avoid
    # the literal "Export PDF" string guarded by test_dead_controls_removed.
    notebook = (FT / "notebook.html").read_text(encoding="utf-8")
    assert 'id="log-export"' in notebook
    assert 'id="log-export-menu"' in notebook
    for fmt in ('data-export="md"', 'data-export="csv"', 'data-export="pdf"'):
        assert fmt in notebook, f"notebook.html missing {fmt}"


def test_app_wires_notebook_export():
    # Client-side export: serialize the in-memory logCache (active notebook, in the
    # displayed sort order) to a downloadable Blob or a print window. No backend call.
    js = (FT / "app.js").read_text(encoding="utf-8")
    for token in (
        "wireNotebookExport",
        "exportNotebook",
        "exportNotebookMarkdown",
        "exportNotebookCSV",
        "printNotebook",
        "URL.createObjectURL",
        "sortLog(logCache",
    ):
        assert token in js, f"app.js missing {token}"
