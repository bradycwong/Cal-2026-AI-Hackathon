"""Wake-gate checks — wake word + follow-up window behaviour."""

from backend.wake import WakeConfig, WakeGate


def test_wake_plus_command_routes():
    g = WakeGate()
    d = g.process("Hey Lab, what's next?", now=100.0)
    assert d.should_route
    assert d.command_text == "what's next?"
    assert not d.just_woke


def test_bare_wake_opens_window_no_route():
    g = WakeGate()
    d = g.process("Hey Lab.", now=100.0)
    assert not d.should_route
    assert d.just_woke


def test_followup_inside_window_routes():
    g = WakeGate()
    g.process("Lab", now=100.0)  # opens window
    d = g.process("start a 10 minute timer", now=104.0)
    assert d.should_route
    assert d.command_text == "start a 10 minute timer"


def test_unaddressed_speech_ignored():
    g = WakeGate()
    d = g.process("can you pass me the pipette", now=100.0)
    assert not d.should_route
    assert not d.just_woke


def test_window_expires():
    g = WakeGate()
    g.process("lab", now=100.0)
    d = g.process("what's next?", now=200.0)  # long after window
    assert not d.should_route


def test_comma_after_greeting():
    # STT punctuates as "Hey, Lab. <command>" — must still match.
    g = WakeGate()
    d = g.process("Hey, Lab. Load the DNA extraction protocol.", now=1.0)
    assert d.should_route
    assert d.command_text == "Load the DNA extraction protocol."


def test_short_vowel_homophone_is_accepted():
    # STT can slightly mangle the wake word — must still wake.
    g = WakeGate()
    d = g.process("Hey labb, what's next?", now=1.0)
    assert d.should_route
    assert d.command_text == "what's next?"


def test_bare_lab_prefix():
    g = WakeGate()
    d = g.process("Lab load DNA extraction protocol", now=1.0)
    assert d.should_route
    assert d.command_text == "load DNA extraction protocol"


def test_runtime_word_change_drops_old_word():
    cfg = WakeConfig()  # defaults to "lab"
    cfg.update(word="jarvis")
    g = WakeGate(cfg)
    # old word (and its homophones) no longer wakes once the word changed.
    # use far-apart timestamps so no follow-up window is open.
    assert not g.process("Lab, what's next?", now=100.0).should_route
    assert not g.process("labb, what's next?", now=200.0).should_route
    # new word still works
    assert g.process("Hey Jarvis, what's next?", now=300.0).should_route


def test_custom_word_keeps_its_homophones():
    cfg = WakeConfig()
    cfg.update(word="jarvis")
    g = WakeGate(cfg)
    assert g.process("Hey jervis, start a 10 minute timer", now=1.0).should_route
