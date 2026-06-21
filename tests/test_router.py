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


def test_find_inventory():
    cmd = route("Where's the proteinase K?")
    assert cmd.intent == "find_inventory"
    assert "proteinase" in cmd.reagent_name.lower()


def test_next_step():
    assert route("What's next?").intent == "next_step"
    assert route("Next step").intent == "next_step"


def test_unknown_has_clarify():
    cmd = deterministic_route("blah blah nonsense")
    assert cmd.intent == "unknown"
    assert cmd.clarify_prompt
