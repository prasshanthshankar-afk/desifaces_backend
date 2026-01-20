from __future__ import annotations

import os
import time
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from app.db import get_pool
from app.security import (
    ACCESS_TTL_SECONDS,
    REFRESH_TTL_SECONDS,
    hash_password,
    hash_refresh_token,
    mint_access_jwt,
    mint_refresh_token,
    verify_password,
)
from app.audit import audit_log

router = APIRouter(prefix="/api/auth", tags=["auth"])


# -------------------------
# Pydantic contracts
# -------------------------
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    full_name: str = Field(default="", max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    device_id: str | None = Field(default=None, max_length=200)
    client_type: str | None = Field(default=None)  # 'web'|'ios'|'android'


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=256)


# -------------------------
# Helpers
# -------------------------
def _req_meta(request: Request) -> tuple[str | None, str | None, str | None]:
    request_id = getattr(request.state, "request_id", None)
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    return request_id, ip, ua


async def _fetch_roles(conn, user_id: str) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT r.role_key
        FROM core.user_roles ur
        JOIN core.roles r ON r.id = ur.role_id
        WHERE ur.user_id = $1
        """,
        UUID(user_id),
    )
    roles = [r["role_key"] for r in rows]
    return roles or ["user"]


# -------------------------
# Routes
# -------------------------
@router.post("/register", response_model=dict)
async def register(req: RegisterRequest, request: Request):
    pool = await get_pool()
    request_id, ip, ua = _req_meta(request)

    async with pool.acquire() as conn:
        email = str(req.email).strip()

        existing = await conn.fetchval(
            "SELECT 1 FROM core.users WHERE lower(email)=lower($1)",
            email,
        )
        if existing:
            await audit_log(
                conn,
                action="auth.register.failed",
                entity_type="auth",
                entity_id=email.lower(),
                actor_user_id=None,
                request_id=request_id,
                ip=ip,
                user_agent=ua,
                after={"reason": "email_already_registered"},
            )
            raise HTTPException(status_code=409, detail="email_already_registered")

        pw_hash = hash_password(req.password)

        row = await conn.fetchrow(
            """
            INSERT INTO core.users(email, password_hash, full_name)
            VALUES ($1, $2, $3)
            RETURNING id::text AS id, email, full_name, tier, is_active
            """,
            email,
            pw_hash,
            req.full_name.strip(),
        )

        await conn.execute(
            """
            INSERT INTO core.user_roles(user_id, role_id)
            SELECT $1::uuid, r.id FROM core.roles r WHERE r.role_key='user'
            ON CONFLICT DO NOTHING
            """,
            row["id"],
        )

        await audit_log(
            conn,
            action="auth.register",
            entity_type="user",
            entity_id=row["id"],
            actor_user_id=row["id"],
            request_id=request_id,
            ip=ip,
            user_agent=ua,
            after={"email": row["email"], "tier": row["tier"]},
        )

        return {
            "id": row["id"],
            "email": row["email"],
            "full_name": row["full_name"],
            "tier": row["tier"],
        }


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, request: Request):
    pool = await get_pool()
    request_id, ip, ua = _req_meta(request)

    async with pool.acquire() as conn:
        email = str(req.email).strip()

        user = await conn.fetchrow(
            """
            SELECT id::text AS id, email, password_hash, tier, is_active
            FROM core.users
            WHERE lower(email)=lower($1)
            """,
            email,
        )

        # record attempt (best-effort)
        try:
            await conn.execute(
                "INSERT INTO core.login_attempts(email_lower, success, ip, user_agent) VALUES (lower($1), $2, $3, $4)",
                email,
                bool(user),
                ip,
                ua,
            )
        except Exception:
            pass

        if not user or not user["is_active"]:
            await audit_log(
                conn,
                action="auth.login.failed",
                entity_type="auth",
                entity_id=email.lower(),
                actor_user_id=None,
                request_id=request_id,
                ip=ip,
                user_agent=ua,
                after={"reason": "invalid_credentials"},
            )
            raise HTTPException(status_code=401, detail="invalid_credentials")

        if not verify_password(req.password, user["password_hash"]):
            try:
                await conn.execute(
                    "INSERT INTO core.login_attempts(email_lower, success, ip, user_agent) VALUES (lower($1), false, $2, $3)",
                    email,
                    ip,
                    ua,
                )
            except Exception:
                pass

            await audit_log(
                conn,
                action="auth.login.failed",
                entity_type="user",
                entity_id=user["id"],
                actor_user_id=user["id"],
                request_id=request_id,
                ip=ip,
                user_agent=ua,
                after={"reason": "invalid_credentials"},
            )
            raise HTTPException(status_code=401, detail="invalid_credentials")

        roles = await _fetch_roles(conn, user["id"])

        access = mint_access_jwt(
            user_id=user["id"],
            email=user["email"],
            tier=user["tier"],
            roles=roles,
        )
        refresh = mint_refresh_token()
        refresh_hash = hash_refresh_token(refresh)

        expires_at = int(time.time()) + REFRESH_TTL_SECONDS
        await conn.execute(
            """
            INSERT INTO core.sessions(user_id, refresh_token_hash, device_id, client_type, expires_at, user_agent, ip)
            VALUES ($1::uuid, $2, $3, $4, to_timestamp($5), $6, $7)
            """,
            user["id"],
            refresh_hash,
            req.device_id,
            req.client_type,
            expires_at,
            ua,
            ip,
        )

        await audit_log(
            conn,
            action="auth.login.success",
            entity_type="session",
            entity_id=refresh_hash,  # hashed only (safe)
            actor_user_id=user["id"],
            request_id=request_id,
            ip=ip,
            user_agent=ua,
            after={"email": user["email"], "tier": user["tier"], "client_type": req.client_type, "device_id": req.device_id},
        )

        return TokenResponse(
            access_token=access,
            expires_in=ACCESS_TTL_SECONDS,
            refresh_token=refresh,
        )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(req: RefreshRequest, request: Request):
    pool = await get_pool()
    request_id, ip, ua = _req_meta(request)

    async with pool.acquire() as conn:
        r_hash = hash_refresh_token(req.refresh_token)

        sess = await conn.fetchrow(
            """
            SELECT s.id::text AS sid,
                   s.user_id::text AS user_id,
                   s.expires_at,
                   s.revoked_at,
                   u.email,
                   u.tier,
                   u.is_active
            FROM core.sessions s
            JOIN core.users u ON u.id = s.user_id
            WHERE s.refresh_token_hash = $1
            """,
            r_hash,
        )

        if not sess or sess["revoked_at"] is not None or not sess["is_active"]:
            await audit_log(
                conn,
                action="auth.refresh.failed",
                entity_type="session",
                entity_id=r_hash,
                actor_user_id=None,
                request_id=request_id,
                ip=ip,
                user_agent=ua,
                after={"reason": "invalid_refresh"},
            )
            raise HTTPException(status_code=401, detail="invalid_refresh")

        if sess["expires_at"].timestamp() < time.time():
            await audit_log(
                conn,
                action="auth.refresh.failed",
                entity_type="session",
                entity_id=sess["sid"],
                actor_user_id=sess["user_id"],
                request_id=request_id,
                ip=ip,
                user_agent=ua,
                after={"reason": "refresh_expired"},
            )
            raise HTTPException(status_code=401, detail="refresh_expired")

        roles = await _fetch_roles(conn, sess["user_id"])

        # Rotate refresh token
        new_refresh = mint_refresh_token()
        new_hash = hash_refresh_token(new_refresh)

        await conn.execute(
            """
            UPDATE core.sessions
            SET refresh_token_hash=$1, last_seen_at=now(), user_agent=$2, ip=$3
            WHERE id = $4::uuid
            """,
            new_hash,
            ua,
            ip,
            sess["sid"],
        )

        access = mint_access_jwt(
            user_id=sess["user_id"],
            email=sess["email"],
            tier=sess["tier"],
            roles=roles,
        )

        await audit_log(
            conn,
            action="auth.refresh",
            entity_type="session",
            entity_id=sess["sid"],
            actor_user_id=sess["user_id"],
            request_id=request_id,
            ip=ip,
            user_agent=ua,
            after={"rotated": True},
        )

        return TokenResponse(
            access_token=access,
            expires_in=ACCESS_TTL_SECONDS,
            refresh_token=new_refresh,
        )


@router.post("/logout", response_model=dict)
async def logout(req: LogoutRequest, request: Request):
    pool = await get_pool()
    request_id, ip, ua = _req_meta(request)

    async with pool.acquire() as conn:
        r_hash = hash_refresh_token(req.refresh_token)

        # Lookup session (so audit includes actor_user_id safely)
        sess = await conn.fetchrow(
            """
            SELECT id::text AS sid, user_id::text AS user_id, revoked_at
            FROM core.sessions
            WHERE refresh_token_hash = $1
            """,
            r_hash,
        )

        await conn.execute(
            """
            UPDATE core.sessions
            SET revoked_at = now()
            WHERE refresh_token_hash = $1
            """,
            r_hash,
        )

        await audit_log(
            conn,
            action="auth.logout",
            entity_type="session",
            entity_id=sess["sid"] if sess else r_hash,
            actor_user_id=sess["user_id"] if sess else None,
            request_id=request_id,
            ip=ip,
            user_agent=ua,
            after={"revoked": True},
        )

        return {"ok": True}


@router.post("/forgot-password", response_model=dict)
async def forgot_password(req: ForgotPasswordRequest, request: Request):
    """
    Phase-1: generate reset token and store hashed.
    You will later send email/SMS using a notification service.
    For now: return ok always (avoid account enumeration).
    """
    pool = await get_pool()
    request_id, ip, ua = _req_meta(request)

    async with pool.acquire() as conn:
        email = str(req.email).strip()

        user_id = await conn.fetchval(
            "SELECT id::text FROM core.users WHERE lower(email)=lower($1)",
            email,
        )

        # Always return ok (anti-enumeration)
        if not user_id:
            await audit_log(
                conn,
                action="auth.forgot_password",
                entity_type="auth",
                entity_id=email.lower(),
                actor_user_id=None,
                request_id=request_id,
                ip=ip,
                user_agent=ua,
                after={"result": "ok_no_user"},
            )
            return {"ok": True}

        raw = __import__("secrets").token_urlsafe(32)
        token_hash = hash_refresh_token(raw)  # reuse HMAC secret for reset token hashing
        expires_at = int(time.time()) + 3600  # 1 hour

        await conn.execute(
            """
            INSERT INTO core.password_reset_tokens(user_id, token_hash, expires_at, request_ip, request_user_agent)
            VALUES ($1::uuid, $2, to_timestamp($3), $4, $5)
            """,
            user_id,
            token_hash,
            expires_at,
            ip,
            ua,
        )

        await audit_log(
            conn,
            action="auth.forgot_password",
            entity_type="user",
            entity_id=user_id,
            actor_user_id=user_id,
            request_id=request_id,
            ip=ip,
            user_agent=ua,
            after={"issued": True},
        )

        # Dev-only return
        if os.getenv("RETURN_RESET_TOKEN_FOR_DEV", "0") == "1":
            return {"ok": True, "dev_reset_token": raw}

        return {"ok": True}


@router.post("/reset-password", response_model=dict)
async def reset_password(req: ResetPasswordRequest, request: Request):
    pool = await get_pool()
    request_id, ip, ua = _req_meta(request)

    async with pool.acquire() as conn:
        token_hash = hash_refresh_token(req.token)

        row = await conn.fetchrow(
            """
            SELECT id::text AS id, user_id::text AS user_id, expires_at, used_at
            FROM core.password_reset_tokens
            WHERE token_hash = $1
            """,
            token_hash,
        )
        if not row or row["used_at"] is not None:
            await audit_log(
                conn,
                action="auth.reset_password.failed",
                entity_type="password_reset",
                entity_id=token_hash,
                actor_user_id=None,
                request_id=request_id,
                ip=ip,
                user_agent=ua,
                after={"reason": "invalid_or_used_token"},
            )
            raise HTTPException(status_code=400, detail="invalid_or_used_token")

        if row["expires_at"].timestamp() < time.time():
            await audit_log(
                conn,
                action="auth.reset_password.failed",
                entity_type="password_reset",
                entity_id=row["id"],
                actor_user_id=row["user_id"],
                request_id=request_id,
                ip=ip,
                user_agent=ua,
                after={"reason": "token_expired"},
            )
            raise HTTPException(status_code=400, detail="token_expired")

        new_hash = hash_password(req.new_password)

        await conn.execute(
            "UPDATE core.users SET password_hash=$1, updated_at=now() WHERE id=$2::uuid",
            new_hash,
            row["user_id"],
        )
        await conn.execute(
            "UPDATE core.password_reset_tokens SET used_at=now() WHERE id=$1::uuid",
            row["id"],
        )

        # Revoke all sessions for safety
        await conn.execute(
            "UPDATE core.sessions SET revoked_at=now() WHERE user_id=$1::uuid AND revoked_at IS NULL",
            row["user_id"],
        )

        await audit_log(
            conn,
            action="auth.reset_password",
            entity_type="user",
            entity_id=row["user_id"],
            actor_user_id=row["user_id"],
            request_id=request_id,
            ip=ip,
            user_agent=ua,
            after={"sessions_revoked": True},
        )

        return {"ok": True}