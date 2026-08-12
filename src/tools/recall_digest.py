"""Canonical digest encoding for the O2-H read-only recall protocol.

The wire payload keeps JSON numbers.  Digest bytes use a schema-aware fixed
decimal form for the three bounded floating-point fields so Python and
JavaScript cannot disagree after JSON parsing erases ``0.0`` versus ``0``.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping


DIGEST_PROFILE = "ombre-fixed6-numeric-v1"
FIXED6_FIELDS = frozenset({"relevance", "valence", "arousal"})
MAX_SAFE_INTEGER = 9_007_199_254_740_991


def normalize_unit_float(value: Any, *, field: str, minimum: float, maximum: float) -> float:
    """Return one finite six-decimal protocol float or fail closed."""

    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise ValueError(f"{field} is outside the protocol range")
    normalized = round(number, 6)
    return 0.0 if normalized == 0.0 else normalized


def normalize_nullable_affect(value: Any, *, field: str) -> float | None:
    if value is None or value == "":
        return None
    return normalize_unit_float(value, field=field, minimum=-1.0, maximum=1.0)


def canonical_json(value: Any, *, field: str = "") -> str:
    """Encode the v2 protocol digest form, independent of wire number lexemes."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int):
        if field in FIXED6_FIELDS:
            return f"{float(value):.6f}"
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError("non-profile protocol numbers must be safe integers")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("protocol JSON contains a non-finite number")
        if field in FIXED6_FIELDS:
            normalized = 0.0 if value == 0.0 else value
            return f"{normalized:.6f}"
        if not value.is_integer() or abs(value) > MAX_SAFE_INTEGER:
            raise ValueError("non-profile protocol numbers must be safe integers")
        return str(int(value))
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, Mapping):
        parts = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise ValueError("protocol JSON object keys must be strings")
            parts.append(
                json.dumps(key, ensure_ascii=False, separators=(",", ":"))
                + ":"
                + canonical_json(value[key], field=key)
            )
        return "{" + ",".join(parts) + "}"
    raise ValueError("protocol JSON contains an unsupported value")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
