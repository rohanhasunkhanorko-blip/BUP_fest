"""Scenario CRUD endpoints.

Scenarios are stored per-owner. A scenario body is a full ``ScenarioIn``
payload; it gets re-validated with the same Pydantic schema used by
``/optimize-energy`` so the UI can rely on round-trip safety.

POST   /scenarios        -> create a named scenario (name must be unique per owner)
GET    /scenarios        -> list the caller's scenarios
GET    /scenarios/{id}   -> fetch a single scenario
PUT    /scenarios/{id}   -> rename or replace payload
DELETE /scenarios/{id}   -> remove
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .auth import require_user
from .schemas import ScenarioIn

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


class ScenarioOut(BaseModel):
    """Stored scenario with metadata. Excludes the raw ``_id``."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    scenario_id: str
    payload: ScenarioIn
    created_at: datetime
    updated_at: datetime


class ScenarioUpsert(BaseModel):
    """Body for create + replace (PUT). The full optimization payload is required."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    payload: ScenarioIn


class ScenarioPatch(BaseModel):
    """Body for partial updates (rename or replace payload, not both optional)."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    payload: Optional[ScenarioIn] = None


def _doc_to_out(doc: dict) -> ScenarioOut:
    return ScenarioOut(
        id=str(doc["_id"]),
        name=doc["name"],
        scenario_id=doc.get("scenario_id", doc["payload"]["scenario_id"]),
        payload=ScenarioIn.model_validate(doc["payload"]),
        created_at=doc.get("created_at") or datetime.now(tz=timezone.utc),
        updated_at=doc.get("updated_at") or datetime.now(tz=timezone.utc),
    )


@router.post("", response_model=ScenarioOut, status_code=status.HTTP_201_CREATED)
async def create_scenario(body: ScenarioUpsert, user=Depends(require_user)) -> ScenarioOut:
    if not db.is_available():
        raise HTTPException(status_code=503, detail="Database unavailable.")
    coll = db.scenarios()
    if await coll.find_one({"owner_id": user["user_id"], "name": body.name}) is not None:
        raise HTTPException(status_code=409, detail="Scenario name already exists for this owner.")
    now = datetime.now(tz=timezone.utc)
    doc = {
        "owner_id": user["user_id"],
        "name": body.name,
        "scenario_id": body.payload.scenario_id,
        "payload": body.payload.model_dump(mode="json"),
        "created_at": now,
        "updated_at": now,
    }
    result = await coll.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _doc_to_out(doc)


@router.get("", response_model=List[ScenarioOut])
async def list_scenarios(user=Depends(require_user)) -> List[ScenarioOut]:
    if not db.is_available():
        raise HTTPException(status_code=503, detail="Database unavailable.")
    cursor = db.scenarios().find({"owner_id": user["user_id"]}).sort("updated_at", -1)
    return [_doc_to_out(d) async for d in cursor]


@router.get("/{scenario_obj_id}", response_model=ScenarioOut)
async def get_scenario(scenario_obj_id: str, user=Depends(require_user)) -> ScenarioOut:
    if not db.is_available():
        raise HTTPException(status_code=503, detail="Database unavailable.")
    try:
        oid = ObjectId(scenario_obj_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid scenario id.")
    doc = await db.scenarios().find_one({"_id": oid, "owner_id": user["user_id"]})
    if doc is None:
        raise HTTPException(status_code=404, detail="Scenario not found.")
    return _doc_to_out(doc)


@router.put("/{scenario_obj_id}", response_model=ScenarioOut)
async def replace_scenario(scenario_obj_id: str, body: ScenarioUpsert, user=Depends(require_user)) -> ScenarioOut:
    if not db.is_available():
        raise HTTPException(status_code=503, detail="Database unavailable.")
    try:
        oid = ObjectId(scenario_obj_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid scenario id.")
    coll = db.scenarios()
    existing = await coll.find_one({"_id": oid, "owner_id": user["user_id"]})
    if existing is None:
        raise HTTPException(status_code=404, detail="Scenario not found.")
    if body.name != existing["name"]:
        dup = await coll.find_one({"owner_id": user["user_id"], "name": body.name})
        if dup is not None and str(dup["_id"]) != scenario_obj_id:
            raise HTTPException(status_code=409, detail="Scenario name already exists for this owner.")
    await coll.update_one(
        {"_id": oid},
        {
            "$set": {
                "name": body.name,
                "scenario_id": body.payload.scenario_id,
                "payload": body.payload.model_dump(mode="json"),
                "updated_at": datetime.now(tz=timezone.utc),
            }
        },
    )
    doc = await coll.find_one({"_id": oid})
    return _doc_to_out(doc)


@router.patch("/{scenario_obj_id}", response_model=ScenarioOut)
async def patch_scenario(scenario_obj_id: str, body: ScenarioPatch, user=Depends(require_user)) -> ScenarioOut:
    if not db.is_available():
        raise HTTPException(status_code=503, detail="Database unavailable.")
    try:
        oid = ObjectId(scenario_obj_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid scenario id.")
    coll = db.scenarios()
    existing = await coll.find_one({"_id": oid, "owner_id": user["user_id"]})
    if existing is None:
        raise HTTPException(status_code=404, detail="Scenario not found.")
    if body.name is None and body.payload is None:
        raise HTTPException(status_code=400, detail="Provide at least one of: name, payload.")
    update: Dict[str, Any] = {"updated_at": datetime.now(tz=timezone.utc)}
    if body.name is not None and body.name != existing["name"]:
        dup = await coll.find_one({"owner_id": user["user_id"], "name": body.name})
        if dup is not None and str(dup["_id"]) != scenario_obj_id:
            raise HTTPException(status_code=409, detail="Scenario name already exists for this owner.")
        update["name"] = body.name
    if body.payload is not None:
        update["payload"] = body.payload.model_dump(mode="json")
        update["scenario_id"] = body.payload.scenario_id
    await coll.update_one({"_id": oid}, {"$set": update})
    doc = await coll.find_one({"_id": oid})
    return _doc_to_out(doc)


@router.delete("/{scenario_obj_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_scenario(scenario_obj_id: str, user=Depends(require_user)) -> Response:
    if not db.is_available():
        raise HTTPException(status_code=503, detail="Database unavailable.")
    try:
        oid = ObjectId(scenario_obj_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid scenario id.")
    result = await db.scenarios().delete_one({"_id": oid, "owner_id": user["user_id"]})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Scenario not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
