"""Voice control gate checks: always-listening plus mute/unmute."""

from backend.voice_control import (
    VoiceControl,
    classify_control,
    wants_mute,
    wants_unmute,
)


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


def test_muted_speech_is_shown_for_debug_but_not_routed():
    # Muting gates the command spine, but the transcript is still surfaced so it
    # stays visible for debugging. The utterance is shown, never acted on.
    vc = VoiceControl()
    vc.set_muted(True)

    decision = vc.process_final("Where's the proteinase K?")

    assert decision.report_transcript
    assert not decision.route_command
    assert not decision.voice_state_changed
    # command_text is the cleaned utterance (trailing "?" stripped), same as the
    # normal unmuted path.
    assert decision.command_text == "Where's the proteinase K"


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
    assert classify_control("") is None
    assert classify_control("   ") is None


def test_mute_matches_loosely_anywhere_in_the_phrase():
    # Per user request: "mute" found anywhere mutes, not just as a bare phrase.
    for phrase in ("mute the sample", "okay mute please", "can you mute now"):
        assert classify_control(phrase) == "mute", phrase
    vc = VoiceControl()
    decision = vc.process_final("okay mute the lab please")
    assert vc.muted
    assert decision.voice_state_changed
    assert not decision.route_command


def test_wants_mute_does_not_fire_inside_unmute_or_commute():
    assert wants_mute("okay mute")
    assert not wants_mute("please unmute now")  # no \bmute\b inside "unmute"
    assert not wants_mute("start the commute")
    # Unmute still wins when both could appear together.
    assert classify_control("un mute") == "unmute"


def test_unmute_tolerates_common_stt_variants():
    for phrase in ("un mute", "un-mute", "unmuted", "Unmute lab", "resume listening"):
        assert classify_control(phrase) == "unmute", phrase


def test_mute_tolerates_common_stt_variants():
    for phrase in ("mute", "muted", "Mute lab", "stop listening"):
        assert classify_control(phrase) == "mute", phrase


def test_muted_mic_keeps_listening_and_only_unmute_resumes():
    """While muted, every non-unmute final is shown (for debugging) but never
    routed, and a spoken unmute (incl. an STT variant) resumes normal operation."""
    vc = VoiceControl(muted=True)

    for noise in ("where is the proteinase k", "next step", "log a note"):
        decision = vc.process_final(noise)
        assert not decision.route_command  # never acts while muted
        assert decision.report_transcript  # but stays visible for debugging
        assert vc.muted  # still listening, still muted

    decision = vc.process_final("un mute")
    assert not vc.muted
    assert decision.voice_state_changed
    assert vc.process_final("next step").route_command  # normal operation resumed


def test_muted_final_resumes_when_unmute_is_embedded_in_a_phrase():
    # STT can bundle the resume word into a longer utterance; still resume.
    vc = VoiceControl(muted=True)
    decision = vc.process_final("okay unmute please")
    assert not vc.muted
    assert decision.voice_state_changed


def test_loose_unmute_only_applies_while_muted_not_for_muting():
    # "unmute the rest" must NOT mute while listening (loose match is unmute-only).
    vc = VoiceControl()
    decision = vc.process_final("unmute the rest of the buffer")
    assert not vc.muted  # still listening
    assert decision.route_command  # treated as a normal command


def test_wants_unmute_is_unmute_only():
    assert wants_unmute("okay unmute now")
    assert wants_unmute("please start listening again")
    assert not wants_unmute("add 200 microliters")
    assert not wants_unmute("mute the music")


def test_process_interim_resumes_on_interim_unmute_only_while_muted():
    vc = VoiceControl(muted=True)
    assert vc.process_interim("uh") is None  # partial, no resume word yet
    assert vc.muted
    state = vc.process_interim("uh unmute")
    assert state is not None and state.muted is False
    assert not vc.muted

    # When already listening, interims never toggle state.
    vc2 = VoiceControl()
    assert vc2.process_interim("unmute") is None
    assert not vc2.muted
