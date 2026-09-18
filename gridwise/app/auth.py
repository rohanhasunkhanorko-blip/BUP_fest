"""Authentication and API-key helpers for GridWise.

Model
-----
* Users sign up with email + password. Passwords are bcrypt-hashed and never
  returned.
* On signup or login, a plaintext API key is returned ONCE. The server stores
  only ``bcrypt(api_key)`` plus a short JWT signed with ``JWT_SECRET`` carrying
  ``sub=owner_id`` and ``exp=created_at + API_KEY_TTL_DAYS``.
* Subsequent requests pass the API key in ``X-API-Key`` (or ``Authorization:
  Bearer ...``).
* If ``JWT_SECRET`` is unset or DB is unavailable, ``get_optional_user`` is a
  no-op so the rest of the API keeps working in the original stateless way.

Public surface
--------------
* ``POST /auth/signup``        -> {user_id, api_key}
* ``POST /auth/login``         -> {user_id, api_key}
* ``POST /auth/keys``          -> {api_key} (requires existing API key)
* ``GET  /auth/me``            -> {user_id, email}
* ``get_optional_user``        -> FastAPI dependency; returns dict or None
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException, status

from . import db as _db

# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

JWT_ALG = "HS256"


def _secret() -> str:
    s = os.environ.get("JWT_SECRET", "")
    if len(s) < 32:
        # Fall back to a process-lifetime random secret so dev / tests still
        # work without explicit config. Production should always set the env.
        return os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))
    return s


def _ttl_days() -> int:
    try:
        return max(1, int(os.environ.get("API_KEY_TTL_DAYS", "30")))
    except ValueError:
        return 30


def issue_token(owner_id: str) -> str:
    """Sign a short JWT containing ``sub=owner_id`` and ``exp=now+ttl``."""
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": str(owner_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=_ttl_days())).timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm=JWT_ALG)


def decode_token(token: str) -> Optional[str]:
    """Return the ``sub`` (owner_id) if the JWT is valid and unexpired."""
    try:
        data = jwt.decode(token, _secret(), algorithms=[JWT_ALG])
    except jwt.PyJWTError:
        return None
    return data.get("sub")


# ---------------------------------------------------------------------------
# API-key hashing + lookup
# ---------------------------------------------------------------------------


def hash_key(plaintext: str) -> str:
    """Return the bcrypt hash of an API key. Constant-time comparison."""
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_key(plaintext: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def sha256(plaintext: str) -> str:
    """Deterministic hash for fast index lookup before the bcrypt verify."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


async def lookup_key(plaintext: str) -> Optional[dict]:
    """Return the api_keys doc whose hash matches the plaintext, or None."""
    coll = _db.api_keys()
    if coll is None:
        return None
    doc = await coll.find_one({"key_hash": sha256(plaintext), "revoked": {"$ne": True}})
    if doc is None:
        return None
    if not verify_key(plaintext, doc.get("key_bcrypt", "")):
        return None
    return doc


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_optional_user(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None),
) -> Optional[dict]:
    """Resolve the calling user from ``X-API-Key`` or ``Authorization`` header.

    Returns ``None`` if no key is provided OR the DB is unreachable OR the key
    is invalid. Endpoints can therefore stay public when the DB is offline.
    """
    if not _db.is_available():
        return None

    plaintext: Optional[str] = None
    if x_api_key:
        plaintext = x_api_key.strip()
    elif authorization and authorization.lower().startswith("bearer "):
        plaintext = authorization.split(None, 1)[1].strip()

    if not plaintext:
        return None

    # Allow callers to pass a raw JWT (issued by /auth/*) directly.
    if plaintext.count(".") == 2:
        owner_id = decode_token(plaintext)
        if owner_id:
            users = _db.users()
            doc = await users.find_one({"_id": owner_id})
            if doc is not None:
                return {"user_id": owner_id, "email": doc.get("email", "")}

    doc = await lookup_key(plaintext)
    if doc is None:
        return None
    user = await _db.users().find_one({"_id": doc["owner_id"]})
    return {
        "user_id": doc["owner_id"],
        "email": (user or {}).get("email", ""),
    }


async def require_user(user: Optional[dict] = Depends(get_optional_user)) -> dict:
    """Variant that 401s when no valid key is present."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


# ---------------------------------------------------------------------------
# Credential helpers used by /auth/* endpoints
# ---------------------------------------------------------------------------


def hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plaintext: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False
