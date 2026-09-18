"""LLM interpretation cache backed by MongoDB.

A row in ``interpretation_cache`` is keyed by the SHA-256 of
``(battery_capacity_kwh, json_dump(sorted_notes))``. Hits return the stored
interpretations list verbatim. Misses store the computed result with a TTL
index that purges rows after 24 hours.

All exceptions are swallowed — the cache must NEVER break an optimization. If
Atlas is unreachable or the document is malformed we fall through to a fresh
interpretation every time.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from . import db

logger = logging.getLogger("gridwise.cache")


def _battery_capacity(value: Any) -> float:
    """Defensive coercion (the LLM layer accepts Battery model or float)."""
    if hasattr(value, "capacity_kwh"):
        return float(value.capacity_kwh)  # type: ignore[attr-defined]
    return float(value)


def cache_key(notes: List[str], battery_capacity_kwh: Any) -> str:
    """Stable SHA-256 of the inputs that affect interpretation."""
    cap = _battery_capacity(battery_capacity_kwh)
    payload = json.dumps(
        {"battery_capacity_kwh": cap, "notes": sorted(n.strip() for n in notes)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def get(notes: List[str], battery_capacity_kwh: Any) -> Optional[List[dict]]:
    """Return a cached interpretation list or None on miss / failure."""
    coll = db.interpretation_cache()
    if coll is None:
        return None
    try:
        doc = await coll.find_one({"cache_key": cache_key(notes, battery_capacity_kwh)})
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache.get failed: %s", exc)
        return None
    if doc is None:
        return None
    payload = doc.get("interpretations")
    if not isinstance(payload, list):
        return None
    return payload


async def put(notes: List[str], battery_capacity_kwh: Any, interpretations: List[dict]) -> None:
    """Store a fresh interpretation. Best-effort, never raises."""
    coll = db.interpretation_cache()
    if coll is None:
        return
    try:
        await coll.update_one(
            {"cache_key": cache_key(notes, battery_capacity_kwh)},
            {
                "$set": {
                    "interpretations": interpretations,
                    "battery_capacity_kwh": _battery_capacity(battery_capacity_kwh),
                    "created_at": datetime.now(tz=timezone.utc),
                },
                "$setOnInsert": {"cache_key": cache_key(notes, battery_capacity_kwh)},
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache.put failed: %s", exc)
