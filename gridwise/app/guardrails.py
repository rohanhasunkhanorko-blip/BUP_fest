"""Deterministic guardrails around LLM operator-note output.

Per Problem Statement §08, LLM output is untrusted structured data until it
passes these checks. We coerce, normalize, and reject unsafe values before
the optimizer ever sees them. The function ``validate_and_normalize`` always
returns exactly ``n_notes`` entries in ``note_index`` order, so the
optimizer never has to handle malformed input.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

from .schemas import ALLOWED_DIRECTIVE_TYPES


ROUND_DIGITS = 4


def _normalize_hours(raw: Any) -> List[int]:
    """Coerce a raw ``hours`` value to a sorted list of unique ints in 0..23.

    Anything that cannot be coerced is dropped silently; callers detect the
    empty result and decide whether to keep or coerce the directive.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        return []
    out: List[int] = []
    seen = set()
    for x in raw:
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v in seen:
            continue
        if 0 <= v <= 23:
            seen.add(v)
            out.append(v)
    return sorted(out)


def _round(x: float) -> float:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return 0.0
    return round(float(x), ROUND_DIGITS)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _validate_solar_reduction(adj: Dict[str, Any], battery) -> Dict[str, Any]:
    if not isinstance(adj, dict):
        return {"hours": [], "factor": 0.0}
    hours = _normalize_hours(adj.get("hours"))
    factor_raw = adj.get("factor", 1.0)
    try:
        factor = float(factor_raw)
    except (TypeError, ValueError):
        factor = 1.0
    factor = _clamp(_round(factor), 0.0, 1.0)
    return {"hours": hours, "factor": factor}


def _validate_min_reserve(adj: Dict[str, Any], battery) -> Dict[str, Any]:
    if not isinstance(adj, dict):
        return {"hours": [], "minimum_energy_kwh": 0.0}
    hours = _normalize_hours(adj.get("hours"))
    try:
        v = float(adj.get("minimum_energy_kwh", 0.0))
    except (TypeError, ValueError):
        v = 0.0
    v = _clamp(_round(v), 0.0, battery.capacity_kwh)
    return {"hours": hours, "minimum_energy_kwh": v}


def _validate_hours_only(adj: Dict[str, Any], battery) -> Dict[str, Any]:
    if not isinstance(adj, dict):
        return {"hours": []}
    return {"hours": _normalize_hours(adj.get("hours"))}


def _validate_max_grid(adj: Dict[str, Any], battery) -> Dict[str, Any]:
    if not isinstance(adj, dict):
        return {"hours": [], "max_grid_kwh": 0.0}
    hours = _normalize_hours(adj.get("hours"))
    try:
        cap = float(adj.get("max_grid_kwh", 0.0))
    except (TypeError, ValueError):
        cap = 0.0
    cap = max(0.0, _round(cap))
    return {"hours": hours, "max_grid_kwh": cap}


_VALIDATORS = {
    "solar_reduction": _validate_solar_reduction,
    "minimum_battery_reserve": _validate_min_reserve,
    "no_charge_window": _validate_hours_only,
    "no_discharge_window": _validate_hours_only,
    "max_grid_window": _validate_max_grid,
}


def _safe_no_op_entry(note_index: int) -> Dict[str, Any]:
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": "Note treated as no-op after guardrail validation.",
    }


def _safe_directive_entry(
    note_index: int,
    directive_type: str,
    adj: Dict[str, Any],
    explanation: str,
) -> Dict[str, Any]:
    return {
        "note_index": note_index,
        "applies": True,
        "directive_type": directive_type,
        "structured_adjustment": adj,
        "explanation": explanation,
    }


def _coerce_one(raw: Dict[str, Any], note_index: int, battery) -> Dict[str, Any]:
    """Validate one raw entry. Falls back to no_op on any structural failure."""
    if not isinstance(raw, dict):
        return _safe_no_op_entry(note_index)

    try:
        raw_idx = int(raw.get("note_index", note_index))
    except (TypeError, ValueError):
        raw_idx = note_index
    if raw_idx != note_index:
        # We never trust the LLM's index ordering; rewrite it to match position.
        raw_idx = note_index

    dtype = raw.get("directive_type")
    applies_raw = raw.get("applies", False)
    explanation = str(raw.get("explanation") or "").strip()

    if dtype not in ALLOWED_DIRECTIVE_TYPES:
        return _safe_no_op_entry(note_index)

    if dtype == "no_op":
        return {
            "note_index": note_index,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": explanation or "Note marked as no-op.",
        }

    # Non-no_op directives must be applicable.
    validator = _VALIDATORS[dtype]
    adj = validator(raw.get("structured_adjustment"), battery)

    # Reject directives that lack required numeric / hour info -> no_op.
    if dtype == "solar_reduction":
        if not adj["hours"]:
            return _safe_no_op_entry(note_index)
    elif dtype == "minimum_battery_reserve":
        if not adj["hours"]:
            return _safe_no_op_entry(note_index)
    elif dtype == "no_charge_window" or dtype == "no_discharge_window":
        if not adj["hours"]:
            return _safe_no_op_entry(note_index)
    elif dtype == "max_grid_window":
        if not adj["hours"]:
            return _safe_no_op_entry(note_index)

    return _safe_directive_entry(
        note_index=note_index,
        directive_type=dtype,
        adj=adj,
        explanation=explanation or f"Applied {dtype} directive.",
    )


def validate_and_normalize(raw_list: List[Any], n_notes: int, battery) -> List[Dict[str, Any]]:
    """Return ``n_notes`` validated directive entries in ``note_index`` order.

    The LLM may return extra fields, duplicate entries, or items with the
    wrong ``note_index``. We rewrite ordering deterministically based on
    position and drop or coerce anything malformed.
    """
    # Normalize raw_list into a dict {position: raw_entry}; prefer explicit
    # note_index when in range, otherwise treat entries in order.
    position_to_raw: Dict[int, Dict[str, Any]] = {}
    fallback_iter = iter(raw_list or [])
    for pos in range(n_notes):
        candidate = None
        # First, try to consume the next entry and trust its note_index if valid.
        try:
            candidate = next(fallback_iter)
        except StopIteration:
            candidate = None
        if isinstance(candidate, dict):
            try:
                idx = int(candidate.get("note_index", pos))
            except (TypeError, ValueError):
                idx = pos
            if 0 <= idx < n_notes and idx not in position_to_raw:
                position_to_raw[idx] = candidate
                continue
            # Out-of-range index: stash under its own position if free.
            if pos not in position_to_raw:
                position_to_raw[pos] = candidate
                continue
        # No candidate left at this position.
        if pos not in position_to_raw:
            position_to_raw[pos] = None  # type: ignore[assignment]

    out: List[Dict[str, Any]] = []
    for i in range(n_notes):
        raw = position_to_raw.get(i)
        out.append(_coerce_one(raw, i, battery))
    return out
