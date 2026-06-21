"""Router harness — fast, no UI, no network. Forces the deterministic path.

Covers the five demo lines + the key negative case (load with no name must NOT
guess a protocol). Intentionally minimal per the hackathon directive.
"""

import os

os.environ["ROUTER_MODE"] = "deterministic"

import backend.router as router
from backend.router import deterministic_route, normalize_ascii, route
from backend.schema import Command


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


def test_start_timer_compound_durations():
    # Minutes + seconds in one utterance must sum, not truncate to the first part.
    cases = {
        "Start a 1 minute 30 timer.": 90,
        "Set a timer for a minute 30.": 90,
        "Start a one minute thirty second timer.": 90,
        "Start a 1 minute 30 second timer.": 90,
        "Set a timer for 1:30.": 90,
        "Start a 90 second timer.": 90,
        "Start a 1.5 minute timer.": 90,
        "Set a timer for two and a half minutes.": 150,
        "Start a timer for a minute and a half.": 90,
        "Set a timer for half a minute.": 30,
        "Start a half hour timer.": 1800,
        "Set a timer for an hour and a half.": 5400,
        "Start a twenty five minute timer.": 1500,
    }
    for phrase, expected in cases.items():
        cmd = route(phrase)
        assert cmd.intent == "start_timer", phrase
        assert cmd.duration_s == expected, (phrase, cmd.duration_s)


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


def test_noisy_timer_phrases_route_deterministically():
    # STT-like phrasings still land on the right control intent.
    starts = {
        "start the incubation timer now": "incubation",
        "begin timer": None,        # no real label word -> handler-default
        "start countdown": None,    # must NOT be read as load_protocol "countdown"
    }
    for phrase, label in starts.items():
        cmd = route(phrase)
        assert cmd.intent == "start_timer", phrase
        assert cmd.timer_label == label, (phrase, cmd.timer_label)
    for phrase in ("stop the alarm please", "cancel all timers", "silence beeping"):
        assert route(phrase).intent == "stop_timer", phrase


def test_clear_done_timers_phrasings_route_to_clear_done():
    for phrase in (
        "delete all done timers",
        "clear done timers",
        "clear finished timers",
        "remove expired timers",
        "delete completed timers",
        "clear the finished timers",
        "clear timers that are done",
        "delete the timers that finished",
        "dismiss done timers",
        "cancel all done timers",
    ):
        assert route(phrase).intent == "clear_done_timers", phrase


def test_clear_done_does_not_steal_plain_stop_or_start():
    # No done-qualifier -> must stay stop_timer / start_timer, never clear_done_timers.
    assert route("stop timer").intent == "stop_timer"
    assert route("cancel all timers").intent == "stop_timer"
    assert route("start a timer").intent == "start_timer"
    assert route("start a 5 minute timer").intent == "start_timer"


def test_clear_done_timers_is_control_fast_path(monkeypatch):
    # Like the other timer controls, it resolves deterministically even when the
    # (normally dead) LLM seam is "live" -> never reaches the sentinel.
    monkeypatch.setattr(router, "ROUTER_MODE", "llm")
    monkeypatch.setattr(router, "_llm_route", lambda t: Command(intent="unknown"))
    assert route("clear done timers").intent == "clear_done_timers"


def test_control_fast_path_beats_llm(monkeypatch):
    # Controls must resolve deterministically even with the LLM seam "live".
    # Stub the (normally dead) LLM with a sentinel so we can SEE if it ran.
    monkeypatch.setattr(router, "ROUTER_MODE", "llm")
    monkeypatch.setattr(router, "_llm_route", lambda t: Command(intent="unknown"))
    # Control intents bypass the LLM -> real intent, never the sentinel.
    assert route("start a 10 minute timer").intent == "start_timer"
    assert route("stop timer").intent == "stop_timer"
    assert route("next step").intent == "next_step"
    assert route("go back").intent == "prev_step"
    # A non-control command DOES traverse the LLM seam -> sentinel surfaces
    # (deterministic would have said load_protocol, proving the LLM ran).
    assert route("load DNA extraction protocol").intent == "unknown"


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
    assert route("Say that again").intent == "repeat_step"


def test_show_protocol_routes_navigation_phrases():
    # Hands-free "take me to the live run": these navigate to the guide view.
    # "what step am I on" now lands here (was repeat_step) so it brings the
    # operator to the running protocol instead of only re-rendering in place.
    for phrase in (
        "jump to run",
        "jump to the run",
        "go to the protocol",
        "go to the guide",
        "back to the guide",
        "take me back to the run",
        "show me the protocol",
        "what step am I on?",
        "what step are we on?",
    ):
        assert route(phrase).intent == "show_protocol", phrase


def test_show_protocol_does_not_swallow_neighbouring_intents():
    # Nearby control/load phrases must keep their own intents.
    assert route("go back").intent == "prev_step"
    assert route("next step").intent == "next_step"
    assert route("repeat that").intent == "repeat_step"
    assert route("load the protocol").intent == "load_protocol"  # not navigation


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
