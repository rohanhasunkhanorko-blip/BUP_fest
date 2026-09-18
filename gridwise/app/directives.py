"""Apply validated operator-note directives to the optimization inputs.

Per Problem Statement §5.3, the directives change the underlying math:
- solar_reduction scales effective solar for the listed hours
- minimum_battery_reserve raises the per-hour minimum battery energy
- no_charge_window / no_discharge_window force the matching battery flow to 0
- max_grid_window caps grid import in the listed hours
- no_op leaves the math unchanged
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set


@dataclass
class AppliedDirectives:
    effective_solar: List[float] = field(default_factory=list)
    min_reserve_by_hour: List[float] = field(default_factory=list)
    no_charge_hours: Set[int] = field(default_factory=set)
    no_discharge_hours: Set[int] = field(default_factory=set)
    max_grid_by_hour: List[float] = field(default_factory=list)


def apply_directives(interpretations: List[dict], hours: List, battery) -> AppliedDirectives:
    """Translate validated interpretations into per-hour constraints."""
    base_solar = [float(h.solar_kwh) for h in hours]
    base_min = float(battery.minimum_energy_kwh)

    effective_solar = list(base_solar)
    min_reserve = [base_min for _ in range(24)]
    no_charge: Set[int] = set()
    no_discharge: Set[int] = set()
    max_grid: List[float] = [float("inf") for _ in range(24)]

    for entry in interpretations:
        dtype = entry.get("directive_type")
        adj = entry.get("structured_adjustment") or {}
        hours_list = [int(h) for h in (adj.get("hours") or [])]
        if dtype == "solar_reduction":
            factor = float(adj.get("factor", 1.0))
            for h in hours_list:
                effective_solar[h] = round(base_solar[h] * factor, 4)
        elif dtype == "minimum_battery_reserve":
            try:
                reserve = float(adj.get("minimum_energy_kwh", base_min))
            except (TypeError, ValueError):
                reserve = base_min
            reserve = min(max(reserve, 0.0), float(battery.capacity_kwh))
            for h in hours_list:
                min_reserve[h] = max(min_reserve[h], reserve)
        elif dtype == "no_charge_window":
            no_charge.update(hours_list)
        elif dtype == "no_discharge_window":
            no_discharge.update(hours_list)
        elif dtype == "max_grid_window":
            cap = float(adj.get("max_grid_kwh", float("inf")))
            for h in hours_list:
                if cap < max_grid[h]:
                    max_grid[h] = cap

    return AppliedDirectives(
        effective_solar=effective_solar,
        min_reserve_by_hour=min_reserve,
        no_charge_hours=no_charge,
        no_discharge_hours=no_discharge,
        max_grid_by_hour=max_grid,
    )
