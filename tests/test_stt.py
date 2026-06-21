"""STT proxy — pure message interpretation + flush/dedup logic.

These cover the parts of the Deepgram bridge that don't need a live socket:
how raw result messages map to actions, and how segments accumulate, flush,
and dedup into a single utterance routed through ingest().
"""

import asyncio

from backend.deepgram_stt import _flush, handle_browser_control_message, interpret_message


def _results(text, *, is_final=False, speech_final=False):
    return {
        "type": "Results",
        "is_final": is_final,
        "speech_final": speech_final,
        "channel": {"alternatives": [{"transcript": text}]},
    }


def test_interim_is_interim():
    assert interpret_message(_results("load dna")) == ("interim", "load dna")


def test_is_final_accumulates_without_flush():
    assert interpret_message(_results("load dna", is_final=True)) == ("segment", "load dna")


def test_speech_final_flushes():
    msg = _results("load dna", is_final=True, speech_final=True)
    assert interpret_message(msg) == ("segment_flush", "load dna")


def test_utterance_end_flushes():
    assert interpret_message({"type": "UtteranceEnd"}) == ("flush", "")


def test_empty_and_unknown_are_ignored():
    assert interpret_message(_results("   ")) == ("ignore", "")
    assert interpret_message({"type": "Metadata"}) == ("ignore", "")
    assert interpret_message({}) == ("ignore", "")


def _run(coro):
    return asyncio.run(coro)


def test_flush_joins_segments_and_clears():
    finals = []
    segments = ["load the", "dna protocol"]
    last = [""]
    _run(_flush(segments, last, lambda t: _collect(finals, t)))
    assert finals == ["load the dna protocol"]
    assert segments == []          # cleared after flush
    assert last[0] == "load the dna protocol"


def test_flush_dedups_consecutive_identical_finals():
    finals = []
    last = [""]
    _run(_flush(["whats next"], last, lambda t: _collect(finals, t)))
    _run(_flush(["whats next"], last, lambda t: _collect(finals, t)))  # duplicate -> dropped
    _run(_flush(["next step"], last, lambda t: _collect(finals, t)))
    assert finals == ["whats next", "next step"]


def test_flush_noop_on_empty():
    finals = []
    _run(_flush([], [""], lambda t: _collect(finals, t)))
    assert finals == []


async def _collect(sink, text):
    sink.append(text)


def test_set_muted_control_invokes_callback_without_stopping():
    controls = []

    should_stop = _run(
        handle_browser_control_message(
            '{"type":"set_muted","muted":true}',
            lambda ctrl: _collect(controls, ctrl),
        )
    )

    assert should_stop is False
    assert controls == [{"type": "set_muted", "muted": True}]


def test_stop_control_stops_without_invoking_callback():
    controls = []

    should_stop = _run(
        handle_browser_control_message(
            '{"type":"stop"}',
            lambda ctrl: _collect(controls, ctrl),
        )
    )

    assert should_stop is True
    assert controls == []
