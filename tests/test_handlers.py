"""Handler checks — hand-built Commands -> exact locked event shapes."""

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
    assert set(ev["payload"]) == {"timer_id", "label", "remaining_s", "expired"}
    assert ev["payload"]["expired"] is False


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


def test_auto_timer_can_be_disabled(monkeypatch):
    monkeypatch.setattr(handlers, "AUTO_TIMERS", False)
    state = fresh_state()
    events = handle_command(
        Command(intent="load_protocol", protocol_name="Bacterial Transformation"), state
    )
    assert all(e["type"] != "timer_update" for e in events)


def test_timers_do_not_auto_start_by_default(monkeypatch):
    # Default is manual: a timed step never auto-starts its countdown.
    monkeypatch.setattr(handlers, "AUTO_TIMERS", False)
    state = fresh_state()
    events = handle_command(
        Command(intent="load_protocol", protocol_name="Bacterial Transformation"), state
    )
    assert all(e["type"] != "timer_update" for e in events)
    events = handle_command(Command(intent="next_step"), state)
    assert all(e["type"] != "timer_update" for e in events)


def test_start_timer_without_duration_uses_current_step(monkeypatch):
    monkeypatch.setattr(handlers, "AUTO_TIMERS", False)
    state = fresh_state()
    # Bacterial Transformation step 1 is timed (thaw on ice, 600 s).
    handle_command(Command(intent="load_protocol", protocol_name="Bacterial Transformation"), state)
    events = handle_command(Command(intent="start_timer"), state)
    assert events[0]["type"] == "timer_update"
    assert events[0]["payload"]["label"] == "thaw on ice"
    assert events[0]["payload"]["remaining_s"] > 0


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


def test_prev_onto_timed_step_does_not_start_duplicate_timer(monkeypatch):
    monkeypatch.setattr(handlers, "AUTO_TIMERS", True)
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
    assert events[0]["payload"] == {"kind": "log_update", "id": entry_id, "text": "corrected"}


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
