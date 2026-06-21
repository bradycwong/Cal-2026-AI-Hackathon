"""Voice control gate checks: always-listening plus mute/unmute."""

from backend.voice_control import VoiceControl, classify_control


def test_normal_speech_reports_and_routes_while_unmuted():
    vc = VoiceControl()

    assert vc.should_report_interim()
    decision = vc.process_final("Load DNA extraction protocol")

    assert decision.report_transcript
    assert decision.route_command
    assert decision.command_text == "Load DNA extraction protocol"
    assert not decision.voice_state_changed
    assert not decision.muted


def test_voice_mute_changes_state_without_reporting_or_routing():
    vc = VoiceControl()

    decision = vc.process_final("mute")

    assert vc.muted
    assert not vc.should_report_interim()
    assert not decision.report_transcript
    assert not decision.route_command
    assert decision.voice_state_changed
    assert decision.muted
    assert decision.label == "muted"


def test_muted_speech_is_ignored_completely():
    vc = VoiceControl()
    vc.set_muted(True)

    decision = vc.process_final("Where's the proteinase K?")

    assert not decision.report_transcript
    assert not decision.route_command
    assert not decision.voice_state_changed
    assert decision.command_text == ""


def test_voice_unmute_resumes_without_reporting_or_routing_control_word():
    vc = VoiceControl(muted=True)

    decision = vc.process_final("unmute")

    assert not vc.muted
    assert vc.should_report_interim()
    assert not decision.report_transcript
    assert not decision.route_command
    assert decision.voice_state_changed
    assert not decision.muted
    assert decision.label == "listening"


def test_button_control_sets_muted_state_directly():
    vc = VoiceControl()

    muted = vc.set_muted(True)
    unmuted = vc.set_muted(False)

    assert muted.muted is True
    assert muted.label == "muted"
    assert unmuted.muted is False
    assert unmuted.label == "listening"


def test_control_phrase_variants_are_recognized():
    vc = VoiceControl()
    assert vc.process_final("stop listening").voice_state_changed
    assert vc.muted
    assert vc.process_final("start listening").voice_state_changed
    assert not vc.muted


def test_classify_control_detects_mute_and_unmute():
    assert classify_control("mute") == "mute"
    assert classify_control("Please mute.") == "mute"
    assert classify_control("stop listening") == "mute"
    assert classify_control("unmute") == "unmute"
    assert classify_control("Unmute!") == "unmute"
    assert classify_control("start listening") == "unmute"


def test_classify_control_ignores_non_control_text():
    assert classify_control("Load DNA extraction protocol") is None
    assert classify_control("mute the sample") is None  # not a bare control phrase
    assert classify_control("") is None
    assert classify_control("   ") is None
