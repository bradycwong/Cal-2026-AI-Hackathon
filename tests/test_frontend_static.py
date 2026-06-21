from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_composer_is_wired_to_demo_datalist():
    html = (ROOT / "frontend" / "index.html").read_text()
    assert 'list="demo-lines"' in html
    assert 'id="demo-lines"' in html


def test_log_has_empty_state_seed():
    html = (ROOT / "frontend" / "index.html").read_text()
    assert "log-empty" in html
    assert "No entries yet" in html


def test_app_handles_new_command_result_kinds():
    js = (ROOT / "frontend" / "app.js").read_text()
    assert 'case "log_removed"' in js
    assert 'case "log_update"' in js
    assert 'case "ask_result"' in js
    assert "dataset.logId" in js
    assert "ensureLogEmptyState" in js
    assert "DEMO_LINES" in js


def test_app_renders_reproducibility_flag():
    js = (ROOT / "frontend" / "app.js").read_text()
    assert "p.flag" in js                 # onLogEntry reads the new flag field
    assert "mismatch" in js               # branches on flag.status
    assert "flagged" in js                # tags the <li> for the mismatch accent
    assert "log-flag" in js               # renders the warning line


def test_styles_have_flag_classes():
    css = (ROOT / "frontend" / "styles.css").read_text()
    assert ".log-flag" in css             # warning line (reuses the --warn token)
    assert ".flagged" in css              # row accent
    assert ".log-ok" in css               # subtle check on a matched entry
