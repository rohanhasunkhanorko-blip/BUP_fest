"""Pydantic schemas for the GridWise preliminary API.

These mirror the canonical contract in the Problem Statement (§07 request,
§08 guardrails, §09 energy rules, §10 response).
"""
from __future__ import annotations

from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]

BatteryAction = Literal["charge", "discharge", "idle"]

ALLOWED_DIRECTIVE_TYPES: tuple[str, ...] = (
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class Hour(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hour: int = Field(ge=0, le=23)
    demand_kwh: float = Field(ge=0)
    solar_kwh: float = Field(ge=0)
    tariff_bdt_per_kwh: float = Field(ge=0)


class Battery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity_kwh: float = Field(gt=0)
    initial_energy_kwh: float = Field(ge=0)
    minimum_energy_kwh: float = Field(ge=0)
    max_charge_kwh_per_hour: float = Field(ge=0)
    max_discharge_kwh_per_hour: float = Field(ge=0)

    @model_validator(mode="after")
    def _check_bounds(self) -> "Battery":
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial_energy_kwh cannot exceed capacity_kwh")
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh cannot exceed capacity_kwh")
        return self


class ScenarioIn(BaseModel):
    """POST /optimize-energy request body."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(min_length=1)
    operator_notes: Annotated[List[str], Field(min_length=1, max_length=3)]
    hours: Annotated[List[Hour], Field(min_length=24, max_length=24)]
    battery: Battery

    @field_validator("operator_notes")
    @classmethod
    def _notes_non_empty(cls, v: List[str]) -> List[str]:
        cleaned = [n.strip() for n in v]
        if any(len(n) == 0 for n in cleaned):
            raise ValueError("operator_notes must not contain empty strings")
        return cleaned

    @field_validator("hours")
    @classmethod
    def _hours_complete_and_unique(cls, v: List[Hour]) -> List[Hour]:
        seen = [h.hour for h in v]
        if sorted(seen) != list(range(24)):
            raise ValueError("hours must contain exactly one entry per hour 0..23")
        return sorted(v, key=lambda h: h.hour)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SolarReductionAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: List[int]
    factor: float = Field(ge=0.0, le=1.0)


class MinimumBatteryReserveAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: List[int]
    minimum_energy_kwh: float = Field(ge=0.0)


class HoursOnlyAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: List[int]


class MaxGridWindowAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: List[int]
    max_grid_kwh: float = Field(ge=0.0)


StructuredAdjustment = Union[
    SolarReductionAdjustment,
    MinimumBatteryReserveAdjustment,
    HoursOnlyAdjustment,
    MaxGridWindowAdjustment,
    None,
]


class DirectiveInterpretation(BaseModel):
    """One machine-checkable interpretation entry per operator note."""

    model_config = ConfigDict(extra="forbid")

    note_index: int = Field(ge=0)
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: Optional[dict] = None
    explanation: str = ""

    @field_validator("structured_adjustment", mode="before")
    @classmethod
    def _coerce_none(cls, v):
        # Accept None / {} / missing and let shape be validated downstream.
        return v


class HourlyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hour: int = Field(ge=0, le=23)
    grid_kwh: float = Field(ge=0)
    solar_used_kwh: float = Field(ge=0)
    battery_action: BatteryAction
    battery_kwh: float = Field(ge=0)
    battery_energy_after_kwh: float = Field(ge=0)

    @model_validator(mode="after")
    def _idle_zero_kwh(self) -> "HourlyPlan":
        if self.battery_action == "idle" and self.battery_kwh != 0:
            raise ValueError("battery_kwh must be 0 when battery_action is idle")
        return self


class ResponseOut(BaseModel):
    """POST /optimize-energy response body."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: List[HourlyPlan]
    total_grid_kwh: float = Field(ge=0)
    total_cost_bdt: float = Field(ge=0)
    peak_grid_kwh: float = Field(ge=0)
    plan_summary: str

    @field_validator("directive_interpretation")
    @classmethod
    def _interpretation_length_matches(cls, v: List[DirectiveInterpretation]) -> List[DirectiveInterpretation]:
        # Order and length are enforced at the API boundary, but check ascending note_index.
        ids = [d.note_index for d in v]
        if ids != sorted(ids) or len(set(ids)) != len(ids):
            raise ValueError("directive_interpretation note_index values must be unique and ascending")
        return v

    @field_validator("hourly_plan")
    @classmethod
    def _plan_complete(cls, v: List[HourlyPlan]) -> List[HourlyPlan]:
        seen = [p.hour for p in v]
        if sorted(seen) != list(range(24)):
            raise ValueError("hourly_plan must contain exactly hours 0..23")
        return sorted(v, key=lambda p: p.hour)
