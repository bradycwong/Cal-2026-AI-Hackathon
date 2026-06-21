"""Handler checks — hand-built Commands -> exact locked event shapes."""

import time

import backend.handlers as handlers
from backend.handlers import handle_command
from backend.schema import Command
from backend.state import SessionState


def fresh_state() -> SessionState:
    state = SessionState()
    state.load_files()
    return state


def test_load_emits_step_change():
    state = fresh_state()
    events = handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "command_result"
    p = ev["payload"]
    assert p["kind"] == "step_change"
    assert p["current_step"]["id"] == 1
    assert p["prev_step"] is None
    assert p["next_step"]["id"] == 2


def test_next_step_advances():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    events = handle_command(Command(intent="next_step"), state)
    assert events[0]["payload"]["current_step"]["id"] == 2


def test_next_step_without_protocol_clarifies():
    state = fresh_state()
    events = handle_command(Command(intent="next_step"), state)
    assert events[0]["payload"]["kind"] == "clarify"


def test_log_entry_field_translation():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    events = handle_command(
        Command(intent="log_entry", log_text="added 200 uL lysis buffer", sample_id="A"), state
    )
    p = events[0]["payload"]
    assert p["kind"] == "log_entry"
    assert p["text"] == "added 200 uL lysis buffer"  # log_text -> text
    assert p["sample_id"] == "A"
    assert p["step_ref"] == 1
    assert set(p) == {"kind", "id", "text", "timestamp", "sample_id", "step_ref"}


def test_start_timer_event_shape():
    state = fresh_state()
    events = handle_command(Command(intent="start_timer", duration_s=600, timer_label="incubation"), state)
    ev = events[0]
    assert ev["type"] == "timer_update"
    assert set(ev["payload"]) == {"timer_id", "label", "remaining_s", "expired", "paused"}
    assert ev["payload"]["expired"] is False
    assert ev["payload"]["paused"] is False  # explicit duration -> runs immediately


def test_auto_timer_starts_on_timed_step(monkeypatch):
    monkeypatch.setattr(handlers, "AUTO_TIMERS", True)
    state = fresh_state()
    # DNA Extraction step 1 is manual -> load emits only step_change.
    events = handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    assert [e["type"] for e in events] == ["command_result"]
    # Step 2 is a 10-min incubation -> advancing auto-starts a labelled timer.
    events = handle_command(Command(intent="next_step"), state)
    assert events[0]["type"] == "command_result"
    timer = events[1]
    assert timer["type"] == "timer_update"
    assert timer["payload"]["label"] == "incubation"
    assert timer["payload"]["remaining_s"] > 0


def test_auto_timer_starts_on_load_when_first_step_is_timed(monkeypatch):
    monkeypatch.setattr(handlers, "AUTO_TIMERS", True)
    state = fresh_state()
    # Bacterial Transformation step 1 IS timed (thaw on ice) -> timer on load.
    events = handle_command(
        Command(intent="load_protocol", protocol_name="Bacterial Transformation"), state
    )
    assert events[0]["payload"]["kind"] == "step_change"
    assert events[1]["type"] == "timer_update"
    assert events[1]["payload"]["label"] == "thaw on ice"


def test_timed_step_shows_paused_timer_by_default(monkeypatch):
    # Default (manual): a timed step surfaces a PAUSED timer card frozen at the
    # step's full duration instead of auto-counting down.
    monkeypatch.setattr(handlers, "AUTO_TIMERS", False)
    state = fresh_state()
    events = handle_command(
        Command(intent="load_protocol", protocol_name="Bacterial Transformation"), state
    )
    timer_events = [e for e in events if e["type"] == "timer_update"]
    assert len(timer_events) == 1
    p = timer_events[0]["payload"]
    assert p["paused"] is True
    assert p["label"] == "thaw on ice"
    assert p["remaining_s"] == 600  # frozen at the step's full duration


def test_paused_timer_does_not_tick():
    # A paused timer's remaining stays frozen regardless of elapsed time.
    state = fresh_state()
    timer = state.add_timer(600, "thaw on ice", paused=True)
    assert timer.paused is True
    assert timer.remaining_s() == 600
    time.sleep(1.1)
    assert timer.remaining_s() == 600


def test_start_timer_resumes_paused_step_timer(monkeypatch):
    # "start timer" resumes the same paused card (same id), now running.
    monkeypatch.setattr(handlers, "AUTO_TIMERS", False)
    state = fresh_state()
    load = handle_command(
        Command(intent="load_protocol", protocol_name="Bacterial Transformation"), state
    )
    paused = [e for e in load if e["type"] == "timer_update"][0]["payload"]
    assert paused["paused"] is True
    events = handle_command(Command(intent="start_timer"), state)
    p = events[0]["payload"]
    assert events[0]["type"] == "timer_update"
    assert p["timer_id"] == paused["timer_id"]
    assert p["paused"] is False
    assert p["label"] == "thaw on ice"
    assert p["remaining_s"] > 0


def test_start_timer_without_duration_on_untimed_step_clarifies(monkeypatch):
    monkeypatch.setattr(handlers, "AUTO_TIMERS", False)
    state = fresh_state()
    # DNA Extraction step 1 is manual (no duration) -> nothing to start.
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    events = handle_command(Command(intent="start_timer"), state)
    assert events[0]["payload"]["kind"] == "clarify"


def test_all_protocols_load_and_advance():
    # Generality: every shipped protocol loads and exposes step 1.
    state = fresh_state()
    for name in ["DNA Extraction", "PCR Setup", "Bacterial Transformation", "Plasmid Miniprep"]:
        events = handle_command(Command(intent="load_protocol", protocol_name=name), state)
        p = events[0]["payload"]
        assert p["kind"] == "step_change", f"{name} did not load"
        assert p["current_step"]["id"] == 1


def test_new_protocol_reagents_in_inventory():
    state = fresh_state()
    for reagent, expect in [
        ("master mix", "2X master mix"),
        ("competent cells", "Competent cells"),
        ("neutralization buffer", "Neutralization buffer"),
    ]:
        events = handle_command(Command(intent="find_inventory", reagent_name=reagent), state)
        p = events[0]["payload"]
        assert p["kind"] == "inventory_result", f"{reagent} not found"
        assert p["name"] == expect


def test_load_no_name_lists_all_protocols():
    state = fresh_state()
    events = handle_command(Command(intent="load_protocol"), state)
    msg = events[0]["payload"]["message"]
    for name in ["DNA Extraction", "PCR Setup", "Bacterial Transformation", "Plasmid Miniprep"]:
        assert name in msg


def test_find_inventory_hit():
    state = fresh_state()
    events = handle_command(Command(intent="find_inventory", reagent_name="proteinase K"), state)
    p = events[0]["payload"]
    assert p["kind"] == "inventory_result"
    assert p["name"] == "Proteinase K"
    assert "Freezer 2" in p["location"]


def test_find_inventory_miss_clarifies():
    state = fresh_state()
    events = handle_command(Command(intent="find_inventory", reagent_name="unobtainium"), state)
    assert events[0]["payload"]["kind"] == "clarify"
    assert "don't have a record" in events[0]["payload"]["message"]
