"""MongoDB Atlas connection management for GridWise.

Centralizes the async Motor client, exposes the ``gridwise`` database and named
collection accessors, and builds all indexes on startup. If the database is
unreachable the connection is left as ``None`` and all helpers become no-ops,
so the optimizer still serves requests (the API stays available even when
Atlas is down).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, IndexModel

logger = logging.getLogger("gridwise.db")

_client: Optional[AsyncIOMotorClient] = None
_db: Optional[AsyncIOMotorDatabase] = None


def _build_uri() -> str:
    """Compose the Atlas URI from the ``MONGODB_*`` env vars."""
    user = os.environ["MONGODB_USERNAME"]
    pwd = os.environ["MONGODB_PASSWORD"]
    cluster = os.environ["MONGODB_CLUSTER"]
    return f"mongodb+srv://{user}:{pwd}@{cluster}/?serverSelectionTimeoutMS=5000"


async def connect() -> None:
    """Open the Atlas connection and build indexes.

    Called from the FastAPI ``lifespan`` startup phase.
    """
    global _client, _db
    if _client is not None:
        return
    try:
        _client = AsyncIOMotorClient(_build_uri(), serverSelectionTimeoutMS=5000)
        # Trigger a real round-trip so we fail fast if creds are bad.
        await _client.server_info()
        db_name = os.environ.get("MONGODB_DB_NAME", "gridwise")
        _db = _client[db_name]
    except Exception as exc:  # noqa: BLE001
        logger.warning("MongoDB unavailable, persistence disabled: %s", exc)
        _client = None
        _db = None
        return
    # Build indexes; an index failure shouldn't kill the connection.
    try:
        await _ensure_indexes(_db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Index creation failed (continuing without them): %s", exc)
    logger.info("Connected to MongoDB Atlas db=%s", db_name)


async def disconnect() -> None:
    """Close the Atlas connection. Idempotent."""
    global _client, _db
    if _client is not None:
        _client.close()
    _client = None
    _db = None


async def _ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    """Create the indexes GridWise relies on. All idempotent."""
    await db["users"].create_indexes([
        IndexModel([("email", ASCENDING)], unique=True, name="users_email_unique"),
        IndexModel([("created_at", DESCENDING)], name="users_created_at"),
    ])
    # Partial unique on active (non-revoked) keys. Mongo only allows $eq-style
    # expressions in partialFilterExpression, so we filter on revoked=False.
    await db["api_keys"].create_indexes([
        IndexModel([("key_hash", ASCENDING)], unique=True, name="apikeys_hash_unique"),
        IndexModel(
            [("owner_id", ASCENDING)],
            name="apikeys_owner",
        ),
        IndexModel(
            [("key_hash", ASCENDING)],
            unique=True,
            partialFilterExpression={"revoked": False},
            name="apikeys_active_unique",
        ),
    ])
    await db["scenarios"].create_indexes([
        IndexModel(
            [("owner_id", ASCENDING), ("name", ASCENDING)],
            unique=True,
            name="scenarios_owner_name",
        ),
        IndexModel([("owner_id", ASCENDING), ("updated_at", DESCENDING)], name="scenarios_recent"),
    ])
    await db["runs"].create_indexes([
        IndexModel([("owner_id", ASCENDING), ("created_at", DESCENDING)], name="runs_recent"),
        IndexModel([("scenario_id", ASCENDING), ("created_at", DESCENDING)], name="runs_by_scenario"),
    ])
    # interpretation_cache TTL: rows auto-expire 24h after their created_at.
    await db["interpretation_cache"].create_indexes([
        IndexModel([("cache_key", ASCENDING)], unique=True, name="cache_key_unique"),
        IndexModel(
            [("created_at", ASCENDING)],
            expireAfterSeconds=86400,
            name="cache_ttl_24h",
        ),
    ])


# ---- Public accessors -------------------------------------------------------

def is_available() -> bool:
    """True iff the Atlas connection is live and indexes are built."""
    return _db is not None


def db() -> Optional[AsyncIOMotorDatabase]:
    """Return the active database handle, or None if disconnected."""
    return _db


def users():
    return _db["users"] if _db is not None else None


def api_keys():
    return _db["api_keys"] if _db is not None else None


def scenarios():
    return _db["scenarios"] if _db is not None else None


def runs():
    return _db["runs"] if _db is not None else None


def interpretation_cache():
    return _db["interpretation_cache"] if _db is not None else None
