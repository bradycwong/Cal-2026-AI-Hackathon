"""handlers.py — deterministic dispatch. The LLM never reaches here.

``handle_command(cmd, state)`` takes a validated Command + the SessionState,
mutates state, and returns a list of locked event envelopes. Missing/empty
required params -> a ``clarify`` event (never fail silently, never act on a guess).
This is also where Command field names are translated to persistence field names
(e.g. ``Command.log_text -> log_entry.text``).
"""

from __future__ import annotations

import difflib
import os
from typing import Any

from .schema import (
    Command,
    clarify_event,
    inventory_result_event,
    log_entry_event,
    step_change_event,
    timer_update_event,
)
from .state import SessionState

# Off by default: timed steps wait for an explicit "start timer" command rather
# than starting their countdown the moment they become current.
AUTO_TIMERS = os.getenv("LAB_AUTO_TIMERS", "false").lower() in {"true", "1", "yes"}


def _step_change_events(state: SessionState) -> list[dict[str, Any]]:
    """step_change for the new cursor; an auto-timer too only if AUTO_TIMERS is on."""
    idx = state.current_step_index
    prev = state.step_at(idx - 1)
    cur = state.step_at(idx)
    nxt = state.step_at(idx + 1)
    events: list[dict[str, Any]] = [
        step_change_event(
            prev_step=prev.as_event() if prev else None,
            current_step=cur.as_event() if cur else None,
            next_step=nxt.as_event() if nxt else None,
        )
    ]
    if AUTO_TIMERS and cur and cur.duration_s:
        label = cur.timer_label or f"step {cur.id}"
        timer = state.add_timer(cur.duration_s, label)
        events.append(
            timer_update_event(timer.timer_id, timer.label, timer.remaining_s(), expired=False)
        )
    return events


def _available_protocols(state: SessionState) -> str:
    return ", ".join(p.name for p in state.protocols.values()) or "none loaded"


def _handle_load_protocol(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    if not cmd.protocol_name:
        # State is authoritative for what's loadable; don't trust a stale prompt.
        return [clarify_event(f"Which protocol would you like to load? (Available: {_available_protocols(state)})")]
    proto = state.find_protocol(cmd.protocol_name)
    if proto is None:
        return [clarify_event(f"I don't have a protocol called '{cmd.protocol_name}'. Available: {_available_protocols(state)}.")]
    state.active_protocol = proto
    state.current_step_index = 0
    state.clear_timers()
    return _step_change_events(state)


def _handle_next_step(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    if not state.active_protocol:
        return [clarify_event("No protocol is loaded. Say 'load DNA extraction protocol' first.")]
    last = len(state.active_protocol.steps) - 1
    if state.current_step_index >= last:
        return [clarify_event(f"You're on the last step of {state.active_protocol.name}.")]
    state.current_step_index += 1
    return _step_change_events(state)


def _handle_log_entry(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    if not cmd.log_text:
        msg = cmd.clarify_prompt or "What would you like to log?"
        return [clarify_event(msg)]
    entry = state.append_log(cmd.log_text, cmd.sample_id)  # log_text -> note.text
    return [log_entry_event(**entry)]


def _handle_start_timer(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    duration_s = cmd.duration_s
    label = cmd.timer_label
    if not duration_s or duration_s <= 0:
        # No explicit duration -> start the current step's declared timer, if any.
        cur = state.step_at(state.current_step_index) if state.active_protocol else None
        if cur and cur.duration_s:
            duration_s = cur.duration_s
            label = label or cur.timer_label or f"step {cur.id}"
        else:
            msg = cmd.clarify_prompt or "How long should the timer run? (e.g. 'start a 10-minute timer')"
            return [clarify_event(msg)]
    timer = state.add_timer(duration_s, label or "timer")
    return [timer_update_event(timer.timer_id, timer.label, timer.remaining_s(), expired=False)]


def _handle_find_inventory(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    if not cmd.reagent_name:
        msg = cmd.clarify_prompt or "Which reagent are you looking for?"
        return [clarify_event(msg)]
    names = [item.name for item in state.inventory]
    query = cmd.reagent_name.strip()
    matches = difflib.get_close_matches(query.lower(), [n.lower() for n in names], n=1, cutoff=0.6)
    if not matches:
        # substring fallback before giving up
        for item in state.inventory:
            if query.lower() in item.name.lower():
                return [inventory_result_event(item.name, item.location, item.quantity_approx)]
        return [clarify_event(f"I don't have a record for {cmd.reagent_name}.")]
    matched_name = matches[0]
    item = next(i for i in state.inventory if i.name.lower() == matched_name)
    return [inventory_result_event(item.name, item.location, item.quantity_approx)]


def _handle_unknown(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    msg = cmd.clarify_prompt or "Sorry, I didn't understand that."
    return [clarify_event(msg)]


_DISPATCH = {
    "load_protocol": _handle_load_protocol,
    "next_step": _handle_next_step,
    "log_entry": _handle_log_entry,
    "start_timer": _handle_start_timer,
    "find_inventory": _handle_find_inventory,
    "unknown": _handle_unknown,
}


def handle_command(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    """Deterministic dispatch on ``cmd.intent``. Always returns >=1 event."""
    handler = _DISPATCH.get(cmd.intent, _handle_unknown)
    return handler(cmd, state)
