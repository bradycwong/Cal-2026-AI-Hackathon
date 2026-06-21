"""Persistence checks — the log survives a fresh SessionState (i.e. a restart)."""

from backend.handlers import handle_command
from backend.schema import Command
from backend.state import SessionState


def test_log_survives_restart(tmp_path):
    db = str(tmp_path / "lab.db")

    s1 = SessionState(db_path=db)
    s1.load_files()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), s1)
    handle_command(
        Command(intent="log_entry", log_text="added 200 uL lysis buffer", sample_id="A"), s1
    )
    first_id = s1.log[-1]["id"]

    # Simulate a process restart: brand-new state, same DB file.
    s2 = SessionState(db_path=db)
    s2.load_files()
    assert len(s2.log) == 1
    row = s2.log[0]
    assert row["text"] == "added 200 uL lysis buffer"
    assert row["sample_id"] == "A"
    assert row["step_ref"] == 1
    # ids keep counting up from what's already on disk.
    handle_command(Command(intent="log_entry", log_text="second note", sample_id=None), s2)
    assert s2.log[-1]["id"] == first_id + 1


def test_flag_survives_restart(tmp_path):
    flag = {
        "parameter": "volume_ul",
        "expected": 200,
        "logged": 250,
        "unit": "uL",
        "status": "mismatch",
    }
    db = str(tmp_path / "lab.db")
    state = SessionState(db_path=db)
    state.load_files()
    state.append_log("added 250 uL", sample_id="A", category="DNA Extraction", flag=flag)

    state2 = SessionState(db_path=db)
    state2.load_files()
    assert state2.log[-1]["flag"] == flag


def test_corrected_flag_survives_restart(tmp_path):
    db = str(tmp_path / "lab.db")
    s1 = SessionState(db_path=db)
    s1.load_files()
    mismatch = {"parameter": "volume_ul", "expected": 200, "logged": 250,
                "unit": "uL", "status": "mismatch"}
    s1.append_log("added 250 uL", sample_id="A", category="DNA Extraction", flag=mismatch)
    ok = {"parameter": "volume_ul", "expected": 200, "logged": 200,
          "unit": "uL", "status": "ok"}
    s1.update_last_log("added 200 uL", ok)

    s2 = SessionState(db_path=db)
    s2.load_files()
    assert s2.log[-1]["flag"] == ok


def test_in_memory_default_has_no_persistence(tmp_path):
    # Default (no db_path) keeps the old pure in-memory behaviour.
    s = SessionState()
    s.load_files()
    handle_command(Command(intent="log_entry", log_text="ephemeral", sample_id=None), s)
    assert s.notes is None
    assert s.log[-1]["id"] == 1


def test_undo_log_persists_delete(tmp_path):
    db = str(tmp_path / "lab.db")

    s1 = SessionState(db_path=db)
    s1.load_files()
    handle_command(Command(intent="log_entry", log_text="first note", sample_id=None), s1)
    removed = s1.pop_log()
    assert removed["text"] == "first note"
    assert s1.log == []

    s2 = SessionState(db_path=db)
    s2.load_files()
    assert s2.log == []

    handle_command(Command(intent="log_entry", log_text="second note", sample_id=None), s2)
    assert s2.log[-1]["id"] > removed["id"]


def test_correct_log_persists_update(tmp_path):
    db = str(tmp_path / "lab.db")

    s1 = SessionState(db_path=db)
    s1.load_files()
    handle_command(Command(intent="log_entry", log_text="original note", sample_id="A"), s1)
    updated = s1.update_last_log("corrected note")
    assert updated["text"] == "corrected note"
    assert updated["sample_id"] == "A"

    s2 = SessionState(db_path=db)
    s2.load_files()
    assert len(s2.log) == 1
    assert s2.log[0]["text"] == "corrected note"
    assert s2.log[0]["sample_id"] == "A"
