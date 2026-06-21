"""Typed mute/unmute: the command box toggles the same shared mic gate."""

import os

os.environ["LAB_DB_PATH"] = ":memory:"  # set before importing the app module

import asyncio

import backend.main as main
from backend.state import SessionState
from backend.voice_control import VoiceControl


def setup_function(_func) -> None:
    # Fresh, isolated process-wide state for each test.
    main.state = SessionState(db_path=":memory:")
    main.state.load_files()
    main.voice = VoiceControl()


def _run(coro):
    return asyncio.run(coro)


def test_typed_mute_toggles_shared_gate():
    events = _run(main.ingest("mute"))
    assert main.voice.muted is True
    assert len(events) == 1
    p = events[0]["payload"]
    assert events[0]["type"] == "command_result"
    assert p["kind"] == "voice_state"
    assert p["muted"] is True
    assert p["label"] == "muted"

    events = _run(main.ingest("unmute"))
    assert main.voice.muted is False
    assert events[0]["payload"]["muted"] is False
    assert events[0]["payload"]["label"] == "listening"


def test_typed_mute_is_not_routed_as_a_lab_command():
    events = _run(main.ingest("mute"))
    # No transcript echo and no clarify/unknown — purely a control toggle.
    assert all(e["payload"].get("kind") != "clarify" for e in events)
    assert main.state.active_protocol is None


def test_typed_commands_still_work_while_muted():
    # Muting silences the mic; the explicit typed channel keeps working.
    _run(main.ingest("mute"))
    assert main.voice.muted is True
    events = _run(main.ingest("Load DNA extraction protocol"))
    assert main.state.active_protocol is not None
    assert any(
        e["type"] == "command_result" and e["payload"].get("kind") == "step_change"
        for e in events
    )


def test_control_variants_via_typed_channel():
    assert _run(main.ingest("stop listening"))[0]["payload"]["muted"] is True
    assert main.voice.muted is True
    assert _run(main.ingest("start listening"))[0]["payload"]["muted"] is False
    assert main.voice.muted is False
