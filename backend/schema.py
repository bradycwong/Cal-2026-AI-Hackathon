"""Lab — the ONE Command schema and the locked WS event envelope.

This module is the data-flow boundary the whole product hangs off of. Two things
live here and nothing else:

1. ``Command`` — the single validated object every transcript becomes (flat-5 + ``unknown``).
2. The event-envelope builders — the *only* shapes the backend is allowed to push
   over ``/ws/events``. The frontend dispatches on 4 outer ``type``s; new command
   kinds add a ``kind`` value, never a new event type. (Locked invariant #5.)
"""

from __future__ import annotations

import time
from typing import Any, Literal, Optional

from pydantic import BaseModel

Intent = Literal[
    "load_protocol",
    "next_step",
    "prev_step",
    "repeat_step",
    "log_entry",
    "undo_log",
    "correct_log",
    "start_timer",
    "stop_timer",
    "find_inventory",
    "ask",
    "unknown",
]


class Command(BaseModel):
    """The locked Command shape. Every transcript -> exactly one of these.

    All payload fields are Optional so a clear-but-incomplete utterance
    (e.g. "load a protocol" with no name) is a *valid* Command, not a
    validation crash. The router leaves the field null / returns ``unknown``
    and puts a one-line question in ``clarify_prompt`` instead of guessing.
    """

    intent: Intent
    protocol_name: Optional[str] = None   # load_protocol
    log_text: Optional[str] = None        # log_entry  (-> note.text on persist)
    sample_id: Optional[str] = None       # log_entry
    duration_s: Optional[int] = None      # start_timer
    timer_label: Optional[str] = None     # start_timer
    reagent_name: Optional[str] = None    # find_inventory
    question: Optional[str] = None      # ask
    clarify_prompt: Optional[str] = None  # unknown / missing param -> clarification area


# --- Event envelope (locked) ------------------------------------------------
# { type, payload, ts }
# type in { transcript_update, command_result, timer_update, error }

EventType = Literal["transcript_update", "command_result", "timer_update", "error"]


def make_event(type: EventType, payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a payload in the locked envelope with a millisecond timestamp."""
    return {"type": type, "payload": payload, "ts": int(time.time() * 1000)}


# --- command_result kinds ---------------------------------------------------
# kind in { step_change, log_entry, log_removed, log_update, inventory_result,
#           clarify, voice_state, ask_result, timer_removed, protocol_imported,
#           reset }


def command_result(kind: str, **fields: Any) -> dict[str, Any]:
    return make_event("command_result", {"kind": kind, **fields})


def step_change_event(
    prev_step: Optional[dict[str, Any]],
    current_step: Optional[dict[str, Any]],
    next_step: Optional[dict[str, Any]],
    all_steps: Optional[list[dict[str, Any]]] = None,
    current_index: Optional[int] = None,
    protocol_name: Optional[str] = None,
    loaded: bool = False,
) -> dict[str, Any]:
    # ``loaded`` is True only when a protocol was just LOADED (not on step nav),
    # so the frontend can route to the active-protocol page without bouncing the
    # user around on every "next step".
    return command_result(
        "step_change",
        prev_step=prev_step,
        current_step=current_step,
        next_step=next_step,
        all_steps=all_steps if all_steps is not None else [],
        current_index=current_index,
        protocol_name=protocol_name,
        loaded=loaded,
    )


def log_entry_event(
    id: int,
    text: str,
    timestamp: str,
    sample_id: Optional[str],
    step_ref: Optional[int],
    category: Optional[str] = None,
    flag: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return command_result(
        "log_entry",
        id=id,
        text=text,
        timestamp=timestamp,
        sample_id=sample_id,
        step_ref=step_ref,
        category=category,
        flag=flag,
    )


def protocol_imported_event(
    name: str,
    protocol_id: str,
    step_count: int,
    aliases: list[str],
    load_hint: str,
) -> dict[str, Any]:
    return command_result(
        "protocol_imported",
        name=name,
        protocol_id=protocol_id,
        step_count=step_count,
        aliases=aliases,
        load_hint=load_hint,
    )


def log_removed_event(id: int) -> dict[str, Any]:
    return command_result("log_removed", id=id)


def log_update_event(
    id: int, text: str, flag: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    return command_result("log_update", id=id, text=text, flag=flag)


def ask_result_event(question: str, answer: str) -> dict[str, Any]:
    return command_result("ask_result", question=question, answer=answer)


def inventory_result_event(
    name: str, location: str, quantity_approx: str
) -> dict[str, Any]:
    return command_result(
        "inventory_result",
        name=name,
        location=location,
        quantity_approx=quantity_approx,
    )


def clarify_event(message: str) -> dict[str, Any]:
    """Never-fail-silently: anything ambiguous renders the clarification area."""
    return command_result("clarify", message=message)


def voice_state_event(muted: bool, label: str) -> dict[str, Any]:
    return command_result("voice_state", muted=muted, label=label)


def reset_event(notes_cleared: bool) -> dict[str, Any]:
    return command_result("reset", notes_cleared=notes_cleared)


def notebook_list_event(
    notebooks: list[dict[str, Any]], active_id: int
) -> dict[str, Any]:
    """Notebooks changed (created/selected): clients refresh the list + active
    feed. Additive ``kind`` on the locked command_result envelope."""
    return command_result("notebook_list", notebooks=notebooks, active_id=active_id)


def timer_update_event(
    timer_id: str, label: str, remaining_s: int, expired: bool, paused: bool = False
) -> dict[str, Any]:
    return make_event(
        "timer_update",
        {
            "timer_id": timer_id,
            "label": label,
            "remaining_s": remaining_s,
            "expired": expired,
            "paused": paused,
        },
    )


def timer_removed_event(timer_id: str) -> dict[str, Any]:
    """A timer was stopped early / dismissed: drop its card and silence its alarm."""
    return command_result("timer_removed", timer_id=timer_id)


def transcript_update_event(text: str, is_final: bool) -> dict[str, Any]:
    return make_event("transcript_update", {"text": text, "is_final": is_final})


def error_event(code: str, message: str, source: str) -> dict[str, Any]:
    return make_event("error", {"code": code, "message": message, "source": source})
