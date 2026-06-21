"""Handler checks — hand-built Commands -> exact locked event shapes."""

import time

import backend.handlers as handlers
from backend.handlers import handle_command
from backend.schema import Command
from backend.state import Protocol, SessionState, Step


def fresh_state() -> SessionState:
    state = SessionState()
    state.load_files()
    return state


def _advance_to_last(state: SessionState) -> None:
    """Step DNA Extraction up to (but not past) its final step (id 5, index 4)."""
    last = len(state.active_protocol.steps) - 1
    while state.current_step_index < last:
        handle_command(Command(intent="next_step"), state)


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


def _kind(events, kind):
    """First event payload of the given command_result kind (order-agnostic).

    ``next_step`` now emits a "Completed step N" log_entry alongside the
    step_change, so tests locate the event they care about by kind.
    """
    return next(e["payload"] for e in events if e["payload"].get("kind") == kind)


def test_next_step_advances():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    events = handle_command(Command(intent="next_step"), state)
    assert _kind(events, "step_change")["current_step"]["id"] == 2


def test_skip_step_advances_marks_skipped_and_logs():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    events = handle_command(Command(intent="skip_step"), state)
    # Cursor advanced to step 2...
    assert _kind(events, "step_change")["current_step"]["id"] == 2
    # ...the left step (index 0) is marked skipped (tracker renders it yellow)...
    assert 0 in state.skipped_steps
    # ...and the note says Skipped, not Completed.
    assert _kind(events, "log_entry")["text"].startswith("Skipped step 1")


def test_next_step_does_not_mark_skipped():
    # Contrast: a plain next_step completes the step, never marks it skipped.
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    handle_command(Command(intent="next_step"), state)
    assert 0 not in state.skipped_steps


def test_next_step_logs_completed_step_to_active_notebook():
    # A spoken/typed "next step" records the step it just left, so progress lands
    # in the notebook automatically.
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    before = len(state.log)
    events = handle_command(Command(intent="next_step"), state)
    entry = _kind(events, "log_entry")
    assert entry["text"].startswith("Completed step 1")
    assert entry["step_ref"] == 1  # the step we left, not the one we advanced to
    assert entry["step_log"] is True  # marks it so the UI stays on the guide page
    assert len(state.log) == before + 1


def test_step_nav_is_not_marked_loaded():
    # Only a fresh LOAD navigates; stepping must not bounce the user around.
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    for intent in ("next_step", "prev_step", "repeat_step"):
        events = handle_command(Command(intent=intent), state)
        assert _kind(events, "step_change")["loaded"] is False, intent


def test_step_change_carries_tracker_fields():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    events = handle_command(Command(intent="next_step"), state)
    payload = _kind(events, "step_change")
    assert payload["all_steps"]
    assert payload["current_index"] == 1
    assert payload["protocol_name"] == "DNA Extraction"


def test_next_step_without_protocol_clarifies():
    state = fresh_state()
    events = handle_command(Command(intent="next_step"), state)
    assert events[0]["payload"]["kind"] == "clarify"


def test_advance_step_skip_logs_skipped():
    # The Skip button advances AND writes a "Skipped step N" note, and marks the
    # step skipped so the tracker can render it yellow.
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    before = len(state.log)
    events = handlers.advance_step(state, completed=False)
    step_change = _kind(events, "step_change")
    assert step_change["current_step"]["id"] == 2
    entry = _kind(events, "log_entry")
    assert entry["text"].startswith("Skipped step 1")
    assert entry["step_ref"] == 1       # the step we left, not the one we advanced to
    assert entry["step_log"] is True    # keeps the UI on the guide page
    assert len(state.log) == before + 1
    # the left step's index (0) is now marked skipped, surfaced on step_change
    assert step_change["skipped_indices"] == [0]
    assert state.skipped_steps == {0}


def test_advance_step_complete_does_not_mark_skipped():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    events = handle_command(Command(intent="next_step"), state)
    assert _kind(events, "step_change")["skipped_indices"] == []
    assert state.skipped_steps == set()


def test_skipped_indices_persist_after_later_step_completed():
    # Skip step 1, then complete step 2 -> step 1 stays marked skipped.
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    handlers.advance_step(state, completed=False)        # skip step 1 (index 0)
    events = handlers.advance_step(state, completed=True)  # complete step 2 (index 1)
    assert _kind(events, "step_change")["skipped_indices"] == [0]
    assert state.skipped_steps == {0}


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
    assert set(p) == {
        "kind", "id", "text", "timestamp", "sample_id", "step_ref", "category",
        "flag", "step_log", "entry_type", "edited",
    }
    assert p["step_log"] is False  # a manual log entry is not a step-advance note
    assert p["entry_type"] == "manual"  # voice/typed "log ..." is a human note
    assert p["edited"] is False
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
    # Step 2 is a 10-min incubation -> advancing shows a paused labelled timer
    # (plus the auto "completed step 1" note).
    events = handle_command(Command(intent="next_step"), state)
    assert _kind(events, "step_change")  # present
    timers = [e for e in events if e["type"] == "timer_update"]
    assert len(timers) == 1
    timer = timers[0]
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


def test_clear_done_timers_removes_only_expired():
    state = fresh_state()
    state.add_timer(600, "incubation")                  # t1, running
    paused = state.add_timer(300, "thaw", paused=True)  # t2, paused
    done = state.add_timer(45, "heat shock")            # t3
    done.expired = True
    events = handle_command(Command(intent="clear_done_timers"), state)
    assert len(events) == 1
    assert events[0]["payload"]["kind"] == "timer_removed"
    assert events[0]["payload"]["timer_id"] == "t3"
    # Running + paused timers survive; only the expired one is gone.
    assert [t.timer_id for t in state.timers] == ["t1", "t2"]
    assert paused.paused is True


def test_clear_done_timers_multiple_expired():
    state = fresh_state()
    a = state.add_timer(45, "a")
    a.expired = True                  # t1, done
    state.add_timer(600, "b")         # t2, running
    c = state.add_timer(30, "c")
    c.expired = True                  # t3, done
    events = handle_command(Command(intent="clear_done_timers"), state)
    assert {e["payload"]["timer_id"] for e in events} == {"t1", "t3"}
    assert all(e["payload"]["kind"] == "timer_removed" for e in events)
    assert [t.timer_id for t in state.timers] == ["t2"]


def test_clear_done_timers_with_no_expired_clarifies():
    state = fresh_state()
    state.add_timer(600, "incubation")  # running, not expired
    events = handle_command(Command(intent="clear_done_timers"), state)
    assert events[0]["payload"]["kind"] == "clarify"
    assert "no finished timers" in events[0]["payload"]["message"].lower()
    assert [t.timer_id for t in state.timers] == ["t1"]  # untouched


def test_remove_expired_timers_helper():
    state = fresh_state()
    a = state.add_timer(45, "a")
    a.expired = True              # t1, done
    state.add_timer(600, "b")     # t2, running
    assert state.remove_expired_timers() == ["t1"]
    assert [t.timer_id for t in state.timers] == ["t2"]
    assert state.remove_expired_timers() == []  # idempotent: nothing left to clear


def test_completing_timed_step_clears_its_paused_timer():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    handle_command(Command(intent="next_step"), state)  # onto step 2 (timed) -> paused t1
    assert [t.step_id for t in state.timers] == [2]
    events = handle_command(Command(intent="next_step"), state)  # leave step 2
    removed = [e["payload"]["timer_id"] for e in events if e["payload"].get("kind") == "timer_removed"]
    assert removed == ["t1"]
    assert state.timers == []


def test_completing_timed_step_clears_its_running_timer():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    handle_command(Command(intent="next_step"), state)   # onto step 2 -> paused t1
    handle_command(Command(intent="start_timer"), state)  # resume -> t1 running, still step_id=2
    assert state.timers[0].paused is False
    events = handle_command(Command(intent="next_step"), state)  # leave step 2
    assert any(e["payload"].get("kind") == "timer_removed" for e in events)
    assert state.timers == []


def test_skipping_timed_step_clears_its_timer():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    handle_command(Command(intent="next_step"), state)  # onto step 2 -> paused t1
    assert len(state.timers) == 1
    events = handle_command(Command(intent="skip_step"), state)  # skip step 2
    assert any(e["payload"].get("kind") == "timer_removed" for e in events)
    assert state.timers == []
    assert 1 in state.skipped_steps  # index 1 (step 2) marked skipped


def test_finishing_last_timed_step_clears_its_timer():
    # Synthetic single-timed-step protocol: completing it finishes AND clears.
    state = fresh_state()
    state.active_protocol = Protocol(
        id="solo_timed", name="Solo Timed",
        steps=[Step(id=1, text="incubate", duration_s=600, timer_label="incubation")],
    )
    state.current_step_index = 0
    state.protocol_complete = False
    state.add_timer(600, "incubation", paused=True, step_id=1)  # the step's own timer
    events = handle_command(Command(intent="next_step"), state)  # finish
    assert _kind(events, "step_change")["finished"] is True
    assert state.protocol_complete is True
    assert any(e["payload"].get("kind") == "timer_removed" for e in events)
    assert state.timers == []


def test_ad_hoc_timer_survives_step_advance_while_step_timer_cleared():
    # The user's exact scenario: a timer started by explicit duration is NOT the
    # step's own timer, so completing the step leaves it running.
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    handle_command(Command(intent="next_step"), state)  # onto step 2 -> t1 step_id=2
    handle_command(Command(intent="start_timer", duration_s=300), state)  # t2 ad-hoc, step_id None
    assert [t.step_id for t in state.timers] == [2, None]
    events = handle_command(Command(intent="next_step"), state)  # leave step 2
    removed = [e["payload"]["timer_id"] for e in events if e["payload"].get("kind") == "timer_removed"]
    assert removed == ["t1"]                              # only the step's own timer
    assert [t.timer_id for t in state.timers] == ["t2"]  # ad-hoc survives
    assert state.timers[0].step_id is None


def test_completing_untimed_step_emits_no_timer_removed():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    events = handle_command(Command(intent="next_step"), state)  # leave step 1 (untimed)
    assert all(e["payload"].get("kind") != "timer_removed" for e in events)


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


def test_show_protocol_emits_navigate_to_guide():
    # "jump to guide" / "what step am I on" navigates the operator to the guide view
    # (and the #run hash centers the current step on arrival). No state mutation.
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    idx_before = state.current_step_index

    events = handle_command(Command(intent="show_protocol"), state)

    assert len(events) == 1
    p = events[0]["payload"]
    assert p["kind"] == "navigate"
    assert p["page"] == "guide.html"
    assert p["hash"] == "#run"
    assert state.current_step_index == idx_before  # navigation never advances


def test_show_protocol_without_protocol_clarifies():
    # Nothing loaded -> tell the operator to load one, don't drop them on an
    # empty guide.
    state = fresh_state()

    events = handle_command(Command(intent="show_protocol"), state)

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
    # A human correction re-tags the entry manual + edited.
    assert events[0]["payload"] == {
        "kind": "log_update", "id": entry_id, "text": "corrected", "flag": None,
        "entry_type": "manual", "edited": True,
    }


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


# --- manual/automatic tagging + by-id edit --------------------------------

def test_log_entry_is_tagged_manual():
    state = fresh_state()
    events = handle_command(Command(intent="log_entry", log_text="added 200 uL", sample_id=None), state)
    entry = _kind(events, "log_entry")
    assert entry["entry_type"] == "manual"
    assert entry["edited"] is False
    assert state.log[-1]["entry_type"] == "manual"


def test_step_advance_note_is_tagged_automatic():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    events = handle_command(Command(intent="next_step"), state)
    entry = _kind(events, "log_entry")
    assert entry["entry_type"] == "automatic"
    assert entry["edited"] is False


def test_edit_log_entry_retags_automatic_note_manual_edited():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    handle_command(Command(intent="next_step"), state)  # automatic "Completed step 1" note
    auto_id = state.log[-1]["id"]
    assert state.log[-1]["entry_type"] == "automatic"

    entry = handlers.edit_log_entry(state, auto_id, "Completed step 1 (amended)")
    assert entry is not None
    assert entry["text"] == "Completed step 1 (amended)"
    assert entry["entry_type"] == "manual"
    assert entry["edited"] is True
    assert state.log[-1]["text"] == "Completed step 1 (amended)"


def test_edit_log_entry_unknown_id_returns_none():
    state = fresh_state()
    handle_command(Command(intent="log_entry", log_text="present", sample_id=None), state)
    assert handlers.edit_log_entry(state, 999, "nope") is None


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


# --- protocol completion on final "next step" -------------------------------


def test_final_next_step_finishes_protocol():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    _advance_to_last(state)

    events = handle_command(Command(intent="next_step"), state)

    sc = _kind(events, "step_change")
    assert sc["finished"] is True
    assert sc["current_step"]["id"] == 5
    assert sc["current_index"] == 4
    assert state.current_step_index == 4
    assert state.protocol_complete is True
    # The final step is logged exactly once...
    log = _kind(events, "log_entry")
    assert log["text"].startswith("Completed step 5")
    # ...and finishing emits no timer.
    assert all(e["type"] != "timer_update" for e in events)


def test_repeated_next_step_after_finish_is_idempotent():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    _advance_to_last(state)
    handle_command(Command(intent="next_step"), state)
    log_len = len(state.log)

    events = handle_command(Command(intent="next_step"), state)

    sc = _kind(events, "step_change")
    assert sc["finished"] is True
    assert sc["current_step"]["id"] == 5
    assert state.current_step_index == 4
    # No duplicate final log entry, and no log_entry event re-emitted.
    assert len(state.log) == log_len
    assert all(e["payload"].get("kind") != "log_entry" for e in events)


def test_skip_finishes_and_logs_skipped():
    # Skip on the final step finishes the protocol AND logs a "Skipped step N"
    # note (mirroring the Skip-anywhere convention) and marks the row skipped.
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    _advance_to_last(state)
    log_len = len(state.log)

    events = handlers.advance_step(state, completed=False)

    sc = _kind(events, "step_change")
    assert sc["finished"] is True
    assert state.protocol_complete is True
    assert _kind(events, "log_entry")["text"].startswith("Skipped step 5")
    assert len(state.log) == log_len + 1
    assert state.skipped_steps == {4}


def test_one_step_protocol_finishes_on_first_next_step():
    state = fresh_state()
    state.active_protocol = Protocol(id="solo", name="Solo", steps=[Step(id=1, text="only step")])
    state.current_step_index = 0
    state.protocol_complete = False

    events = handle_command(Command(intent="next_step"), state)

    sc = _kind(events, "step_change")
    assert sc["finished"] is True
    assert sc["current_step"]["id"] == 1
    assert state.current_step_index == 0
    assert state.protocol_complete is True
    assert _kind(events, "log_entry")["text"].startswith("Completed step 1")


def test_prev_repeat_and_load_clear_finished():
    state = fresh_state()
    handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    _advance_to_last(state)
    handle_command(Command(intent="next_step"), state)
    assert state.protocol_complete is True

    events = handle_command(Command(intent="prev_step"), state)
    assert _kind(events, "step_change")["finished"] is False
    assert state.protocol_complete is False

    # Re-finish (prev_step dropped us off the last step), then repeat clears it.
    _advance_to_last(state)
    handle_command(Command(intent="next_step"), state)
    assert state.protocol_complete is True
    events = handle_command(Command(intent="repeat_step"), state)
    assert _kind(events, "step_change")["finished"] is False
    assert state.protocol_complete is False

    # Re-finish, then loading another protocol clears it.
    handle_command(Command(intent="next_step"), state)
    assert state.protocol_complete is True
    events = handle_command(Command(intent="load_protocol", protocol_name="PCR Setup"), state)
    assert _kind(events, "step_change")["finished"] is False
    assert state.protocol_complete is False


def test_normal_load_and_advance_emit_finished_false():
    state = fresh_state()
    load = handle_command(Command(intent="load_protocol", protocol_name="DNA Extraction"), state)
    assert _kind(load, "step_change")["finished"] is False
    events = handle_command(Command(intent="next_step"), state)
    assert _kind(events, "step_change")["finished"] is False
    assert state.protocol_complete is False


def test_inventory_match_helper_exact_and_fuzzy():
    from backend.inventory import find_inventory_match

    state = fresh_state()

    exact = find_inventory_match("Proteinase K", state.inventory)
    assert exact is not None
    assert exact.name == "Proteinase K"

    fuzzy = find_inventory_match("proteinase k", state.inventory)
    assert fuzzy is not None
    assert fuzzy.name == "Proteinase K"

    substring = find_inventory_match("master mix", state.inventory)
    assert substring is not None
    assert substring.name == "2X master mix"


def test_inventory_match_helper_miss():
    from backend.inventory import find_inventory_match

    state = fresh_state()
    assert find_inventory_match("unobtainium", state.inventory) is None
