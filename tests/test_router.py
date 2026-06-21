"""Router harness — fast, no UI, no network. Forces the deterministic path.

Covers the five demo lines + the key negative case (load with no name must NOT
guess a protocol). Intentionally minimal per the hackathon directive.
"""

import os

os.environ["ROUTER_MODE"] = "deterministic"

from backend.router import deterministic_route, normalize_ascii, route


def test_normalize_units():
    assert normalize_ascii("200 microliters") == "200 uL"
    assert normalize_ascii("200 \u00b5L lysis") == "200 uL lysis"
    assert "degrees C" in normalize_ascii("incubate at 65 \u00b0C")


def test_load_protocol():
    cmd = route("Load DNA extraction protocol.")
    assert cmd.intent == "load_protocol"
    assert cmd.protocol_name and "dna" in cmd.protocol_name.lower()


def test_load_protocol_no_name_does_not_guess():
    cmd = route("Load a protocol.")
    assert cmd.protocol_name is None
    assert cmd.clarify_prompt  # non-empty question, never a guessed name


def test_log_entry_extracts_sample_and_ascii_units():
    cmd = route("Log: added 200 microliters lysis buffer to sample A.")
    assert cmd.intent == "log_entry"
    assert cmd.sample_id == "A"
    assert "200 uL lysis buffer" in cmd.log_text


def test_load_protocol_verb_dropped_by_stt():
    # STT sometimes drops the leading "load"; the spoken name is still explicit.
    cmd = route("DNA extraction protocol.")
    assert cmd.intent == "load_protocol"
    assert cmd.protocol_name and "dna" in cmd.protocol_name.lower()


def test_start_timer():
    cmd = route("Start a 10-minute incubation timer.")
    assert cmd.intent == "start_timer"
    assert cmd.duration_s == 600
    assert cmd.timer_label == "incubation"


def test_start_timer_seconds():
    cmd = route("Start a 30 second timer.")
    assert cmd.intent == "start_timer"
    assert cmd.duration_s == 30


def test_start_timer_no_duration_defers_to_handler():
    # Bare "start timer" carries no duration so the handler can use the step's.
    cmd = route("Start timer")
    assert cmd.intent == "start_timer"
    assert cmd.duration_s is None
    assert cmd.timer_label is None


def test_stop_timer_variants_route_to_stop_timer():
    for phrase in (
        "Stop timer",
        "stop the timer",
        "stop all timers",
        "cancel the timer",
        "stop the alarm",
        "stop beeping",
    ):
        assert route(phrase).intent == "stop_timer", phrase


def test_stop_timer_wins_over_start_timer():
    # "stop timer" contains "timer" but must not be parsed as start_timer.
    assert route("stop timer").intent == "stop_timer"
    assert route("start timer").intent == "start_timer"


def test_find_inventory():
    cmd = route("Where's the proteinase K?")
    assert cmd.intent == "find_inventory"
    assert "proteinase" in cmd.reagent_name.lower()


def test_next_step():
    assert route("What's next?").intent == "next_step"
    assert route("Next step").intent == "next_step"


def test_confirm_action_routes_to_next_step():
    # "Confirm Action" is the Guide button's label; saying it should advance too.
    for phrase in ("Confirm action", "confirm", "confirm this step", "confirmed"):
        assert route(phrase).intent == "next_step", phrase


def test_completion_phrasings_route_to_next_step():
    # Natural "I finished this step, move on" phrasings all advance.
    for phrase in (
        "done",
        "step done",
        "I'm done",
        "all done",
        "complete",
        "completed",
        "step complete",
        "mark complete",
        "mark it done",
        "finished",
        "continue",
        "proceed",
        "move on",
        "moving on",
        "advance",
        "next",
    ):
        assert route(phrase).intent == "next_step", phrase


def test_completion_words_inside_a_note_still_log():
    # A note that merely mentions "complete"/"done" must be logged, not advanced:
    # log_entry is matched before next_step, so the leading verb wins.
    cmd = route("Note that the reaction is complete")
    assert cmd.intent == "log_entry"
    assert "reaction is complete" in cmd.log_text

    cmd = route("Record that step two is done")
    assert cmd.intent == "log_entry"
    assert "step two is done" in cmd.log_text


def test_skip_routes_to_skip_step():
    assert route("Skip this step").intent == "skip_step"
    assert route("skip").intent == "skip_step"
    assert route("skip ahead").intent == "skip_step"


def test_unknown_has_clarify():
    cmd = deterministic_route("blah blah nonsense")
    assert cmd.intent == "unknown"
    assert cmd.clarify_prompt


def test_prev_step_routes():
    assert route("Go back").intent == "prev_step"
    assert route("Previous step").intent == "prev_step"


def test_repeat_step_routes():
    assert route("Repeat that").intent == "repeat_step"
    assert route("What step am I on?").intent == "repeat_step"


def test_undo_log_routes():
    assert route("Scratch that").intent == "undo_log"
    assert route("Delete the last note").intent == "undo_log"


def test_correct_log_routes_replacement_text():
    cmd = route("Change that to added 300 uL lysis buffer")
    assert cmd.intent == "correct_log"
    assert cmd.log_text == "added 300 uL lysis buffer"


def test_ask_routes_as_last_resort_question():
    cmd = route("How much lysis buffer in step 1?")
    assert cmd.intent == "ask"
    assert cmd.question == "How much lysis buffer in step 1?"


def test_broadened_log_phrasing_routes_to_log_entry():
    cmd = route("Make a note that the pellet looks white")
    assert cmd.intent == "log_entry"
    assert cmd.log_text == "the pellet looks white"

    cmd = route("Record: added 200 microliters to sample B")
    assert cmd.intent == "log_entry"
    assert cmd.sample_id == "B"
    assert "200 uL" in cmd.log_text

    cmd = route("Write down the supernatant is cloudy")
    assert cmd.intent == "log_entry"
    assert cmd.log_text == "the supernatant is cloudy"


def test_existing_next_step_still_wins_over_ask():
    assert route("What's next?").intent == "next_step"


def test_answer_question_fallback_matches_protocol_step(monkeypatch):
    from backend import router
    from backend.state import SessionState

    monkeypatch.setattr(router, "_llm_available", lambda: False)
    state = SessionState()
    state.load_files()
    protocol = state.find_protocol("DNA Extraction")
    assert protocol is not None

    answer = router.answer_question("How much lysis buffer in step 1?", protocol)

    assert "Step 1:" in answer
    assert "lysis buffer" in answer.lower()
