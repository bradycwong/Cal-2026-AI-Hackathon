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
    assert p["loaded"] is True  # drives front-end navigation to the active protocol


def test_next_step_advances():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    events = handle_command(Command(intent="next_step"), state)
    assert events[0]["payload"]["current_step"]["id"] == 2


def test_step_nav_is_not_marked_loaded():
    # Only a fresh LOAD navigates; stepping must not bounce the user around.
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    for intent in ("next_step", "prev_step", "repeat_step"):
        ev = handle_command(Command(intent=intent), state)[0]
        assert ev["payload"]["loaded"] is False, intent


def test_step_change_carries_tracker_fields():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    events = handle_command(Command(intent="next_step"), state)
    payload = events[0]["payload"]
    assert payload["kind"] == "step_change"
    assert payload["all_steps"]
    assert payload["current_index"] == 1
    assert payload["protocol_name"] == "DNA Extraction"


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
    assert set(p) == {"kind", "id", "text", "timestamp", "sample_id", "step_ref", "category", "flag"}
    # DNA Extraction step 1 expects 200 uL; the logged 200 uL matches.
    assert p["flag"]["status"] == "ok"


def test_log_entry_flags_volume_mismatch_and_correct_clears_it():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    events = handle_command(
        Command(intent="log_entry", log_text="added 250 uL lysis buffer", sample_id="A"), state
    )
    assert events[0]["payload"]["flag"]["status"] == "mismatch"
    # Log text is preserved exactly; the flag never rewrites it.
    assert events[0]["payload"]["text"] == "added 250 uL lysis buffer"

    events = handle_command(
        Command(intent="correct_log", log_text="added 200 uL lysis buffer"), state
    )
    assert events[0]["payload"]["kind"] == "log_update"
    assert events[0]["payload"]["flag"]["status"] == "ok"
    assert state.log[-1]["text"] == "added 200 uL lysis buffer"


def test_start_timer_event_shape():
    state = fresh_state()
    events = handle_command(Command(intent="start_timer", duration_s=600, timer_label="incubation"), state)
    ev = events[0]
    assert ev["type"] == "timer_update"
    assert set(ev["payload"]) == {"timer_id", "label", "remaining_s", "expired", "paused"}
    assert ev["payload"]["expired"] is False
    assert ev["payload"]["paused"] is False  # explicit duration -> runs immediately


def test_advancing_to_timed_step_shows_paused_timer():
    # Advancing onto a timed step surfaces a PAUSED card frozen at full duration;
    # it must NOT auto-start the countdown.
    state = fresh_state()
    # DNA Extraction step 1 is manual -> load emits only step_change.
    events = handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    assert [e["type"] for e in events] == ["command_result"]
    # Step 2 is a 10-min incubation -> advancing shows a paused labelled timer.
    events = handle_command(Command(intent="next_step"), state)
    assert events[0]["type"] == "command_result"
    timer = events[1]
    assert timer["type"] == "timer_update"
    assert timer["payload"]["label"] == "incubation"
    assert timer["payload"]["paused"] is True
    assert timer["payload"]["remaining_s"] == 600  # frozen at full duration


def test_timed_step_shows_paused_timer_by_default():
    # A timed step surfaces a PAUSED timer card frozen at the step's full
    # duration instead of auto-counting down.
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


def test_start_timer_resumes_paused_step_timer():
    # "start timer" resumes the same paused card (same id), now running.
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


def test_start_timer_without_duration_on_untimed_step_clarifies():
    state = fresh_state()
    # DNA Extraction step 1 is manual (no duration) -> nothing to start.
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    events = handle_command(Command(intent="start_timer"), state)
    assert events[0]["payload"]["kind"] == "clarify"


def test_stop_timer_cancels_all_active_timers():
    state = fresh_state()
    state.add_timer(600, "incubation")
    state.add_timer(45, "heat shock")
    assert len(state.timers) == 2
    events = handle_command(Command(intent="stop_timer"), state)
    assert len(events) == 2
    assert all(e["payload"]["kind"] == "timer_removed" for e in events)
    assert {e["payload"]["timer_id"] for e in events} == {"t1", "t2"}
    assert state.timers == []


def test_stop_timer_with_no_timers_clarifies():
    state = fresh_state()
    events = handle_command(Command(intent="stop_timer"), state)
    assert events[0]["payload"]["kind"] == "clarify"
    assert "no active timers" in events[0]["payload"]["message"].lower()


def test_remove_timer_by_id():
    state = fresh_state()
    state.add_timer(600, "incubation")  # t1
    state.add_timer(45, "heat shock")   # t2
    assert state.remove_timer("t1") is True
    assert [t.timer_id for t in state.timers] == ["t2"]
    assert state.remove_timer("nope") is False


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


def test_prev_step_goes_back():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    handle_command(Command(intent="next_step"), state)

    events = handle_command(Command(intent="prev_step"), state)

    assert len(events) == 1
    assert events[0]["payload"]["kind"] == "step_change"
    assert events[0]["payload"]["current_step"]["id"] == 1
    assert state.current_step_index == 0


def test_prev_step_at_first_step_clarifies():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)

    events = handle_command(Command(intent="prev_step"), state)

    assert events[0]["payload"]["kind"] == "clarify"
    assert "first step" in events[0]["payload"]["message"]
    assert state.current_step_index == 0


def test_prev_step_without_protocol_clarifies():
    state = fresh_state()

    events = handle_command(Command(intent="prev_step"), state)

    assert events[0]["payload"]["kind"] == "clarify"


def test_repeat_step_reemits_current_step_without_advancing():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)

    events = handle_command(Command(intent="repeat_step"), state)

    assert len(events) == 1
    assert events[0]["payload"]["kind"] == "step_change"
    assert events[0]["payload"]["current_step"]["id"] == 1
    assert state.current_step_index == 0


def test_repeat_step_without_protocol_clarifies():
    state = fresh_state()

    events = handle_command(Command(intent="repeat_step"), state)

    assert events[0]["payload"]["kind"] == "clarify"


def test_prev_onto_timed_step_does_not_start_duplicate_timer():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    handle_command(Command(intent="next_step"), state)
    handle_command(Command(intent="next_step"), state)
    timer_count = len(state.timers)

    events = handle_command(Command(intent="prev_step"), state)

    assert events[0]["payload"]["kind"] == "step_change"
    assert events[0]["payload"]["current_step"]["id"] == 2
    assert all(e["type"] != "timer_update" for e in events)
    assert len(state.timers) == timer_count


def test_undo_log_removes_last_entry():
    state = fresh_state()
    handle_command(Command(intent="log_entry", log_text="first", sample_id=None), state)
    entry_id = state.log[-1]["id"]

    events = handle_command(Command(intent="undo_log"), state)

    assert state.log == []
    assert events[0]["payload"] == {"kind": "log_removed", "id": entry_id}


def test_undo_log_on_empty_log_clarifies():
    state = fresh_state()

    events = handle_command(Command(intent="undo_log"), state)

    assert events[0]["payload"]["kind"] == "clarify"
    assert "nothing to undo" in events[0]["payload"]["message"].lower()


def test_correct_log_updates_last_entry():
    state = fresh_state()
    handle_command(Command(intent="log_entry", log_text="original", sample_id="A"), state)
    entry_id = state.log[-1]["id"]

    events = handle_command(Command(intent="correct_log", log_text="corrected"), state)

    assert state.log[-1]["text"] == "corrected"
    assert events[0]["payload"] == {"kind": "log_update", "id": entry_id, "text": "corrected", "flag": None}


def test_correct_log_without_replacement_clarifies():
    state = fresh_state()
    handle_command(Command(intent="log_entry", log_text="original", sample_id=None), state)

    events = handle_command(Command(intent="correct_log"), state)

    assert events[0]["payload"]["kind"] == "clarify"
    assert "change the last note" in events[0]["payload"]["message"].lower()


def test_correct_log_on_empty_log_clarifies():
    state = fresh_state()

    events = handle_command(Command(intent="correct_log", log_text="replacement"), state)

    assert events[0]["payload"]["kind"] == "clarify"
    assert "nothing to correct" in events[0]["payload"]["message"].lower()


def test_ask_without_question_clarifies():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)

    events = handle_command(Command(intent="ask"), state)

    assert events[0]["payload"]["kind"] == "clarify"
    assert "what would you like to ask" in events[0]["payload"]["message"].lower()


def test_ask_without_protocol_clarifies():
    state = fresh_state()

    events = handle_command(Command(intent="ask", question="How much lysis buffer?"), state)

    assert events[0]["payload"]["kind"] == "clarify"
    assert "load a protocol first" in events[0]["payload"]["message"].lower()


def test_ask_returns_answer(monkeypatch):
    from backend import router

    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    monkeypatch.setattr(router, "answer_question", lambda q, p: "Use 200 uL.")

    events = handle_command(Command(intent="ask", question="How much lysis buffer?"), state)

    assert events[0]["payload"] == {
        "kind": "ask_result",
        "question": "How much lysis buffer?",
        "answer": "Use 200 uL.",
    }
