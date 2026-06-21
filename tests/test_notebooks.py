"""Multi-notebook model — each notebook owns its own log feed."""

import os

os.environ["LAB_DB_PATH"] = ":memory:"  # set before importing the app module

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.handlers import handle_command
from backend.schema import Command
from backend.state import SessionState


# --- state model (in-memory) ----------------------------------------------
def fresh_state():
    state = SessionState()  # in-memory, no db
    state.load_files()
    return state


def test_default_notebook_exists_and_is_active():
    state = fresh_state()
    nbs = state.notebooks_view()
    assert len(nbs) == 1
    assert nbs[0]["active"] is True
    assert nbs[0]["entry_count"] == 0
    assert state.active_notebook_id == nbs[0]["id"]


def test_logs_land_in_the_active_notebook():
    state = fresh_state()
    handle_command(Command(intent="log_entry", log_text="note in default"), state)
    nbs = state.notebooks_view()
    assert nbs[0]["entry_count"] == 1


def test_create_switches_active_and_isolates_logs():
    state = fresh_state()
    handle_command(Command(intent="log_entry", log_text="in default"), state)

    nb = state.create_notebook("PCR Run 1")
    assert state.active_notebook_id == nb["id"]
    assert state.log == []  # new notebook starts empty

    handle_command(Command(intent="log_entry", log_text="in pcr"), state)
    assert len(state.log) == 1
    assert state.log[-1]["text"] == "in pcr"

    view = {n["name"]: n["entry_count"] for n in state.notebooks_view()}
    assert view["Lab Notebook"] == 1
    assert view["PCR Run 1"] == 1


def test_select_switches_the_feed_back():
    state = fresh_state()
    default_id = state.active_notebook_id
    handle_command(Command(intent="log_entry", log_text="in default"), state)
    state.create_notebook("Other")
    handle_command(Command(intent="log_entry", log_text="in other"), state)

    assert state.select_notebook(default_id) is True
    assert [e["text"] for e in state.log] == ["in default"]


def test_select_unknown_notebook_returns_false():
    state = fresh_state()
    assert state.select_notebook(99999) is False


# --- persistence (db-backed) ----------------------------------------------
def test_notebooks_and_scoped_logs_survive_restart(tmp_path):
    db = str(tmp_path / "lab.db")
    s1 = SessionState(db_path=db)
    s1.load_files()
    handle_command(Command(intent="log_entry", log_text="default note"), s1)
    nb = s1.create_notebook("Experiment B")
    handle_command(Command(intent="log_entry", log_text="b note"), s1)

    s2 = SessionState(db_path=db)
    s2.load_files()
    # active notebook (Experiment B) persisted, with only its own note
    assert s2.active_notebook_id == nb["id"]
    assert [e["text"] for e in s2.log] == ["b note"]
    names = {n["name"]: n["entry_count"] for n in s2.notebooks_view()}
    assert names == {"Lab Notebook": 1, "Experiment B": 1}


# --- endpoints -------------------------------------------------------------
@pytest.fixture
def client(tmp_path):
    main.state = SessionState(db_path=":memory:", data_dir=tmp_path)
    main.state.load_files()
    with TestClient(main.app) as c:
        yield c


def test_list_notebooks_endpoint(client):
    r = client.get("/api/notebooks")
    assert r.status_code == 200
    body = r.json()
    assert len(body["notebooks"]) == 1
    assert body["active_id"] == body["notebooks"][0]["id"]


def test_create_and_select_notebook_endpoints(client):
    r = client.post("/api/notebooks", json={"name": "Plasmid Prep"})
    assert r.status_code == 201
    created = r.json()
    new_id = created["notebook"]["id"]
    assert created["active_id"] == new_id

    # a log now lands in the new notebook
    client.post("/api/log", json={"text": "prep note"})
    assert [e["text"] for e in client.get("/api/log").json()["log"]] == ["prep note"]

    # switch back to the default notebook -> empty feed
    default_id = next(n["id"] for n in created["notebooks"] if n["name"] == "Lab Notebook")
    r = client.post(f"/api/notebooks/{default_id}/select")
    assert r.status_code == 200
    assert r.json()["active_id"] == default_id
    assert client.get("/api/log").json()["log"] == []


def test_select_missing_notebook_404s(client):
    assert client.post("/api/notebooks/4242/select").status_code == 404
