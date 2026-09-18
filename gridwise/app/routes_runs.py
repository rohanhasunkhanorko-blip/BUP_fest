"""Run-history HTTP endpoints.

POST /runs        -> insert a completed run (used internally by /optimize-energy
                    and exposed for clients that ran their own pipeline).
GET  /runs        -> list the caller's recent runs (paginated).
GET  /runs/{id}   -> fetch a single run owned by the caller.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .auth import get_optional_user

router = APIRouter(prefix="/runs", tags=["runs"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class RunIn(BaseModel):
    """Payload that an authenticated client can POST to record a run."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(min_length=1)
    request_payload: Dict[str, Any]
    response_payload: Dict[str, Any]
    status: str = Field(default="success", pattern="^(success|failed)$")
    error_message: Optional[str] = None


class RunOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    scenario_id: str
    status: str
    request_payload: Dict[str, Any]
    response_payload: Dict[str, Any]
    error_message: Optional[str] = None
    created_at: datetime
    owner_id: Optional[str] = None  # hidden when caller is not the owner


class RunSummary(BaseModel):
    """Lightweight projection used by GET /runs list responses."""

    model_config = ConfigDict(extra="ignore")

    id: str
    scenario_id: str
    status: str
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc_to_summary(doc: dict) -> RunSummary:
    rp = doc.get("response_payload", {}) or {}
    return RunSummary(
        id=str(doc["_id"]),
        scenario_id=doc.get("scenario_id", ""),
        status=doc.get("status", "success"),
        total_grid_kwh=float(rp.get("total_grid_kwh", 0.0)),
        total_cost_bdt=float(rp.get("total_cost_bdt", 0.0)),
        peak_grid_kwh=float(rp.get("peak_grid_kwh", 0.0)),
        plan_summary=str(rp.get("plan_summary", "")),
        created_at=doc.get("created_at") or datetime.now(tz=timezone.utc),
    )


def _doc_to_out(doc: dict) -> RunOut:
    return RunOut(
        id=str(doc["_id"]),
        scenario_id=doc.get("scenario_id", ""),
        status=doc.get("status", "success"),
        request_payload=doc.get("request_payload", {}),
        response_payload=doc.get("response_payload", {}),
        error_message=doc.get("error_message"),
        created_at=doc.get("created_at") or datetime.now(tz=timezone.utc),
    )


async def _record_run(
    *,
    owner_id: Optional[str],
    scenario_id: str,
    request_payload: Dict[str, Any],
    response_payload: Dict[str, Any],
    status_str: str = "success",
    error_message: Optional[str] = None,
) -> Optional[str]:
    """Insert one row into the runs collection. Returns the new id, or None
    if the DB is unavailable (callers must not fail because of persistence)."""
    coll = db.runs()
    if coll is None:
        return None
    doc = {
        "owner_id": owner_id,
        "scenario_id": scenario_id,
        "request_payload": request_payload,
        "response_payload": response_payload,
        "status": status_str,
        "error_message": error_message,
        "created_at": datetime.now(tz=timezone.utc),
    }
    result = await coll.insert_one(doc)
    return str(result.inserted_id)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def create_run(body: RunIn, user=Depends(get_optional_user)) -> RunOut:
    """Manually record a run. Owner is the authenticated user (or anonymous)."""
    if not db.is_available():
        raise HTTPException(status_code=503, detail="Database unavailable.")
    owner = user["user_id"] if user else None
    new_id = await _record_run(
        owner_id=owner,
        scenario_id=body.scenario_id,
        request_payload=body.request_payload,
        response_payload=body.response_payload,
        status_str=body.status,
        error_message=body.error_message,
    )
    if new_id is None:
        raise HTTPException(status_code=503, detail="Database unavailable.")
    doc = await db.runs().find_one({"_id": ObjectId(new_id)})
    return _doc_to_out(doc) if doc else _doc_to_out({
        "_id": new_id,
        "scenario_id": body.scenario_id,
        "status": body.status,
        "request_payload": body.request_payload,
        "response_payload": body.response_payload,
        "error_message": body.error_message,
        "created_at": datetime.now(tz=timezone.utc),
    })


@router.get("", response_model=List[RunSummary])
async def list_runs(
    limit: int = Query(default=20, ge=1, le=100),
    scenario_id: Optional[str] = Query(default=None),
    user=Depends(get_optional_user),
) -> List[RunSummary]:
    if not db.is_available():
        raise HTTPException(status_code=503, detail="Database unavailable.")
    query: Dict[str, Any] = {}
    if user:
        query["owner_id"] = user["user_id"]
    if scenario_id:
        query["scenario_id"] = scenario_id
    cursor = (
        db.runs()
        .find(query)
        .sort("created_at", -1)
        .limit(limit)
    )
    return [_doc_to_summary(d) async for d in cursor]


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: str, user=Depends(get_optional_user)) -> RunOut:
    if not db.is_available():
        raise HTTPException(status_code=503, detail="Database unavailable.")

    try:
        oid = ObjectId(run_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid run id.")
    doc = await db.runs().find_one({"_id": oid})
    if doc is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if user and doc.get("owner_id") not in (None, user["user_id"]):
        raise HTTPException(status_code=404, detail="Run not found.")
    if not user and doc.get("owner_id") is not None:
        # Anonymous callers can only see runs that have no owner.
        raise HTTPException(status_code=404, detail="Run not found.")
    return _doc_to_out(doc)
