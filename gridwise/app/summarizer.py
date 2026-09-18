"""Deterministic, template-based plan-summary generator.

The LLM role is intentionally optional here: every directive was already
validated by guardrails and applied deterministically, so the summary is a
pure function of the validated interpretations plus the requested hours.
"""
from __future__ import annotations

from typing import List

from .guardrails import validate_and_normalize  # re-export not needed; we use the raw interpretations
from .schemas import DirectiveInterpretation

_TYPE_LABELS = {
    "solar_reduction": "Reduced solar output",
    "no_charge_window": "Enforced no-charge window",
    "no_discharge_window": "Enforced no-discharge window",
    "no_charge_no_discharge_window": "Enforced no-charge/no-discharge window",
    "minimum_battery_reserve": "Maintained minimum battery reserve",
    "max_grid_window": "Capped grid import",
    "no_op": "No directive applied",
}


def _format_hours(hours: List[int]) -> str:
    if not hours:
        return ""
    # Collapse contiguous ranges for readability, e.g. [13,14,15,18,19] -> "13-15, 18-19"
    hours = sorted(set(hours))
    ranges: List[str] = []
    start = prev = hours[0]
    for h in hours[1:]:
        if h == prev + 1:
            prev = h
            continue
        ranges.append(f"{start}-{prev}" if start != prev else f"{start}")
        start = prev = h
    ranges.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ", ".join(ranges)


def build_plan_summary(interpretations: List[DirectiveInterpretation]) -> str:
    """Render the natural-language summary of applied directives.

    The function is pure: identical inputs always yield identical output.
    """
    parts: List[str] = []
    applied_count = 0

    for interp in interpretations:
        if not interp.applies:
            continue
        applied_count += 1
        dtype = interp.directive_type
        adj = interp.structured_adjustment
        label = _TYPE_LABELS.get(dtype, dtype.replace("_", " ").capitalize())

        hours = adj.get("hours") if isinstance(adj, dict) else getattr(adj, "hours", None)
        if dtype == "solar_reduction" and adj and hours:
            raw_factor = adj.get("factor") if isinstance(adj, dict) else getattr(adj, "factor", None)
            if raw_factor is None:
                parts.append(f"{label} during hours {_format_hours(hours)}.")
            else:
                factor = round(raw_factor, 2)
                fstr = f"{int(factor * 100)}%"
                parts.append(f"{label} to {fstr} during hours {_format_hours(hours)}.")
        elif dtype in (
            "no_charge_window",
            "no_discharge_window",
            "no_charge_no_discharge_window",
        ) and adj and hours:
            parts.append(f"{label} during hours {_format_hours(hours)}.")
        elif dtype == "minimum_battery_reserve" and adj and hours:
            raw_kw = adj.get("minimum_energy_kwh") if isinstance(adj, dict) else getattr(adj, "minimum_energy_kwh", None)
            if raw_kw is None:
                parts.append(f"{label} during hours {_format_hours(hours)}.")
            else:
                kw = round(raw_kw, 2)
                parts.append(
                    f"{label} of at least {kw} kWh during hours {_format_hours(hours)}."
                )
        elif dtype == "max_grid_window" and adj and hours:
            raw_cap = adj.get("max_grid_kwh") if isinstance(adj, dict) else getattr(adj, "max_grid_kwh", None)
            if raw_cap is None:
                parts.append(f"{label} during hours {_format_hours(hours)}.")
            else:
                cap = round(raw_cap, 2)
                parts.append(f"{label} at {cap} kWh during hours {_format_hours(hours)}.")
        else:
            parts.append(f"{label}.")

    if applied_count == 0:
        return "No operator directives were applied; the schedule was optimized from the baseline scenario alone."

    return " ".join(parts)
