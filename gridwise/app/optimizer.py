"""24-hour cost-minimizing LP using PuLP.

Variables per hour h:
  g[h]   grid import  (>= 0)
  s[h]   solar used    (0..effective_solar[h])
  c[h]   battery charge  (>= 0)
  d[h]   battery discharge (>= 0)
  yc[h]  binary: 1 iff c[h] > 0
  yd[h]  binary: 1 iff d[h] > 0

Derived:
  E[h]   battery energy after hour h
       = E0 + sum_{i<=h}(c[i] - d[i])

Energy balance per hour:
  g[h] + s[h] + d[h] = demand[h] + c[h]

Hard constraints:
  s[h] <= effective_solar[h]
  0 <= E[h] <= capacity_kwh
  E[h] >= max(base_min, directive_min[h])
  c[h] <= max_charge_kwh_per_hour * yc[h]
  d[h] <= max_discharge_kwh_per_hour * yd[h]
  c[h] = 0 if h in no_charge_hours
  d[h] = 0 if h in no_discharge_hours
  g[h] <= max_grid[h]   (default +inf)
  yc[h] + yd[h] <= 1    (exactly one action per hour: charge XOR discharge XOR idle)
  yc[h] <= c[h] / eps   (forces binary to 0 when charge is 0)
  yd[h] <= d[h] / eps   (forces binary to 0 when discharge is 0)
  sum_{h}(c[h] - d[h]) == 0   (end-of-day neutrality)

Objective: minimize sum_h g[h] * tariff[h]
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import pulp

from .directives import AppliedDirectives


ROUND = 4


@dataclass
class OptimizationResult:
    hourly_plan: List[dict]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float


def _round(x: float) -> float:
    return round(float(x), ROUND)


def solve(hours: List, battery, applied: AppliedDirectives) -> OptimizationResult:
    """Solve the 24-hour LP and return a structured plan.

    Raises ``Infeasible`` when PuLP cannot find a feasible solution.
    """
    n = 24
    prob = pulp.LpProblem("gridwise", pulp.LpMinimize)

    g = [pulp.LpVariable(f"g_{h}", lowBound=0) for h in range(n)]
    s = [pulp.LpVariable(f"s_{h}", lowBound=0, upBound=float(applied.effective_solar[h])) for h in range(n)]
    c = [pulp.LpVariable(f"c_{h}", lowBound=0) for h in range(n)]
    d = [pulp.LpVariable(f"d_{h}", lowBound=0) for h in range(n)]
    yc = [pulp.LpVariable(f"yc_{h}", cat=pulp.LpBinary) for h in range(n)]
    yd = [pulp.LpVariable(f"yd_{h}", cat=pulp.LpBinary) for h in range(n)]

    # Big-M for action mutex
    max_charge = float(battery.max_charge_kwh_per_hour)
    max_discharge = float(battery.max_discharge_kwh_per_hour)
    cap = float(battery.capacity_kwh)
    e0 = float(battery.initial_energy_kwh)

    # Objective
    tariff = [float(h.tariff_bdt_per_kwh) for h in hours]
    prob += pulp.lpSum(g[h] * tariff[h] for h in range(n))

    # Battery energy after hour h, expressed via telescoping sum
    # E[h] = E0 + sum_{i<=h}(c[i] - d[i]). We use auxiliary variables E[h].
    E = [pulp.LpVariable(f"E_{h}", lowBound=0, upBound=cap) for h in range(n)]
    prob += E[0] == e0 + c[0] - d[0]
    for h in range(1, n):
        prob += E[h] == E[h - 1] + c[h] - d[h]

    # Energy balance per hour
    for h in range(n):
        demand = float(hours[h].demand_kwh)
        prob += g[h] + s[h] + d[h] == demand + c[h]

    # Charge / discharge bounds + binary mutex
    eps = 1e-6
    for h in range(n):
        # Charge limits
        if h in applied.no_charge_hours:
            prob += c[h] == 0
            prob += yc[h] == 0
        else:
            prob += c[h] <= max_charge * yc[h]
            prob += c[h] >= eps * yc[h]
        # Discharge limits
        if h in applied.no_discharge_hours:
            prob += d[h] == 0
            prob += yd[h] == 0
        else:
            prob += d[h] <= max_discharge * yd[h]
            prob += d[h] >= eps * yd[h]
        # Mutex: at most one action per hour
        prob += yc[h] + yd[h] <= 1

    # Per-hour minimum reserve
    for h in range(n):
        prob += E[h] >= float(applied.min_reserve_by_hour[h])

    # Max-grid caps
    for h in range(n):
        cap_h = applied.max_grid_by_hour[h]
        if cap_h != float("inf"):
            prob += g[h] <= float(cap_h)

    # End-of-day neutrality
    prob += pulp.lpSum(c[h] - d[h] for h in range(n)) == 0

    solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=8, gapRel=1e-4)
    status = prob.solve(solver)
    if pulp.LpStatus[status] not in ("Optimal", "Not Solved") and status != 1:
        # status==1 is Optimal; allow Not Solved only when objective is finite.
        if status != 1:
            raise Infeasible(f"PuLP status: {pulp.LpStatus[status]}")

    # Extract solution
    plan: List[dict] = []
    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0
    for h in range(n):
        gv = max(0.0, pulp.value(g[h]) or 0.0)
        sv = max(0.0, min(float(applied.effective_solar[h]), pulp.value(s[h]) or 0.0))
        cv = max(0.0, pulp.value(c[h]) or 0.0)
        dv = max(0.0, pulp.value(d[h]) or 0.0)
        ev = max(0.0, min(cap, pulp.value(E[h]) or 0.0))
        # Snap near-zero values to zero to avoid spurious "idle" misclassifications.
        if cv < 1e-3:
            cv = 0.0
        if dv < 1e-3:
            dv = 0.0

        # Classify the action. If both c and d are essentially zero, idle.
        # Mutex from the LP ensures only one is positive, but we still defensively prefer discharge when
        # both happen to be near zero (the optimizer should not allow this).
        if cv > 0 and dv <= 0:
            action = "charge"
            batt_kwh = cv
        elif dv > 0 and cv <= 0:
            action = "discharge"
            batt_kwh = dv
        else:
            action = "idle"
            batt_kwh = 0.0

        gv_r = _round(gv)
        sv_r = _round(sv)
        ev_r = _round(ev)
        batt_r = _round(batt_kwh)

        plan.append(
            {
                "hour": h,
                "grid_kwh": gv_r,
                "solar_used_kwh": sv_r,
                "battery_action": action,
                "battery_kwh": batt_r,
                "battery_energy_after_kwh": ev_r,
            }
        )
        total_grid += gv_r
        total_cost += gv_r * tariff[h]
        if gv_r > peak_grid:
            peak_grid = gv_r

    return OptimizationResult(
        hourly_plan=plan,
        total_grid_kwh=_round(total_grid),
        total_cost_bdt=_round(total_cost),
        peak_grid_kwh=_round(peak_grid),
    )


class Infeasible(Exception):
    """Raised when the optimizer cannot produce a feasible schedule."""
