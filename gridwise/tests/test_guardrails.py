"""Tests for the deterministic guardrails around LLM output."""
from __future__ import annotations

from app.guardrails import validate_and_normalize


class _StubBattery:
    capacity_kwh = 200.0


def _normalize(raw, n_notes=3, battery=None):
    battery = battery or _StubBattery()
    return validate_and_normalize(raw, n_notes, battery)


def test_no_op_passthrough():
    out = _normalize(
        [
            {"note_index": 0, "applies": False, "directive_type": "no_op",
             "structured_adjustment": None, "explanation": "ignored"}
        ],
        n_notes=1,
    )
    assert len(out) == 1
    assert out[0]["applies"] is False
    assert out[0]["directive_type"] == "no_op"
    assert out[0]["structured_adjustment"] is None


def test_solar_reduction_factor_clamped():
    raw = [
        {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
         "structured_adjustment": {"hours": [10, 11], "factor": 5.0}},
    ]
    out = _normalize(raw, n_notes=1)
    assert out[0]["directive_type"] == "solar_reduction"
    assert out[0]["structured_adjustment"]["factor"] == 1.0


def test_invalid_directive_falls_back_to_no_op():
    raw = [
        {"note_index": 0, "applies": True, "directive_type": "made_up_thing",
         "structured_adjustment": {"hours": [10]}, "factor": 0.5}
    ]
    out = _normalize(raw, n_notes=1)
    assert out[0]["directive_type"] == "no_op"
    assert out[0]["applies"] is False


def test_missing_hours_in_solar_reduction_becomes_no_op():
    raw = [
        {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
         "structured_adjustment": {"hours": [], "factor": 0.2}}
    ]
    out = _normalize(raw, n_notes=1)
    assert out[0]["directive_type"] == "no_op"


def test_min_reserve_clamped_to_capacity():
    raw = [
        {"note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
         "structured_adjustment": {"hours": [18, 19], "minimum_energy_kwh": 9999}}
    ]
    out = _normalize(raw, n_notes=1)
    assert out[0]["structured_adjustment"]["minimum_energy_kwh"] == 200.0


def test_note_index_rewritten_to_position():
    raw = [
        {"note_index": 5, "applies": True, "directive_type": "solar_reduction",
         "structured_adjustment": {"hours": [10, 11], "factor": 0.2}},
        {"note_index": 0, "applies": False, "directive_type": "no_op",
         "structured_adjustment": None},
        {"note_index": 0, "applies": False, "directive_type": "no_op",
         "structured_adjustment": None},
    ]
    out = _normalize(raw, n_notes=3)
    assert [e["note_index"] for e in out] == [0, 1, 2]
