"""Auth-related HTTP endpoints.

POST /auth/signup   -> {user_id, email, api_key}
POST /auth/login    -> {user_id, email, api_key}
POST /auth/keys     -> {api_key, expires_at}  (requires existing X-API-Key)
GET  /auth/me       -> {user_id, email}        (requires existing X-API-Key)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import secrets
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from . import auth, db

router = APIRouter(prefix="/auth", tags=["auth"])


class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class IssueKeyIn(BaseModel):
    ttl_days: Optional[int] = Field(default=None, ge=1, le=365)


class _ApiKeyOut(BaseModel):
    user_id: str
    email: str
    api_key: str
    expires_at: datetime


class _MeOut(BaseModel):
    user_id: str
    email: str


def _new_api_key() -> str:
    # 32 bytes -> 43 chars base64url. URL-safe and copy/paste friendly.
    return "gw_" + secrets.token_urlsafe(32)


async def _persist_api_key(owner_id: str, plaintext: str, ttl_days: Optional[int] = None) -> datetime:
    """Insert the api_keys row and return its expiry datetime."""
    if ttl_days is None:
        ttl_days = auth._ttl_days()
    now = datetime.now(tz=timezone.utc)
    expires = now + timedelta(days=ttl_days)
    doc = {
        "owner_id": str(owner_id),
        "key_hash": auth.sha256(plaintext),
        "key_bcrypt": auth.hash_key(plaintext),
        "created_at": now,
        "expires_at": expires,
        "revoked": False,
    }
    await db.api_keys().insert_one(doc)
    return expires


@router.post("/signup", response_model=_ApiKeyOut, status_code=status.HTTP_201_CREATED)
async def signup(body: SignupIn) -> _ApiKeyOut:
    if not db.is_available():
        raise HTTPException(status_code=503, detail="Database unavailable; auth disabled.")
    users = db.users()
    if await users.find_one({"email": body.email}) is not None:
        raise HTTPException(status_code=409, detail="Email already registered.")
    user_id = secrets.token_urlsafe(16)
    await users.insert_one(
        {
            "_id": user_id,
            "email": body.email,
            "password_bcrypt": auth.hash_password(body.password),
            "created_at": datetime.now(tz=timezone.utc),
        }
    )
    plaintext = _new_api_key()
    expires = await _persist_api_key(user_id, plaintext)
    return _ApiKeyOut(user_id=user_id, email=body.email, api_key=plaintext, expires_at=expires)


@router.post("/login", response_model=_ApiKeyOut)
async def login(body: LoginIn) -> _ApiKeyOut:
    if not db.is_available():
        raise HTTPException(status_code=503, detail="Database unavailable; auth disabled.")
    doc = await db.users().find_one({"email": body.email})
    if doc is None or not auth.verify_password(body.password, doc.get("password_bcrypt", "")):
        # Constant-time-ish: hash a dummy to avoid timing side channels.
        auth.hash_password(body.password)
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    plaintext = _new_api_key()
    expires = await _persist_api_key(doc["_id"], plaintext)
    return _ApiKeyOut(user_id=doc["_id"], email=doc["email"], api_key=plaintext, expires_at=expires)


@router.post("/keys", response_model=_ApiKeyOut)
async def issue_key(body: IssueKeyIn, user=Depends(auth.require_user)) -> _ApiKeyOut:
    if not db.is_available():
        raise HTTPException(status_code=503, detail="Database unavailable; auth disabled.")
    plaintext = _new_api_key()
    expires = await _persist_api_key(user["user_id"], plaintext, ttl_days=body.ttl_days)
    return _ApiKeyOut(user_id=user["user_id"], email=user["email"], api_key=plaintext, expires_at=expires)


@router.get("/me", response_model=_MeOut)
async def me(user=Depends(auth.require_user)) -> _MeOut:
    return _MeOut(user_id=user["user_id"], email=user["email"])
