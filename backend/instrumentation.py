"""instrumentation.py — Arize AX tracing (additive, server-run only).

Auto-instruments the Anthropic SDK so every live LLM call (currently
``answer_question`` -> ``messages.create``) emits an OpenInference LLM span, and
provides ``chain_span`` for the manual CHAIN span that wraps the
``ingest -> route -> handle_command`` spine. LLM spans created inside a
``chain_span`` block nest under it automatically (shared OTel context).

Design constraints (matching the rest of the backend):
  * Purely additive — never changes routing/handler behaviour.
  * Degrades to no-ops when OpenTelemetry is not installed, when Arize creds are
    absent, or when running under pytest — so the typed demo and the pinned test
    suite need no cloud credentials and incur no tracing overhead.
  * Reads creds from env ONLY (``ARIZE_SPACE_ID`` / ``ARIZE_API_KEY``); never
    embeds secret values. ``project_name`` is a non-secret constant.

Note: ``_llm_route``/``_llm_parse`` call ``messages.parse``, which does not exist
in the pinned anthropic 0.40.0 SDK, so those paths fall back to the deterministic
router and emit no LLM span. Only the ``ask`` path (``messages.create``) is a
live, auto-instrumented call. See the project notes for the parse() gap.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Iterator

PROJECT_NAME = "cal-2026-lab"

try:
    from opentelemetry import trace as _trace
    from openinference.semconv.trace import (
        OpenInferenceSpanKindValues,
        SpanAttributes,
    )

    _OTEL = True
except Exception:  # OpenTelemetry/OpenInference not installed — tracing is optional
    _trace = None
    _OTEL = False

_initialized = False


def setup_tracing() -> bool:
    """Initialise Arize tracing + Anthropic auto-instrumentation exactly once.

    Returns True when tracing is active, False when skipped. Never raises: a
    tracing failure must not take down the app.
    """
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
        from openinference.instrumentation.anthropic import AnthropicInstrumentor

        provider = register(
            space_id=space_id,
            api_key=api_key,
            project_name=PROJECT_NAME,
        )
        AnthropicInstrumentor().instrument(tracer_provider=provider)
        _initialized = True
        print(f"[trace] Arize tracing on -> project '{PROJECT_NAME}'.")
        return True
    except Exception as exc:  # tracing must never crash the app
        print(f"[trace] Arize tracing setup failed ({exc}); continuing without it.")
        return False


class _NullSpan:
    """No-op span handle used when tracing is inactive."""

    def set_output(self, value: object) -> None:
        pass

    def set_attribute(self, key: str, value: object) -> None:
        pass


class _SpanHandle:
    """Thin wrapper exposing only the two operations callers need."""

    def __init__(self, span) -> None:
        self._span = span

    def set_output(self, value: object) -> None:
        self._span.set_attribute(SpanAttributes.OUTPUT_VALUE, str(value))

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
        span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND,
            OpenInferenceSpanKindValues.CHAIN.value,
        )
        if input_value:
            span.set_attribute(SpanAttributes.INPUT_VALUE, input_value)
        yield _SpanHandle(span)
