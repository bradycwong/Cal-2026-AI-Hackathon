"""handlers.py — deterministic dispatch. The LLM never reaches here.

``handle_command(cmd, state)`` takes a validated Command + the SessionState,
mutates state, and returns a list of locked event envelopes. Missing/empty
required params -> a ``clarify`` event (never fail silently, never act on a guess).
This is also where Command field names are translated to persistence field names
(e.g. ``Command.log_text -> log_entry.text``).
"""

from __future__ import annotations

import difflib
from typing import Any

from . import router
from .reproducibility import check as check_reproducibility
from .schema import (
    Command,
    ask_result_event,
    clarify_event,
    inventory_added_event,
    inventory_result_event,
    log_entry_event,
    log_removed_event,
    log_update_event,
    step_change_event,
    timer_removed_event,
    timer_update_event,
)
from .state import SessionState


def _step_change_events(
    state: SessionState,
    auto_timer: bool = True,
    loaded: bool = False,
    finished: bool = False,
) -> list[dict[str, Any]]:
    """step_change for the new cursor, plus a PAUSED timer card if the step is timed."""
    idx = state.current_step_index
    prev = state.step_at(idx - 1)
    cur = state.step_at(idx)
    nxt = state.step_at(idx + 1)
    proto = state.active_protocol
    all_steps = [s.as_event() for s in proto.steps] if proto else []
    current_index = state.current_step_index if proto else None
    protocol_name = proto.name if proto else None
    events: list[dict[str, Any]] = [
        step_change_event(
            prev_step=prev.as_event() if prev else None,
            current_step=cur.as_event() if cur else None,
            next_step=nxt.as_event() if nxt else None,
            all_steps=all_steps,
            current_index=current_index,
            protocol_name=protocol_name,
            loaded=loaded,
            finished=finished,
        )
    ]
    if auto_timer and not finished and cur and cur.duration_s:
        label = cur.timer_label or f"step {cur.id}"
        # Timed steps always arrive PAUSED (frozen at full duration) and wait for
        # an explicit "start timer" — they never auto-count-down on step change.
        timer = state.add_timer(cur.duration_s, label, paused=True)
        events.append(
            timer_update_event(
                timer.timer_id, timer.label, timer.remaining_s(),
                expired=False, paused=timer.paused,
            )
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
    state.protocol_complete = False
    state.clear_timers()
    return _step_change_events(state, loaded=True)


def _handle_next_step(
    cmd: Command, state: SessionState, *, log_event: bool = True
) -> list[dict[str, Any]]:
    if not state.active_protocol:
        return [clarify_event("No protocol is loaded. Say 'load DNA extraction protocol' first.")]
    last = len(state.active_protocol.steps) - 1
    if state.current_step_index >= last:
        # Final step: completing it FINISHES the protocol instead of dead-ending.
        # The cursor stays clamped to the last real step; we just flip the
        # protocol_complete flag and emit step_change(finished=True).
        state.current_step_index = last
        if state.protocol_complete:
            # Idempotent: a repeated "next step" after finishing changes nothing
            # and must not duplicate the final log entry.
            return _step_change_events(state, auto_timer=False, finished=True)
        events: list[dict[str, Any]] = []
        if log_event:
            completed = state.current_step()
            if completed is not None:
                category = state.active_protocol.name
                entry = state.append_log(
                    f"Completed step {completed.id}: {completed.text}", None, category, None
                )
                events.append(log_entry_event(**entry, step_log=True))
        state.protocol_complete = True
        events.extend(_step_change_events(state, auto_timer=False, finished=True))
        return events
    # Record completing the step we're leaving BEFORE advancing, so the note's
    # step_ref points at the finished step (and lands in the active notebook).
    # The "skip" button advances with log_event=False to move on without a note.
    events = []
    if log_event:
        completed = state.current_step()
        if completed is not None:
            category = state.active_protocol.name if state.active_protocol else None
            entry = state.append_log(
                f"Completed step {completed.id}: {completed.text}", None, category, None
            )
            # step_log=True so the frontend logs it without leaving the guide page.
            events.append(log_entry_event(**entry, step_log=True))
    state.current_step_index += 1
    state.protocol_complete = False
    events.extend(_step_change_events(state))
    return events


def advance_step(state: SessionState, *, log_event: bool = True) -> list[dict[str, Any]]:
    """Public entrypoint for the Confirm/Skip buttons to advance one step.

    Mirrors the voice/typed ``next_step`` command; ``log_event`` is False for the
    Skip button so the step advances without writing a note to the notebook.
    """
    return _handle_next_step(Command(intent="next_step"), state, log_event=log_event)


def _handle_prev_step(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    if not state.active_protocol:
        return [clarify_event("No protocol is loaded. Say 'load DNA extraction protocol' first.")]
    if state.current_step_index <= 0:
        return [clarify_event(f"You're on the first step of {state.active_protocol.name}.")]
    state.current_step_index -= 1
    state.protocol_complete = False
    return _step_change_events(state, auto_timer=False)


def _handle_repeat_step(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    if not state.active_protocol:
        return [clarify_event("No protocol is loaded. Say 'load DNA extraction protocol' first.")]
    state.protocol_complete = False
    return _step_change_events(state, auto_timer=False)


def _step_params_for_ref(state: SessionState, step_ref: Any) -> dict[str, Any]:
    """Parameters of the step a note was logged at (per-protocol id, 1..N)."""
    proto = state.active_protocol
    if not proto or step_ref is None:
        return {}
    for step in proto.steps:
        if step.id == step_ref:
            return step.parameters
    return {}


def _handle_log_entry(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    if not cmd.log_text:
        msg = cmd.clarify_prompt or "What would you like to log?"
        return [clarify_event(msg)]
    category = state.active_protocol.name if state.active_protocol else None
    step = state.current_step()
    flag = check_reproducibility(step.parameters, cmd.log_text) if step else None
    entry = state.append_log(cmd.log_text, cmd.sample_id, category, flag)  # log_text -> note.text
    return [log_entry_event(**entry)]


def _handle_undo_log(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    entry = state.pop_log()
    if entry is None:
        return [clarify_event("There's nothing to undo.")]
    return [log_removed_event(int(entry["id"]))]


def _handle_correct_log(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    if not cmd.log_text:
        return [clarify_event("What should I change the last note to?")]
    # Recompute the flag against the step the note was ORIGINALLY logged at, not
    # the current cursor (the researcher may have advanced since logging).
    original_step_ref = state.log[-1].get("step_ref") if state.log else None
    params = _step_params_for_ref(state, original_step_ref)
    flag = check_reproducibility(params, cmd.log_text)
    entry = state.update_last_log(cmd.log_text, flag)
    if entry is None:
        return [clarify_event("There's nothing to correct.")]
    return [log_update_event(int(entry["id"]), str(entry["text"]), entry.get("flag"))]


def _handle_start_timer(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    duration_s = cmd.duration_s
    label = cmd.timer_label
    if not duration_s or duration_s <= 0:
        # No explicit duration -> resume the step's paused timer if one is waiting.
        pending = state.start_pending_timer()
        if pending is not None:
            return [timer_update_event(pending.timer_id, pending.label, pending.remaining_s(), expired=False, paused=False)]
        # Otherwise start the current step's declared timer fresh, if any.
        cur = state.step_at(state.current_step_index) if state.active_protocol else None
        if cur and cur.duration_s:
            duration_s = cur.duration_s
            label = label or cur.timer_label or f"step {cur.id}"
        else:
            msg = cmd.clarify_prompt or "How long should the timer run? (e.g. 'start a 10-minute timer')"
            return [clarify_event(msg)]
    timer = state.add_timer(duration_s, label or "timer")
    return [timer_update_event(timer.timer_id, timer.label, timer.remaining_s(), expired=False, paused=False)]


def _handle_stop_timer(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    """Cancel every active timer (and thereby silence any alarm). Same gate a
    clicked timer "x" hits, just for all of them at once."""
    ids = state.remove_all_timers()
    if not ids:
        return [clarify_event("There are no active timers to stop.")]
    return [timer_removed_event(tid) for tid in ids]


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


def _handle_add_inventory(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    """Add a reagent to inventory from a voice/typed command.

    The NAME is required: with no name we add nothing and clarify. Per the
    product rule, any other missing field defaults to "TBD" (amount/unit/
    location) and a missing expiration defaults to "N/A" — never a guess.
    """
    name = (cmd.reagent_name or "").strip()
    if not name:
        msg = cmd.clarify_prompt or "What's the name of the reagent to add to the inventory?"
        return [clarify_event(msg)]
    amount = (cmd.amount or "").strip() or "TBD"
    unit = (cmd.unit or "").strip() or "TBD"
    location = (cmd.location or "").strip() or "TBD"
    expiration = (cmd.expiration or "").strip() or "N/A"
    item = state.add_inventory_item(
        name,
        location=location,
        expiration=expiration,
        amount=amount,
        unit=unit,
    )
    return [
        inventory_added_event(
            item.name, item.amount, item.unit, item.location, item.expiration
        )
    ]


def _handle_ask(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    if not cmd.question:
        return [clarify_event("What would you like to ask about the protocol?")]
    if not state.active_protocol:
        return [clarify_event("Load a protocol first, then ask about it.")]
    answer = router.answer_question(cmd.question, state.active_protocol)
    return [ask_result_event(cmd.question, answer)]


def _handle_unknown(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    msg = cmd.clarify_prompt or "Sorry, I didn't understand that."
    return [clarify_event(msg)]


_DISPATCH = {
    "load_protocol": _handle_load_protocol,
    "next_step": _handle_next_step,
    "prev_step": _handle_prev_step,
    "repeat_step": _handle_repeat_step,
    "log_entry": _handle_log_entry,
    "undo_log": _handle_undo_log,
    "correct_log": _handle_correct_log,
    "start_timer": _handle_start_timer,
    "stop_timer": _handle_stop_timer,
    "find_inventory": _handle_find_inventory,
    "add_inventory": _handle_add_inventory,
    "ask": _handle_ask,
    "unknown": _handle_unknown,
}


def handle_command(cmd: Command, state: SessionState) -> list[dict[str, Any]]:
    """Deterministic dispatch on ``cmd.intent``. Always returns >=1 event."""
    handler = _DISPATCH.get(cmd.intent, _handle_unknown)
    return handler(cmd, state)
