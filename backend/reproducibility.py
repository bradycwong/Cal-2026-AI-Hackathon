"""Pure, network-free reproducibility checker (v1: volume only).

Compares the volume the researcher logged against the active step's
``parameters["volume_ul"]`` and returns a small flag dict the rest of the system
threads through events, persistence, and the notebook renderer. Read-only: it
never mutates state and never edits the logged text.

v1 scope is ``volume_ul`` exact-match only. ``status == "ok"`` means the volume
matches; it does NOT verify reagent or temperature.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .router import normalize_ascii

# After normalize_ascii(), all volume spellings collapse to "<n> uL".
_VOL_RE = re.compile(r"(\d+(?:\.\d+)?)\s*uL")


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _coerce(value: float) -> Any:
    return int(value) if float(value).is_integer() else value


def check(step_parameters: dict[str, Any], log_text: str) -> Optional[dict[str, Any]]:
    expected_raw = (step_parameters or {}).get("volume_ul")
    expected = _as_number(expected_raw)
    if expected is None:
        return None

    match = _VOL_RE.search(normalize_ascii(log_text or ""))
    if not match:
        return None
    logged = float(match.group(1))

    return {
        "parameter": "volume_ul",
        "expected": _coerce(expected),
        "logged": _coerce(logged),
        "unit": "uL",
        "status": "ok" if expected == logged else "mismatch",
    }
