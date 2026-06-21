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


def test_cancel_protocol_variants_route_to_cancel():
    for phrase in (
        "Cancel protocol",
        "cancel the protocol",
        "stop the protocol",
        "stop protocol",
        "abort the protocol",
        "stop the run",
    ):
        assert route(phrase).intent == "cancel_protocol", phrase


def test_cancel_protocol_does_not_collide_with_load_or_timers():
    # "stop/cancel the protocol" must NOT be misread as a load (the bare
    # "<name> protocol" fallback) nor as a timer command.
    assert route("stop the protocol").intent == "cancel_protocol"
    assert route("cancel the protocol").intent == "cancel_protocol"
    # timer commands still win when the object is a timer
    assert route("stop the timer").intent == "stop_timer"
    assert route("stop all timers").intent == "stop_timer"
    # a genuine protocol load is unaffected
    assert route("load DNA extraction protocol").intent == "load_protocol"


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
    # Imperative advance phrasings still mutate. (The question "What's next?" now
    # routes to `ask` — see test_whats_next_is_a_safe_question.)
    assert route("Next step").intent == "next_step"
    assert route("Next").intent == "next_step"


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
        "jump to guide",
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


def test_navigate_page_routes():
    # Hands-free "go to <page>" for the standalone pages. Each phrase routes to
    # navigate_page with the matching page key (the guide itself stays show_protocol).
    cases = {
        "dashboard": ["go to the dashboard", "open dashboard", "show me the dashboard",
                      "go home", "home"],
        "protocols": ["go to protocols", "open the protocol library", "show protocols",
                      "protocol library"],
        "notebook": ["go to the notebook", "open the notebook", "show me the notebook",
                     "open my notes"],
        "inventory": ["go to inventory", "open the inventory", "show inventory"],
        "commands": ["go to the commands page", "open commands", "show commands",
                     "help", "what can I say?"],
    }
    for page, phrases in cases.items():
        for phrase in phrases:
            cmd = route(phrase)
            assert cmd.intent == "navigate_page", f"{phrase!r} -> {cmd.intent}"
            assert cmd.page == page, f"{phrase!r} -> page={cmd.page!r}"


def test_navigate_page_does_not_swallow_guide_inventory_or_load():
    # The new nav intent must not cannibalise neighbouring intents.
    assert route("go to the guide").intent == "show_protocol"
    assert route("back to the run").intent == "show_protocol"
    assert route("show me the current step").intent == "show_protocol"
    assert route("what step am I on?").intent == "show_protocol"
    assert route("where is the EDTA?").intent == "find_inventory"
    assert route("find Tris").intent == "find_inventory"
    assert route("load DNA extraction protocol").intent == "load_protocol"


def test_unknown_fallback_points_to_commands_page():
    cmd = route("flibbertigibbet zorp")
    assert cmd.intent == "unknown"
    assert "didn't understand" in cmd.clarify_prompt  # keep test_aliases substring contract
    assert "Commands page" in cmd.clarify_prompt


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


def test_whats_next_is_a_safe_question():
    # Broad question guard: an interrogative never mutates. "What's next?"
    # previews the next step (routes to `ask`) instead of advancing.
    assert route("What's next?").intent == "ask"


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


# --- question safety: a question must never mutate protocol state -------------


def test_questions_never_trigger_a_mutation():
    # A question-shaped utterance routes to `ask`, never to a state mutation
    # (advance/skip/timer/cancel/load), even when it contains a control keyword.
    # Leading speech-filler ("uh,") is tolerated.
    for phrase in (
        "uh, what's next?",
        "what do I need for the next step?",
        "should I skip this step?",
        "can I stop the timer?",
        "do I need to start a timer?",
        "should I cancel the protocol?",
        "should I load the DNA extraction protocol?",
    ):
        cmd = route(phrase)
        assert cmd.intent == "ask", phrase
        assert cmd.question, phrase  # the user's words are preserved for the UI


def test_readonly_question_shortcuts_still_work():
    # Read-only intents are safe to trigger from a question, so these keep their
    # direct behavior instead of becoming a generic `ask`.
    assert route("where is the EDTA?").intent == "find_inventory"
    assert route("what step am I on?").intent == "show_protocol"
    assert route("what can I say?").intent == "navigate_page"


def test_imperatives_still_mutate():
    # Non-question commands are unaffected by the question guard.
    assert route("next step").intent == "next_step"
    assert route("done").intent == "next_step"
    assert route("skip").intent == "skip_step"
    assert route("stop timer").intent == "stop_timer"


def test_looks_like_question_detects_filler_led_questions():
    assert router._looks_like_question("uh, what's next") is True
    assert router._looks_like_question("should I skip this") is True
    assert router._looks_like_question("can we stop the timer") is True
    assert router._looks_like_question("next step") is False
    assert router._looks_like_question("log the temperature") is False


def test_clean_question_strips_filler_keeps_lab_terms():
    cleaned = router._clean_question("uh, um, how much lysis buffer do I need, please?")
    low = cleaned.lower()
    assert "uh" not in low.split()
    assert "um" not in low.split()
    assert "please" not in low
    assert "lysis buffer" in low

    kept = router._clean_question("so, add 200 microliters of proteinase K in 5 minutes")
    assert "200 uL" in kept
    assert "proteinase k" in kept.lower()
    assert "5 minutes" in kept


def test_answer_targets_relative_and_numbered_steps(monkeypatch):
    from backend.state import SessionState

    monkeypatch.setattr(router, "_llm_available", lambda: False)
    state = SessionState()
    state.load_files()
    protocol = state.find_protocol("DNA Extraction")
    assert protocol is not None

    # "next step" relative to the current cursor (index 0 -> step id 2)
    nxt = router.answer_question("what's in the next step?", protocol, current_step_index=0)
    assert nxt.startswith("Step 2:")
    # "this step" relative to the current cursor (index 2 -> step id 3)
    cur = router.answer_question("what is this step?", protocol, current_step_index=2)
    assert cur.startswith("Step 3:")
    # an explicit numbered step ignores the cursor
    s3 = router.answer_question("what reagent in step 3?", protocol, current_step_index=0)
    assert s3.startswith("Step 3:")


def test_step_context_humanizes_volume():
    from backend.state import Protocol, Step

    proto = Protocol(
        id="p",
        name="P",
        steps=[
            Step(id=1, text="Add buffer", parameters={"volume_ul": 50000, "reagent": "buffer"}),
            Step(id=2, text="Add water", parameters={"volume_ul": 200, "reagent": "water"}),
        ],
    )
    ctx = router._step_context(proto)
    # large volume humanized to mL; raw microliter number never shown to the model
    assert "volume=50 mL" in ctx
    assert "50000" not in ctx
    # sub-mL volume stays in uL
    assert "volume=200 uL" in ctx


# --- reagent prep modal voice control --------------------------------------


def test_set_sample_count_parses_phrasings():
    cases = {
        "set samples to 24": 24,
        "set the samples to 24": 24,
        "change samples to 12": 12,
        "change the sample count to 8": 8,
        "scale to 8 samples": 8,
        "scale for 16 samples": 16,
        "make it 6 samples": 6,
        "use 10 samples": 10,
        "run 30 samples": 30,
        "prep 18 samples": 18,
        "32 samples": 32,
    }
    for phrase, n in cases.items():
        cmd = route(phrase)
        assert cmd.intent == "set_sample_count", phrase
        assert cmd.sample_count == n, (phrase, cmd.sample_count)


def test_set_sample_count_spelled_out_number():
    cmd = route("set samples to twenty four")
    assert cmd.intent == "set_sample_count"
    assert cmd.sample_count == 24


def test_set_sample_count_no_number_clarifies_not_guesses():
    cmd = route("change the samples")
    assert cmd.intent == "set_sample_count"
    assert cmd.sample_count is None
    assert cmd.clarify_prompt  # a question, never a guessed count


def test_confirm_prep_phrasings_route_to_confirm_prep():
    for phrase in (
        "close prep",
        "close the prep",
        "looks good",
        "reagents look good",
        "prep looks good",
        "prep done",
        "ready to start",
    ):
        assert route(phrase).intent == "confirm_prep", phrase


def test_prep_commands_are_control_fast_path(monkeypatch):
    # Like the other control intents, prep commands resolve deterministically even
    # with the (normally dead) LLM seam live -> they never reach the sentinel.
    monkeypatch.setattr(router, "ROUTER_MODE", "llm")
    monkeypatch.setattr(router, "_llm_route", lambda t: Command(intent="unknown"))
    assert route("set samples to 24").intent == "set_sample_count"
    assert route("close prep").intent == "confirm_prep"


def test_prep_commands_do_not_steal_existing_intents():
    # "done"/completion still advances (the handler closes the prep when it's
    # open); naming a protocol still loads; a note that mentions samples is still
    # logged; a sample question still asks / finds.
    assert route("done").intent == "next_step"
    assert route("start DNA extraction protocol").intent == "load_protocol"
    assert route("log that we prepped 12 samples").intent == "log_entry"
    assert route("how many samples do we need?").intent == "ask"
    assert route("where are the samples?").intent == "find_inventory"
