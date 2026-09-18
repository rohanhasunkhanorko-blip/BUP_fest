"""End-to-end tests against the public sample case pack."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.directives import apply_directives
from app.guardrails import validate_and_normalize
from app.llm import _mock_interpret
from app.optimizer import solve
from app.schemas import Battery, Hour, ResponseOut


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_FILE = ROOT.parent / "files" / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"

TOLERANCE = 0.01


def _load_samples():
    with SAMPLE_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    return data["cases"]


def _run_case(case: dict):
    inp = case["input"]
    notes = inp["operator_notes"]
    hours = [Hour(**h) for h in inp["hours"]]
    battery = Battery(**inp["battery"])
    raw = _mock_interpret(notes, battery.capacity_kwh)
    validated = validate_and_normalize(raw, len(notes), battery)
    applied = apply_directives(validated, hours, battery)
    result = solve(hours, battery, applied)
    response = ResponseOut(
        scenario_id=case["input"]["scenario_id"],
        directive_interpretation=validated,
        hourly_plan=result.hourly_plan,
        total_grid_kwh=result.total_grid_kwh,
        total_cost_bdt=result.total_cost_bdt,
        peak_grid_kwh=result.peak_grid_kwh,
        plan_summary="summary",
    )
    return response, hours, battery


@pytest.mark.parametrize("case", _load_samples(), ids=lambda c: c["id"])
def test_response_matches_expected_totals(case: dict) -> None:
    response, hours, battery = _run_case(case)
    expected = case["expected_output"]
    assert response.total_grid_kwh == pytest.approx(expected["total_grid_kwh"], abs=TOLERANCE)
    assert response.total_cost_bdt == pytest.approx(expected["total_cost_bdt"], abs=TOLERANCE)
    assert response.peak_grid_kwh == pytest.approx(expected["peak_grid_kwh"], abs=TOLERANCE)


@pytest.mark.parametrize("case", _load_samples(), ids=lambda c: c["id"])
def test_response_shape(case: dict) -> None:
    response, hours, battery = _run_case(case)
    # Schema rules: 24 entries, hours 0..23, ascending.
    assert [p.hour for p in response.hourly_plan] == list(range(24))
    # Each directive entry has note_index ascending unique.
    idxs = [d.note_index for d in response.directive_interpretation]
    assert idxs == sorted(idxs) and len(set(idxs)) == len(idxs)
    # Idle -> battery_kwh must be 0.
    for p in response.hourly_plan:
        if p.battery_action == "idle":
            assert p.battery_kwh == 0.0


@pytest.mark.parametrize("case", _load_samples(), ids=lambda c: c["id"])
def test_energy_balance_and_neutrality(case: dict) -> None:
    response, hours, battery = _run_case(case)
    # Energy balance per hour: g + s + d = demand + c.
    e0 = battery.initial_energy_kwh
    last_e = e0
    for p, h in zip(response.hourly_plan, hours):
        c = p.battery_kwh if p.battery_action == "charge" else 0.0
        d = p.battery_kwh if p.battery_action == "discharge" else 0.0
        lhs = p.grid_kwh + p.solar_used_kwh + d
        rhs = h.demand_kwh + c
        assert lhs == pytest.approx(rhs, abs=TOLERANCE)
        # Battery after hour.
        last_e = last_e + c - d
        assert p.battery_energy_after_kwh == pytest.approx(last_e, abs=TOLERANCE)
    # End-of-day neutrality.
    assert last_e == pytest.approx(e0, abs=TOLERANCE)
