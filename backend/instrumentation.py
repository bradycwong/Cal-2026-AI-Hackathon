"""instrumentation.py — Arize AX tracing (additive, server-run only).

Exports OpenInference spans to Arize via ``arize-otel``'s ``register()`` and
provides two manual span helpers:

  * ``chain_span`` — a CHAIN span over the ``ingest -> route -> handle_command``
    spine. Any LLM span opened inside it nests under it (shared OTel context).
  * ``llm_span`` — an LLM span around a raw Anthropic ``messages.create`` call
    (used by ``router.answer_question`` / the ``ask`` path).

Why manual LLM spans instead of the OpenInference Anthropic auto-instrumentor:
the published instrumentor requires ``anthropic >= 0.41`` (the 1.0.x line needs
>= 0.84), and the only version below that floor is transitively incompatible with
the modern OpenTelemetry/wrapt stack ``arize-otel`` pulls in. This project pins
``anthropic == 0.40.0``, so a small hand-rolled LLM span is the robust path that
keeps the reproducible build intact. (``messages.parse`` does not exist in 0.40.0
either, so the router/import LLM paths fall back deterministically and make no
API call to trace — only the ``ask`` path is a live Anthropic call.)

Design constraints (matching the rest of the backend):
  * Purely additive — never changes routing/handler behaviour.
  * Degrades to no-ops when OpenTelemetry is not installed, when Arize creds are
    absent, or when running under pytest — so the typed demo and the pinned test
    suite need no cloud credentials and incur no tracing overhead.
  * Reads creds from env ONLY (``ARIZE_SPACE_ID`` / ``ARIZE_API_KEY``); never
    embeds secret values. ``project_name`` is a non-secret constant.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Iterator, Optional, Sequence

PROJECT_NAME = "cal-2026-lab"

try:
    from opentelemetry import trace as _trace
    from openinference.semconv.trace import (
        OpenInferenceSpanKindValues,
        SpanAttributes,
    )

    _KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND
    _INPUT = SpanAttributes.INPUT_VALUE
    _OUTPUT = SpanAttributes.OUTPUT_VALUE
    _OTEL = True
except Exception:  # OpenTelemetry/OpenInference not installed — tracing is optional
    _trace = None
    _OTEL = False

_initialized = False


def setup_tracing() -> bool:
    """Initialise Arize tracing exactly once. Returns True when active, False when
    skipped. Never raises: a tracing failure must not take down the app."""
    global _initialized
    if _initialized:
        return True
    # Never trace under the test suite: keeps the pinned tests fast, offline, and
    # free of real LLM/export calls even when creds happen to be in the env
    # (backend/__init__ loads .env on every import, including under pytest).
    if "pytest" in sys.modules:
        return False
    if not _OTEL:
        print("[trace] OpenTelemetry not installed; tracing disabled.")
        return False

    space_id = os.getenv("ARIZE_SPACE_ID") or os.getenv("ARIZE_SPACE")
    api_key = os.getenv("ARIZE_API_KEY")
    if not (space_id and api_key):
        print(
            "[trace] Arize tracing disabled "
            "(set ARIZE_SPACE_ID and ARIZE_API_KEY to enable)."
        )
        return False

    try:
        from arize.otel import register

        # Sets the global OpenTelemetry tracer provider; manual spans below pick it
        # up via trace.get_tracer().
        register(space_id=space_id, api_key=api_key, project_name=PROJECT_NAME)
        _initialized = True
        print(f"[trace] Arize tracing on -> project '{PROJECT_NAME}'.")
        return True
    except Exception as exc:  # tracing must never crash the app
        print(f"[trace] Arize tracing setup failed ({exc}); continuing without it.")
        return False


# --- CHAIN span -------------------------------------------------------------


class _NullSpan:
    """No-op span handle used when tracing is inactive (covers CHAIN + LLM)."""

    def set_output(self, value: object) -> None:
        pass

    def set_attribute(self, key: str, value: object) -> None:
        pass

    def record_response(self, text: object, usage: object = None) -> None:
        pass


class _ChainSpanHandle:
    def __init__(self, span) -> None:
        self._span = span

    def set_output(self, value: object) -> None:
        self._span.set_attribute(_OUTPUT, str(value))

    def set_attribute(self, key: str, value: object) -> None:
        self._span.set_attribute(key, value)


@contextmanager
def chain_span(name: str, input_value: str = "") -> Iterator[object]:
    """CHAIN-kind span over one spine run. No-op (zero overhead) when tracing is
    off, so callers need no conditional logic."""
    if not (_OTEL and _initialized):
        yield _NullSpan()
        return
    tracer = _trace.get_tracer(PROJECT_NAME)
    with tracer.start_as_current_span(name) as span:
        span.set_attribute(_KIND, OpenInferenceSpanKindValues.CHAIN.value)
        if input_value:
            span.set_attribute(_INPUT, input_value)
        yield _ChainSpanHandle(span)


# --- LLM span ---------------------------------------------------------------


class _LLMSpanHandle:
    def __init__(self, span) -> None:
        self._span = span

    def record_response(self, text: object, usage: object = None) -> None:
        """Record the assistant reply and (if available) Anthropic token usage."""
        self._span.set_attribute("llm.output_messages.0.message.role", "assistant")
        self._span.set_attribute("llm.output_messages.0.message.content", str(text))
        self._span.set_attribute(_OUTPUT, str(text))
        if usage is not None:
            prompt = getattr(usage, "input_tokens", None)
            completion = getattr(usage, "output_tokens", None)
            if prompt is not None:
                self._span.set_attribute("llm.token_count.prompt", prompt)
            if completion is not None:
                self._span.set_attribute("llm.token_count.completion", completion)
            if prompt is not None and completion is not None:
                self._span.set_attribute("llm.token_count.total", prompt + completion)


@contextmanager
def llm_span(
    name: str,
    *,
    model: str,
    system: Optional[str] = None,
    user_messages: Optional[Sequence[dict]] = None,
) -> Iterator[object]:
    """LLM-kind span around an Anthropic ``messages.create`` call. Records the
    request (model, system + user messages) up front; call ``record_response``
    on the yielded handle to record the reply + token counts. No-op when tracing
    is off."""
    if not (_OTEL and _initialized):
        yield _NullSpan()
        return
    tracer = _trace.get_tracer(PROJECT_NAME)
    with tracer.start_as_current_span(name) as span:
        span.set_attribute(_KIND, OpenInferenceSpanKindValues.LLM.value)
        span.set_attribute("llm.provider", "anthropic")
        span.set_attribute("llm.model_name", model)
        idx = 0
        if system:
            span.set_attribute(f"llm.input_messages.{idx}.message.role", "system")
            span.set_attribute(f"llm.input_messages.{idx}.message.content", system)
            idx += 1
        contents = []
        for msg in user_messages or ():
            content = str(msg.get("content", ""))
            span.set_attribute(
                f"llm.input_messages.{idx}.message.role", str(msg.get("role", "user"))
            )
            span.set_attribute(f"llm.input_messages.{idx}.message.content", content)
            contents.append(content)
            idx += 1
        if contents:
            span.set_attribute(_INPUT, "\n".join(contents))
        yield _LLMSpanHandle(span)
