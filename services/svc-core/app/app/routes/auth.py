from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import secrets
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field

from app.audit import audit_log
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

from app.services.notification_service import NotificationService
from app.schemas.notifications import InternalNotificationEventCreate, InternalRecipient

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)

_ALLOWED_CLIENT_TYPES = ("web", "ios", "android")
_ALLOWED_EMAIL_CHALLENGE_PURPOSES = {
    "register_verify",
    "password_change",
    "password_reset",
}
_bearer = HTTPBearer(auto_error=True)


async def _emit_account_notification_best_effort(_conn_unused, *, req: InternalNotificationEventCreate, log_context: dict) -> None:
    try:
        pool = await get_pool()
        svc = NotificationService(pool)
    except Exception:
        logger.exception("auth_notification_service_init_failed", extra=log_context)
        return
    try:
        await svc.emit_internal_event(req=req)
    except Exception:
        logger.exception("auth_notification_emit_failed", extra=log_context)


async def _failed_login_count(conn, *, email: str, window_minutes: int = 30) -> int:
    try:
        value = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM core.login_attempts
            WHERE email_lower = lower($1)
              AND success = false
              AND created_at >= now() - make_interval(mins => $2)
            """,
            email,
            int(window_minutes),
        )
        return int(value or 0)
    except Exception:
        return 0


# -------------------------
# Pydantic contracts
# -------------------------
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    full_name: str = Field(default="", max_length=200)


class VerifyRegisterEmailRequest(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=4, max_length=12)
    device_id: str | None = Field(default=None, max_length=200)
    client_type: str | None = Field(default=None)


class ResendRegisterEmailCodeRequest(BaseModel):
    email: EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    device_id: str | None = Field(default=None, max_length=200)
    client_type: str | None = Field(default=None)  # 'web'|'ios'|'android'


class AuthUser(BaseModel):
    id: str
    email: EmailStr
    full_name: str = ""
    tier: str | None = None
    is_active: bool = True
    roles: list[str] = Field(default_factory=list)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str
    user: AuthUser | None = None


class RegisterPendingResponse(BaseModel):
    id: str
    email: EmailStr
    full_name: str = ""
    tier: str | None = None
    status: str
    next_step: str
    challenge_id: str
    expires_in: int
    resend_after_seconds: int
    dev_email_otp_code: str | None = None


class VerifyRegisterEmailResponse(TokenResponse):
    email_verified: bool = True


class MeResponse(AuthUser):
    pass


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class PasswordResetStartRequest(BaseModel):
    email: EmailStr


class PasswordResetStartResponse(BaseModel):
    ok: bool = True
    status: str = "otp_sent"
    challenge_id: str
    expires_in: int
    resend_after_seconds: int
    dev_email_otp_code: str | None = None


class PasswordResetConfirmRequest(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=4, max_length=12)
    new_password: str = Field(min_length=8, max_length=256)


class ResetPasswordRequest(BaseModel):
    # Legacy link-token reset fallback. Primary mobile/web UX uses
    # /password/reset/start + /password/reset/confirm.
    token: str
    new_password: str = Field(min_length=8, max_length=256)


class ChangePasswordStartRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)


class ChangePasswordStartResponse(BaseModel):
    ok: bool = True
    status: str = "otp_sent"
    challenge_id: str
    expires_in: int
    resend_after_seconds: int
    dev_email_otp_code: str | None = None


class ChangePasswordConfirmRequest(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=4, max_length=12)
    new_password: str = Field(min_length=8, max_length=256)


# -------------------------
# Helpers
# -------------------------
def _req_meta(request: Request) -> tuple[str | None, str | None, str | None]:
    request_id = getattr(request.state, "request_id", None)
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    return request_id, ip, ua



def _normalize_client_type(v: str | None) -> str:
    """
    core.sessions has CHECK constraint allowing only: web | ios | android.
    Normalize any missing/unknown/internal values to 'ios' (safe default for service callers too).
    """
    s = (v or "").strip().lower()
    if s not in _ALLOWED_CLIENT_TYPES:
        return "ios"
    return s



def _normalize_device_id(v: str | None) -> str | None:
    s = (v or "").strip()
    return s if s else None


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


async def _build_auth_user(conn, user_id: str) -> AuthUser:
    row = await conn.fetchrow(
        """
        SELECT id::text AS id, email, full_name, tier, is_active
        FROM core.users
        WHERE id = $1::uuid
        """,
        user_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="user_not_found")

    roles = await _fetch_roles(conn, row["id"])
    return AuthUser(
        id=row["id"],
        email=row["email"],
        full_name=(row["full_name"] or "").strip(),
        tier=row["tier"],
        is_active=bool(row["is_active"]),
        roles=roles,
    )



def _decode_access_claims(token: str) -> dict:
    """
    Best-effort access token verification with minimal assumptions about app.security.
    Tries existing security helpers first, then common JWT library fallbacks.
    """
    security_mod = importlib.import_module("app.security")

    # Preferred: reuse the project's existing access-token decoder/verifier if present.
    for helper_name in (
        "decode_access_jwt",
        "verify_access_jwt",
        "decode_access_token",
        "verify_access_token",
        "decode_jwt",
        "verify_jwt",
    ):
        helper = getattr(security_mod, helper_name, None)
        if callable(helper):
            payload = helper(token)
            if isinstance(payload, dict):
                return payload
            if hasattr(payload, "dict"):
                return payload.dict()
            if hasattr(payload, "claims") and isinstance(payload.claims, dict):
                return payload.claims
            raise HTTPException(status_code=401, detail="invalid_token")

    # Fallback: decode using a common shared secret/env if the project exposes one.
    secret = (
        getattr(security_mod, "ACCESS_JWT_SECRET", None)
        or getattr(security_mod, "JWT_SECRET", None)
        or getattr(security_mod, "SECRET_KEY", None)
        or os.getenv("ACCESS_JWT_SECRET")
        or os.getenv("JWT_SECRET")
        or os.getenv("SECRET_KEY")
    )
    algorithm = (
        getattr(security_mod, "JWT_ALGORITHM", None)
        or getattr(security_mod, "ALGORITHM", None)
        or os.getenv("JWT_ALGORITHM")
        or os.getenv("ALGORITHM")
        or "HS256"
    )

    if not secret:
        raise HTTPException(status_code=500, detail="access_token_decoder_not_configured")

    for lib_name in ("jose", "jwt"):
        try:
            if lib_name == "jose":
                from jose import jwt as jwt_lib  # type: ignore
            else:
                import jwt as jwt_lib  # type: ignore

            payload = jwt_lib.decode(token, secret, algorithms=[algorithm])
            if isinstance(payload, dict):
                return payload
        except HTTPException:
            raise
        except Exception:
            continue

    raise HTTPException(status_code=401, detail="invalid_token")



def _pricing_bootstrap_url() -> str | None:
    raw = (
        os.getenv("DF_PRICING_INTERNAL_URL")
        or os.getenv("PRICING_INTERNAL_URL")
        or os.getenv("DF_PRICING_BASE_URL")
        or os.getenv("PRICING_BASE_URL")
        or ""
    ).strip().rstrip("/")
    if not raw:
        return None
    return f"{raw}/api/pricing/bootstrap/free-user"



def _pricing_bootstrap_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("DF_PRICING_BOOTSTRAP_TIMEOUT_SECONDS", "5")))
    except Exception:
        return 5.0



def _normalize_tier_text(value: str | None) -> str:
    return str(value or "").strip().lower()



def _should_try_free_pricing_bootstrap(tier: str | None) -> bool:
    normalized = _normalize_tier_text(tier)
    return normalized in {"", "free"}


async def _best_effort_bootstrap_pricing_for_user(
    *,
    user_id: str,
    email: str | None,
    tier: str | None,
    source: str,
) -> None:
    if not user_id or not _should_try_free_pricing_bootstrap(tier):
        return

    url = _pricing_bootstrap_url()
    token = (
        os.getenv("DF_PRICING_INTERNAL_BEARER")
        or os.getenv("SVC_TO_SVC_BEARER")
        or ""
    ).strip()

    if not url or not token:
        logger.warning(
            "pricing bootstrap skipped for user %s: missing url or internal bearer",
            user_id,
        )
        return

    payload = json.dumps(
        {
            "user_id": user_id,
            "email": email,
            "source": source,
        }
    ).encode("utf-8")

    timeout_s = _pricing_bootstrap_timeout_seconds()

    def _send() -> tuple[int, str]:
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "X-User-Id": user_id,
                "X-Service-Name": "svc-core",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return int(getattr(resp, "status", 200) or 200), body

    try:
        status_code, body = await asyncio.to_thread(_send)
        if status_code < 200 or status_code >= 300:
            logger.warning(
                "pricing bootstrap returned non-2xx for user %s: status=%s body=%s",
                user_id,
                status_code,
                body,
            )
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        logger.warning(
            "pricing bootstrap HTTP error for user %s: status=%s body=%s",
            user_id,
            getattr(e, "code", "?"),
            body,
        )
    except urllib.error.URLError as e:
        logger.warning("pricing bootstrap URL error for user %s: %s", user_id, e)
    except Exception as e:
        logger.warning("pricing bootstrap after auth failed for user %s: %s", user_id, e)



def _email_otp_ttl_seconds() -> int:
    try:
        return max(120, int(os.getenv("AUTH_EMAIL_OTP_TTL_SECONDS", "600")))
    except Exception:
        return 600


def _password_reset_otp_ttl_seconds() -> int:
    # Password reset should be short-lived but still usable on mobile where
    # users switch between Mail/Gmail and the app. Default: 5 minutes.
    try:
        return max(30, int(os.getenv("AUTH_PASSWORD_RESET_OTP_TTL_SECONDS", "300")))
    except Exception:
        return 300


def _email_challenge_ttl_seconds(purpose: str) -> int:
    if purpose == "password_reset":
        return _password_reset_otp_ttl_seconds()
    return _email_otp_ttl_seconds()



def _email_otp_max_attempts() -> int:
    try:
        return max(1, int(os.getenv("AUTH_EMAIL_OTP_MAX_ATTEMPTS", "5")))
    except Exception:
        return 5



def _email_otp_resend_cooldown_seconds() -> int:
    try:
        return max(15, int(os.getenv("AUTH_EMAIL_OTP_RESEND_COOLDOWN_SECONDS", "60")))
    except Exception:
        return 60



def _generate_email_otp_code() -> str:
    return f"{secrets.randbelow(1000000):06d}"



def _mask_email(email: str) -> str:
    value = (email or "").strip()
    if not value or "@" not in value:
        return "your email"
    local, domain = value.split("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "*" * max(0, len(local) - 1)
    else:
        masked_local = local[0] + ("*" * (len(local) - 2)) + local[-1]
    return f"{masked_local}@{domain}"



def _email_sender() -> str:
    return (
        os.getenv("DF_EMAIL_FROM")
        or os.getenv("EMAIL_FROM")
        or os.getenv("DF_SMTP_FROM")
        or os.getenv("SMTP_FROM")
        or os.getenv("DF_SMTP_USERNAME")
        or os.getenv("SMTP_USERNAME")
        or "noreply@desifaces.ai"
    ).strip()



def _email_subject_and_body(*, purpose: str, code: str, email: str) -> tuple[str, str]:
    if purpose == "register_verify":
        subject = "Verify your desifaces.ai email"
        body = (
            f"Welcome to desifaces.ai.\n\n"
            f"Use this verification code to activate your account: {code}\n\n"
            f"This code expires in {_email_otp_ttl_seconds() // 60} minutes.\n"
            f"If you did not start this signup, you can ignore this email."
        )
        return subject, body

    if purpose == "password_change":
        subject = "Confirm your desifaces.ai password change"
        body = (
            f"A password change was requested for your desifaces.ai account ({_mask_email(email)}).\n\n"
            f"Use this code to continue: {code}\n\n"
            f"This code expires in {_email_otp_ttl_seconds() // 60} minutes.\n"
            f"If you did not request this, do not share this code and review your account immediately."
        )
        return subject, body

    if purpose == "password_reset":
        subject = "Reset your desifaces.ai password"
        body = (
            f"A password reset was requested for your desifaces.ai account ({_mask_email(email)}).\n\n"
            f"Use this verification code in the DesiFaces app or web app: {code}\n\n"
            f"This code expires in {_email_challenge_ttl_seconds(purpose) // 60} minutes.\n"
            f"If you did not request this, you can ignore this email. Do not share this code with anyone."
        )
        return subject, body

    raise ValueError(f"unsupported_email_challenge_purpose:{purpose}")



def _smtp_config() -> dict:
    host = (os.getenv("DF_SMTP_HOST") or os.getenv("SMTP_HOST") or "").strip()
    username = (os.getenv("DF_SMTP_USERNAME") or os.getenv("SMTP_USERNAME") or "").strip()
    password = (os.getenv("DF_SMTP_PASSWORD") or os.getenv("SMTP_PASSWORD") or "").strip()
    port_raw = (os.getenv("DF_SMTP_PORT") or os.getenv("SMTP_PORT") or "587").strip()
    use_ssl = str(os.getenv("DF_SMTP_USE_SSL") or os.getenv("SMTP_USE_SSL") or "0").strip() in {"1", "true", "yes"}
    use_tls = str(os.getenv("DF_SMTP_USE_TLS") or os.getenv("SMTP_USE_TLS") or "1").strip() not in {"0", "false", "no"}
    try:
        port = int(port_raw)
    except Exception:
        port = 587
    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "use_ssl": use_ssl,
        "use_tls": use_tls,
    }



def _send_email_sync(*, to_email: str, subject: str, body: str) -> None:
    cfg = _smtp_config()
    if not cfg["host"]:
        raise RuntimeError("smtp_not_configured")

    msg = EmailMessage()
    msg["From"] = _email_sender()
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    ehlo_domain = (
        os.getenv("DF_SMTP_EHLO_DOMAIN")
        or os.getenv("SMTP_EHLO_DOMAIN")
        or "mail.desifaces.ai"
    ).strip()
    try:
        timeout_s = max(5, int(os.getenv("DF_SMTP_TIMEOUT_SEC", "20")))
    except Exception:
        timeout_s = 20

    logger.warning(
        "auth_smtp_send_begin host=%s port=%s use_tls=%s use_ssl=%s username_present=%s from=%s to=%s subject=%s ehlo=%s",
        cfg["host"],
        cfg["port"],
        bool(cfg["use_tls"]),
        bool(cfg["use_ssl"]),
        bool(cfg["username"]),
        msg["From"],
        to_email,
        subject,
        ehlo_domain,
    )

    context = ssl.create_default_context()
    send_result = None
    if cfg["use_ssl"]:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=context, timeout=timeout_s) as server:
            server.ehlo(ehlo_domain)
            if cfg["username"]:
                server.login(cfg["username"], cfg["password"])
            send_result = server.send_message(msg)
        logger.warning(
            "auth_smtp_send_complete to=%s subject=%s result=%s",
            to_email,
            subject,
            send_result,
        )
        return

    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=timeout_s) as server:
        server.ehlo(ehlo_domain)
        if cfg["use_tls"]:
            server.starttls(context=context)
            server.ehlo(ehlo_domain)
        if cfg["username"]:
            server.login(cfg["username"], cfg["password"])
        send_result = server.send_message(msg)

    logger.warning(
        "auth_smtp_send_complete to=%s subject=%s result=%s",
        to_email,
        subject,
        send_result,
    )


async def _send_email_code_best_effort(*, email: str, purpose: str, code: str, log_context: dict) -> bool:
    subject, body = _email_subject_and_body(purpose=purpose, code=code, email=email)

    logger.warning(
        "auth_email_code_send_attempt purpose=%s email=%s challenge_id=%s",
        purpose,
        email,
        (log_context or {}).get("challenge_id"),
    )
    try:
        await asyncio.to_thread(_send_email_sync, to_email=email, subject=subject, body=body)
        logger.warning(
            "auth_email_code_send_success purpose=%s email=%s challenge_id=%s",
            purpose,
            email,
            (log_context or {}).get("challenge_id"),
        )
        return True
    except Exception:
        logger.exception("auth_email_code_send_failed", extra=log_context)
        return False



def _dev_return_email_otp_code() -> bool:
    return str(os.getenv("RETURN_EMAIL_OTP_FOR_DEV", "0")).strip() in {"1", "true", "yes"}


async def _latest_pending_email_challenge(conn, *, email: str, purpose: str):
    if purpose not in _ALLOWED_EMAIL_CHALLENGE_PURPOSES:
        raise ValueError(f"unsupported_email_challenge_purpose:{purpose}")

    return await conn.fetchrow(
        """
        SELECT id::text AS id, user_id::text AS user_id, email, purpose, code_hash, status,
               attempt_count, expires_at, consumed_at, created_at
        FROM core.auth_email_challenges
        WHERE lower(email) = lower($1)
          AND purpose = $2
          AND status = 'pending'
          AND consumed_at IS NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        email,
        purpose,
    )


async def _issue_email_challenge(
    conn,
    *,
    user_id: str | None,
    email: str,
    purpose: str,
    ip: str | None,
    ua: str | None,
) -> tuple[dict, str]:
    if purpose not in _ALLOWED_EMAIL_CHALLENGE_PURPOSES:
        raise HTTPException(status_code=400, detail="unsupported_email_challenge_purpose")

    latest = await _latest_pending_email_challenge(conn, email=email, purpose=purpose)
    resend_after_seconds = _email_otp_resend_cooldown_seconds()
    if latest:
        elapsed = max(0, int(time.time() - latest["created_at"].timestamp()))
        if elapsed < resend_after_seconds:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "email_otp_cooldown",
                    "retry_after_seconds": resend_after_seconds - elapsed,
                },
            )

    await conn.execute(
        """
        UPDATE core.auth_email_challenges
        SET status = CASE WHEN status = 'pending' THEN 'expired' ELSE status END
        WHERE lower(email) = lower($1)
          AND purpose = $2
          AND status = 'pending'
          AND consumed_at IS NULL
        """,
        email,
        purpose,
    )

    raw_code = _generate_email_otp_code()
    code_hash = hash_refresh_token(raw_code)
    ttl_seconds = _email_challenge_ttl_seconds(purpose)

    row = await conn.fetchrow(
        """
        INSERT INTO core.auth_email_challenges(
            user_id,
            email,
            purpose,
            code_hash,
            status,
            attempt_count,
            expires_at,
            consumed_at,
            created_at,
            request_ip,
            request_user_agent
        )
        VALUES (
            $1::uuid,
            $2,
            $3,
            $4,
            'pending',
            0,
            to_timestamp($5),
            NULL,
            now(),
            $6,
            $7
        )
        RETURNING id::text AS id, user_id::text AS user_id, email, purpose, code_hash, status,
                  attempt_count, expires_at, consumed_at, created_at
        """,
        user_id,
        email,
        purpose,
        code_hash,
        int(time.time()) + ttl_seconds,
        ip,
        ua,
    )
    if not row:
        raise HTTPException(status_code=500, detail="email_challenge_create_failed")

    return dict(row), raw_code


async def _verify_email_challenge(
    conn,
    *,
    challenge_id: str,
    purpose: str,
    code: str,
    user_id: str | None = None,
    email: str | None = None,
):
    if purpose not in _ALLOWED_EMAIL_CHALLENGE_PURPOSES:
        raise HTTPException(status_code=400, detail="unsupported_email_challenge_purpose")

    # Never let malformed client input reach Postgres UUID casting.
    # Invalid/no-op challenge ids should behave like a normal invalid OTP challenge,
    # not crash the route with a 500.
    try:
        challenge_uuid = str(UUID(str(challenge_id or "").strip()))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_email_otp_challenge")

    row = await conn.fetchrow(
        """
        SELECT id::text AS id, user_id::text AS user_id, email, purpose, code_hash, status,
               attempt_count, expires_at, consumed_at, created_at
        FROM core.auth_email_challenges
        WHERE id = $1::uuid
          AND purpose = $2
        """,
        challenge_uuid,
        purpose,
    )
    if not row:
        raise HTTPException(status_code=400, detail="invalid_email_otp_challenge")

    if user_id and str(row["user_id"] or "") != str(user_id):
        raise HTTPException(status_code=403, detail="email_otp_challenge_user_mismatch")

    if email and str(row["email"] or "").strip().lower() != str(email).strip().lower():
        raise HTTPException(status_code=400, detail="email_otp_challenge_email_mismatch")

    if row["status"] != "pending" or row["consumed_at"] is not None:
        raise HTTPException(status_code=400, detail="invalid_or_used_email_otp")

    if row["expires_at"].timestamp() < time.time():
        await conn.execute(
            "UPDATE core.auth_email_challenges SET status = 'expired' WHERE id = $1::uuid",
            row["id"],
        )
        raise HTTPException(status_code=400, detail="email_otp_expired")

    max_attempts = _email_otp_max_attempts()
    next_attempt_count = int(row["attempt_count"] or 0) + 1
    provided_hash = hash_refresh_token((code or "").strip())
    if provided_hash != row["code_hash"]:
        next_status = "locked" if next_attempt_count >= max_attempts else "pending"
        await conn.execute(
            """
            UPDATE core.auth_email_challenges
            SET attempt_count = $1,
                status = $2
            WHERE id = $3::uuid
            """,
            next_attempt_count,
            next_status,
            row["id"],
        )
        if next_status == "locked":
            raise HTTPException(status_code=429, detail="email_otp_max_attempts_exceeded")
        raise HTTPException(status_code=400, detail="invalid_email_otp")

    await conn.execute(
        """
        UPDATE core.auth_email_challenges
        SET status = 'consumed',
            consumed_at = now(),
            attempt_count = $1
        WHERE id = $2::uuid
        """,
        next_attempt_count,
        row["id"],
    )
    return row


async def _issue_login_tokens(
    conn,
    *,
    user_id: str,
    email: str,
    full_name: str,
    tier: str | None,
    is_active: bool,
    request_id: str | None,
    ip: str | None,
    ua: str | None,
    device_id: str | None,
    client_type: str | None,
) -> TokenResponse:
    roles = await _fetch_roles(conn, user_id)

    access = mint_access_jwt(
        user_id=user_id,
        email=email,
        tier=tier,
        roles=roles,
    )
    refresh = mint_refresh_token()
    refresh_hash = hash_refresh_token(refresh)

    normalized_device_id = _normalize_device_id(device_id)
    normalized_client_type = _normalize_client_type(client_type)

    expires_at = int(time.time()) + REFRESH_TTL_SECONDS
    await conn.execute(
        """
        INSERT INTO core.sessions(user_id, refresh_token_hash, device_id, client_type, expires_at, user_agent, ip)
        VALUES ($1::uuid, $2, $3, $4, to_timestamp($5), $6, $7)
        """,
        user_id,
        refresh_hash,
        normalized_device_id,
        normalized_client_type,
        expires_at,
        ua,
        ip,
    )

    await audit_log(
        conn,
        action="auth.login.success",
        entity_type="session",
        entity_id=refresh_hash,
        actor_user_id=user_id,
        request_id=request_id,
        ip=ip,
        user_agent=ua,
        after={
            "email": email,
            "tier": tier,
            "client_type": normalized_client_type,
            "device_id": normalized_device_id,
        },
    )

    return TokenResponse(
        access_token=access,
        expires_in=ACCESS_TTL_SECONDS,
        refresh_token=refresh,
        user=AuthUser(
            id=user_id,
            email=email,
            full_name=(full_name or "").strip(),
            tier=tier,
            is_active=bool(is_active),
            roles=roles,
        ),
    )


async def _authenticate_bearer_user(
    request: Request,
    creds: HTTPAuthorizationCredentials,
) -> tuple[dict, str, str | None, str | None, str | None]:
    payload = _decode_access_claims(creds.credentials)
    user_id = str(payload.get("sub") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid_token")
    request_id, ip, ua = _req_meta(request)
    return payload, user_id, request_id, ip, ua


# -------------------------
# Routes
# -------------------------
@router.post("/register", response_model=RegisterPendingResponse)
async def register(req: RegisterRequest, request: Request):
    pool = await get_pool()
    request_id, ip, ua = _req_meta(request)

    async with pool.acquire() as conn:
        email = str(req.email).strip()

        existing = await conn.fetchrow(
            "SELECT id::text AS id, email, is_active, tier, full_name FROM core.users WHERE lower(email)=lower($1)",
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
            INSERT INTO core.users(email, password_hash, full_name, is_active)
            VALUES ($1, $2, $3, false)
            RETURNING id::text AS id, email, full_name, tier, is_active
            """,
            email,
            pw_hash,
            req.full_name.strip(),
        )
        if not row:
            raise HTTPException(status_code=500, detail="register_failed")

        await conn.execute(
            """
            INSERT INTO core.user_roles(user_id, role_id)
            SELECT $1::uuid, r.id FROM core.roles r WHERE r.role_key='user'
            ON CONFLICT DO NOTHING
            """,
            row["id"],
        )

        challenge, raw_code = await _issue_email_challenge(
            conn,
            user_id=row["id"],
            email=row["email"],
            purpose="register_verify",
            ip=ip,
            ua=ua,
        )

        await audit_log(
            conn,
            action="auth.register.pending_email_verification",
            entity_type="user",
            entity_id=row["id"],
            actor_user_id=row["id"],
            request_id=request_id,
            ip=ip,
            user_agent=ua,
            after={"email": row["email"], "tier": row["tier"], "challenge_id": challenge["id"]},
        )

        send_ok = await _send_email_code_best_effort(
            email=row["email"],
            purpose="register_verify",
            code=raw_code,
            log_context={"user_id": row["id"], "email": row["email"], "purpose": "register_verify"},
        )
        if not send_ok and not _dev_return_email_otp_code():
            raise HTTPException(status_code=500, detail="verification_email_send_failed")

        response_payload = RegisterPendingResponse(
            id=row["id"],
            email=row["email"],
            full_name=row["full_name"] or "",
            tier=row["tier"],
            status="pending_email_verification",
            next_step="verify_email",
            challenge_id=challenge["id"],
            expires_in=_email_challenge_ttl_seconds("register_verify"),
            resend_after_seconds=_email_otp_resend_cooldown_seconds(),
            dev_email_otp_code=raw_code if _dev_return_email_otp_code() else None,
        )

        return response_payload


@router.post("/register/verify-email", response_model=VerifyRegisterEmailResponse)
async def verify_register_email(req: VerifyRegisterEmailRequest, request: Request):
    pool = await get_pool()
    request_id, ip, ua = _req_meta(request)

    async with pool.acquire() as conn:
        challenge = await _verify_email_challenge(
            conn,
            challenge_id=req.challenge_id,
            purpose="register_verify",
            code=req.code,
        )
        user_id = str(challenge["user_id"] or "").strip()
        if not user_id:
            raise HTTPException(status_code=400, detail="email_otp_challenge_missing_user")

        user = await conn.fetchrow(
            """
            SELECT id::text AS id, email, full_name, tier, is_active
            FROM core.users
            WHERE id = $1::uuid
            """,
            user_id,
        )
        if not user:
            raise HTTPException(status_code=404, detail="user_not_found")

        await conn.execute(
            "UPDATE core.users SET is_active = true, updated_at = now(), email_verified_at = now() WHERE id = $1::uuid",
            user_id,
        )

        await audit_log(
            conn,
            action="auth.register.email_verified",
            entity_type="user",
            entity_id=user_id,
            actor_user_id=user_id,
            request_id=request_id,
            ip=ip,
            user_agent=ua,
            after={"email": user["email"], "challenge_id": req.challenge_id},
        )

        token_response = await _issue_login_tokens(
            conn,
            user_id=user_id,
            email=user["email"],
            full_name=user["full_name"] or "",
            tier=user["tier"],
            is_active=True,
            request_id=request_id,
            ip=ip,
            ua=ua,
            device_id=req.device_id,
            client_type=req.client_type,
        )

        bootstrap_user_id = user_id
        bootstrap_email = user["email"]
        bootstrap_tier = user["tier"]

    await _best_effort_bootstrap_pricing_for_user(
        user_id=bootstrap_user_id,
        email=bootstrap_email,
        tier=bootstrap_tier,
        source="svc_core_register_verify_email",
    )

    await _emit_account_notification_best_effort(
        None,
        req=InternalNotificationEventCreate(
            event_type="USER_REGISTERED",
            category="account",
            priority="info",
            source_service="svc-core",
            source_ref_type="user",
            source_ref_id=str(bootstrap_user_id),
            actor_user_id=str(bootstrap_user_id),
            title="Welcome to desifaces.ai",
            body="Your account has been verified and activated successfully.",
            action_route="/notifications",
            action_label="Open account",
            image_url=None,
            payload_json={"user_id": str(bootstrap_user_id), "email": bootstrap_email},
            metadata_json={"user_id": str(bootstrap_user_id), "email": bootstrap_email},
            dedupe_key=f"user-registered:{bootstrap_user_id}",
            recipients=[InternalRecipient(user_id=str(bootstrap_user_id))],
        ),
        log_context={"user_id": str(bootstrap_user_id), "event_type": "USER_REGISTERED"},
    )

    return VerifyRegisterEmailResponse(
        access_token=token_response.access_token,
        token_type=token_response.token_type,
        expires_in=token_response.expires_in,
        refresh_token=token_response.refresh_token,
        user=token_response.user,
        email_verified=True,
    )


@router.post("/register/resend-email-code", response_model=dict)
async def resend_register_email_code(req: ResendRegisterEmailCodeRequest, request: Request):
    pool = await get_pool()
    request_id, ip, ua = _req_meta(request)
    email = str(req.email).strip()

    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT id::text AS id, email, is_active
            FROM core.users
            WHERE lower(email) = lower($1)
            """,
            email,
        )
        if not user:
            await audit_log(
                conn,
                action="auth.register.resend_email_code.no_user",
                entity_type="auth",
                entity_id=email.lower(),
                actor_user_id=None,
                request_id=request_id,
                ip=ip,
                user_agent=ua,
                after={"result": "ok_no_user"},
            )
            return {"ok": True}

        if bool(user["is_active"]):
            return {"ok": True, "status": "already_verified"}

        challenge, raw_code = await _issue_email_challenge(
            conn,
            user_id=user["id"],
            email=user["email"],
            purpose="register_verify",
            ip=ip,
            ua=ua,
        )

        await audit_log(
            conn,
            action="auth.register.resend_email_code",
            entity_type="user",
            entity_id=user["id"],
            actor_user_id=user["id"],
            request_id=request_id,
            ip=ip,
            user_agent=ua,
            after={"challenge_id": challenge["id"]},
        )

        send_ok = await _send_email_code_best_effort(
            email=user["email"],
            purpose="register_verify",
            code=raw_code,
            log_context={"user_id": user["id"], "email": user["email"], "purpose": "register_verify"},
        )
        if not send_ok and not _dev_return_email_otp_code():
            raise HTTPException(status_code=500, detail="verification_email_send_failed")

        return {
            "ok": True,
            "status": "otp_sent",
            "challenge_id": challenge["id"],
            "expires_in": _email_otp_ttl_seconds(),
            "resend_after_seconds": _email_otp_resend_cooldown_seconds(),
            "dev_email_otp_code": raw_code if _dev_return_email_otp_code() else None,
        }


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, request: Request):
    pool = await get_pool()
    request_id, ip, ua = _req_meta(request)

    async with pool.acquire() as conn:
        email = str(req.email).strip()

        user = await conn.fetchrow(
            """
            SELECT id::text AS id, email, full_name, password_hash, tier, is_active
            FROM core.users
            WHERE lower(email)=lower($1)
            """,
            email,
        )

        if not user:
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
            failed_count = await _failed_login_count(conn, email=email)
            if failed_count >= 5:
                bucket = int(time.time() // 1800)
                await _emit_account_notification_best_effort(
                    None,
                    req=InternalNotificationEventCreate(
                        event_type="INVALID_LOGIN_THRESHOLD_REACHED",
                        category="account",
                        priority="important",
                        source_service="svc-core",
                        source_ref_type="user",
                        source_ref_id=str(user["id"]),
                        actor_user_id=None,
                        title="Multiple failed sign-in attempts detected",
                        body="We detected repeated unsuccessful login attempts on your desifaces.ai account.",
                        action_route="/notifications",
                        action_label="Review activity",
                        image_url=None,
                        payload_json={"user_id": str(user["id"]), "failed_attempts": int(failed_count)},
                        metadata_json={"user_id": str(user["id"]), "failed_attempts": int(failed_count)},
                        dedupe_key=f"invalid-login-threshold:{user['id']}:{bucket}",
                        recipients=[InternalRecipient(user_id=str(user["id"]))],
                    ),
                    log_context={"user_id": str(user["id"]), "event_type": "INVALID_LOGIN_THRESHOLD_REACHED", "failed_attempts": int(failed_count)},
                )
            raise HTTPException(status_code=401, detail="invalid_credentials")

        if not user["is_active"]:
            await audit_log(
                conn,
                action="auth.login.failed",
                entity_type="user",
                entity_id=user["id"],
                actor_user_id=user["id"],
                request_id=request_id,
                ip=ip,
                user_agent=ua,
                after={"reason": "email_verification_required"},
            )
            raise HTTPException(status_code=403, detail="email_verification_required")

        response = await _issue_login_tokens(
            conn,
            user_id=user["id"],
            email=user["email"],
            full_name=user["full_name"] or "",
            tier=user["tier"],
            is_active=bool(user["is_active"]),
            request_id=request_id,
            ip=ip,
            ua=ua,
            device_id=req.device_id,
            client_type=req.client_type,
        )

        bootstrap_user_id = user["id"]
        bootstrap_email = user["email"]
        bootstrap_tier = user["tier"]

    await _best_effort_bootstrap_pricing_for_user(
        user_id=bootstrap_user_id,
        email=bootstrap_email,
        tier=bootstrap_tier,
        source="svc_core_login",
    )

    return response


@router.get("/me", response_model=MeResponse)
async def me(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
):
    pool = await get_pool()
    request_id, ip, ua = _req_meta(request)

    payload = _decode_access_claims(creds.credentials)
    user_id = str(payload.get("sub") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid_token")

    async with pool.acquire() as conn:
        auth_user = await _build_auth_user(conn, user_id)

        await audit_log(
            conn,
            action="auth.me",
            entity_type="user",
            entity_id=auth_user.id,
            actor_user_id=auth_user.id,
            request_id=request_id,
            ip=ip,
            user_agent=ua,
            after={"email": auth_user.email},
        )

        return MeResponse(**auth_user.model_dump())


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
                   u.full_name,
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
            user=AuthUser(
                id=sess["user_id"],
                email=sess["email"],
                full_name=(sess["full_name"] or "").strip(),
                tier=sess["tier"],
                is_active=bool(sess["is_active"]),
                roles=roles,
            ),
        )


@router.post("/logout", response_model=dict)
async def logout(req: LogoutRequest, request: Request):
    pool = await get_pool()
    request_id, ip, ua = _req_meta(request)

    async with pool.acquire() as conn:
        r_hash = hash_refresh_token(req.refresh_token)

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


@router.post("/password/change/start", response_model=ChangePasswordStartResponse)
async def change_password_start(
    req: ChangePasswordStartRequest,
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
):
    pool = await get_pool()
    _, user_id, request_id, ip, ua = await _authenticate_bearer_user(request, creds)

    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT id::text AS id, email, password_hash, is_active
            FROM core.users
            WHERE id = $1::uuid
            """,
            user_id,
        )
        if not user or not user["is_active"]:
            raise HTTPException(status_code=401, detail="invalid_token")

        if not verify_password(req.current_password, user["password_hash"]):
            await audit_log(
                conn,
                action="auth.password_change_start.failed",
                entity_type="user",
                entity_id=user_id,
                actor_user_id=user_id,
                request_id=request_id,
                ip=ip,
                user_agent=ua,
                after={"reason": "invalid_current_password"},
            )
            raise HTTPException(status_code=401, detail="invalid_current_password")

        challenge, raw_code = await _issue_email_challenge(
            conn,
            user_id=user_id,
            email=user["email"],
            purpose="password_change",
            ip=ip,
            ua=ua,
        )

        await audit_log(
            conn,
            action="auth.password_change_start",
            entity_type="user",
            entity_id=user_id,
            actor_user_id=user_id,
            request_id=request_id,
            ip=ip,
            user_agent=ua,
            after={"challenge_id": challenge["id"]},
        )

        send_ok = await _send_email_code_best_effort(
            email=user["email"],
            purpose="password_change",
            code=raw_code,
            log_context={"user_id": user_id, "email": user["email"], "purpose": "password_change"},
        )
        if not send_ok and not _dev_return_email_otp_code():
            raise HTTPException(status_code=500, detail="password_change_email_otp_send_failed")

        return ChangePasswordStartResponse(
            challenge_id=challenge["id"],
            expires_in=_email_challenge_ttl_seconds("password_change"),
            resend_after_seconds=_email_otp_resend_cooldown_seconds(),
            dev_email_otp_code=raw_code if _dev_return_email_otp_code() else None,
        )


@router.post("/password/change/confirm", response_model=dict)
async def change_password_confirm(
    req: ChangePasswordConfirmRequest,
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
):
    pool = await get_pool()
    _, user_id, request_id, ip, ua = await _authenticate_bearer_user(request, creds)

    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT id::text AS id, email, password_hash, is_active
            FROM core.users
            WHERE id = $1::uuid
            """,
            user_id,
        )
        if not user or not user["is_active"]:
            raise HTTPException(status_code=401, detail="invalid_token")

        await _verify_email_challenge(
            conn,
            challenge_id=req.challenge_id,
            purpose="password_change",
            code=req.code,
            user_id=user_id,
            email=user["email"],
        )

        if verify_password(req.new_password, user["password_hash"]):
            raise HTTPException(status_code=400, detail="new_password_must_differ")

        new_hash = hash_password(req.new_password)
        await conn.execute(
            "UPDATE core.users SET password_hash=$1, updated_at=now() WHERE id=$2::uuid",
            new_hash,
            user_id,
        )
        await conn.execute(
            "UPDATE core.sessions SET revoked_at=now() WHERE user_id=$1::uuid AND revoked_at IS NULL",
            user_id,
        )

        await audit_log(
            conn,
            action="auth.password_change.completed",
            entity_type="user",
            entity_id=user_id,
            actor_user_id=user_id,
            request_id=request_id,
            ip=ip,
            user_agent=ua,
            after={"sessions_revoked": True},
        )

        await _emit_account_notification_best_effort(
            None,
            req=InternalNotificationEventCreate(
                event_type="PASSWORD_CHANGED",
                category="account",
                priority="important",
                source_service="svc-core",
                source_ref_type="user",
                source_ref_id=str(user_id),
                actor_user_id=str(user_id),
                title="Password changed successfully",
                body="Your desifaces.ai password was changed and active sessions were revoked.",
                action_route="/notifications",
                action_label="Review security",
                image_url=None,
                payload_json={"user_id": str(user_id)},
                metadata_json={"user_id": str(user_id)},
                dedupe_key=f"password-changed:{user_id}:{int(time.time() // 60)}",
                recipients=[InternalRecipient(user_id=str(user_id))],
            ),
            log_context={"user_id": str(user_id), "event_type": "PASSWORD_CHANGED"},
        )

        return {"ok": True, "status": "password_changed", "sessions_revoked": True, "reauth_required": True}


async def _start_password_reset_otp(
    *,
    email: str,
    request: Request,
) -> PasswordResetStartResponse:
    """Start OTP-first password recovery for mobile and web.

    The response shape is intentionally stable for existing and non-existing
    emails to reduce account enumeration. Existing active users receive a
    one-time code via email; non-existing/inactive users receive a fake
    challenge id and no email.
    """
    pool = await get_pool()
    request_id, ip, ua = _req_meta(request)
    normalized_email = str(email or "").strip()

    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT id::text AS id, email, is_active
            FROM core.users
            WHERE lower(email)=lower($1)
            """,
            normalized_email,
        )

        # Anti-enumeration: same public response shape, no user existence leak.
        if not user or not bool(user["is_active"]):
            await audit_log(
                conn,
                action="auth.password_reset_start",
                entity_type="auth",
                entity_id=normalized_email.lower(),
                actor_user_id=None,
                request_id=request_id,
                ip=ip,
                user_agent=ua,
                after={"result": "ok_no_active_user"},
            )
            return PasswordResetStartResponse(
                challenge_id=f"noop_{secrets.token_urlsafe(24)}",
                expires_in=_email_challenge_ttl_seconds("password_reset"),
                resend_after_seconds=_email_otp_resend_cooldown_seconds(),
                dev_email_otp_code=None,
            )

        challenge, raw_code = await _issue_email_challenge(
            conn,
            user_id=user["id"],
            email=user["email"],
            purpose="password_reset",
            ip=ip,
            ua=ua,
        )

        await audit_log(
            conn,
            action="auth.password_reset_start",
            entity_type="user",
            entity_id=user["id"],
            actor_user_id=user["id"],
            request_id=request_id,
            ip=ip,
            user_agent=ua,
            after={"challenge_id": challenge["id"], "delivery": "attempted"},
        )

        logger.info(
            "auth_password_reset_otp_created",
            extra={"user_id": user["id"], "email": user["email"], "challenge_id": challenge["id"]},
        )

        send_ok = await _send_email_code_best_effort(
            email=user["email"],
            purpose="password_reset",
            code=raw_code,
            log_context={"user_id": user["id"], "email": user["email"], "purpose": "password_reset", "challenge_id": challenge["id"]},
        )

        if send_ok:
            logger.info(
                "auth_password_reset_otp_email_sent",
                extra={"user_id": user["id"], "email": user["email"], "challenge_id": challenge["id"]},
            )
        else:
            # Keep anti-enumeration response stable, but log loudly.
            logger.error(
                "auth_password_reset_otp_email_failed",
                extra={"user_id": user["id"], "email": user["email"], "challenge_id": challenge["id"]},
            )

        await _emit_account_notification_best_effort(
            None,
            req=InternalNotificationEventCreate(
                event_type="PASSWORD_RESET_REQUESTED",
                category="account",
                priority="important",
                source_service="svc-core",
                source_ref_type="user",
                source_ref_id=str(user["id"]),
                actor_user_id=str(user["id"]),
                title="Password reset requested",
                body="A password reset verification code was requested for your desifaces.ai account.",
                action_route="/notifications",
                action_label="Review request",
                image_url=None,
                payload_json={"user_id": str(user["id"]), "challenge_id": challenge["id"]},
                metadata_json={"user_id": str(user["id"]), "challenge_id": challenge["id"]},
                dedupe_key=f"password-reset-requested:{user['id']}:{int(time.time() // 900)}",
                recipients=[InternalRecipient(user_id=str(user["id"]))],
            ),
            log_context={"user_id": str(user["id"]), "event_type": "PASSWORD_RESET_REQUESTED"},
        )

        return PasswordResetStartResponse(
            challenge_id=challenge["id"],
            expires_in=_email_challenge_ttl_seconds("password_reset"),
            resend_after_seconds=_email_otp_resend_cooldown_seconds(),
            dev_email_otp_code=raw_code if _dev_return_email_otp_code() else None,
        )


@router.post("/password/reset/start", response_model=PasswordResetStartResponse)
async def password_reset_start(req: PasswordResetStartRequest, request: Request):
    return await _start_password_reset_otp(email=str(req.email).strip(), request=request)


@router.post("/password/reset/confirm", response_model=dict)
async def password_reset_confirm(req: PasswordResetConfirmRequest, request: Request):
    pool = await get_pool()
    request_id, ip, ua = _req_meta(request)

    async with pool.acquire() as conn:
        challenge = await _verify_email_challenge(
            conn,
            challenge_id=req.challenge_id,
            purpose="password_reset",
            code=req.code,
        )
        user_id = str(challenge["user_id"] or "").strip()
        if not user_id:
            raise HTTPException(status_code=400, detail="password_reset_challenge_missing_user")

        user = await conn.fetchrow(
            """
            SELECT id::text AS id, email, password_hash, is_active
            FROM core.users
            WHERE id = $1::uuid
            """,
            user_id,
        )
        if not user or not bool(user["is_active"]):
            raise HTTPException(status_code=400, detail="invalid_password_reset_challenge")

        if verify_password(req.new_password, user["password_hash"]):
            raise HTTPException(status_code=400, detail="new_password_must_differ")

        new_hash = hash_password(req.new_password)
        await conn.execute(
            "UPDATE core.users SET password_hash=$1, updated_at=now() WHERE id=$2::uuid",
            new_hash,
            user_id,
        )
        await conn.execute(
            "UPDATE core.sessions SET revoked_at=now() WHERE user_id=$1::uuid AND revoked_at IS NULL",
            user_id,
        )

        await audit_log(
            conn,
            action="auth.password_reset.completed",
            entity_type="user",
            entity_id=user_id,
            actor_user_id=user_id,
            request_id=request_id,
            ip=ip,
            user_agent=ua,
            after={"challenge_id": req.challenge_id, "sessions_revoked": True},
        )

        await _emit_account_notification_best_effort(
            None,
            req=InternalNotificationEventCreate(
                event_type="PASSWORD_RESET_COMPLETED",
                category="account",
                priority="important",
                source_service="svc-core",
                source_ref_type="user",
                source_ref_id=str(user_id),
                actor_user_id=str(user_id),
                title="Password changed successfully",
                body="Your desifaces.ai password was updated and existing sessions were revoked.",
                action_route="/notifications",
                action_label="Review security",
                image_url=None,
                payload_json={"user_id": str(user_id), "challenge_id": req.challenge_id},
                metadata_json={"user_id": str(user_id), "challenge_id": req.challenge_id},
                dedupe_key=f"password-reset-completed:{user_id}:{req.challenge_id}",
                recipients=[InternalRecipient(user_id=str(user_id))],
            ),
            log_context={"user_id": str(user_id), "event_type": "PASSWORD_RESET_COMPLETED"},
        )

        logger.info(
            "auth_password_reset_completed",
            extra={"user_id": user_id, "email": user["email"], "challenge_id": req.challenge_id},
        )

        return {"ok": True, "status": "password_reset", "sessions_revoked": True, "reauth_required": True}


@router.post("/forgot-password", response_model=PasswordResetStartResponse)
async def forgot_password(req: ForgotPasswordRequest, request: Request):
    # Backward-compatible route name for existing mobile/web UI.
    # Primary implementation is OTP-first and shared with /password/reset/start.
    return await _start_password_reset_otp(email=str(req.email).strip(), request=request)


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

        await _emit_account_notification_best_effort(
            None,
            req=InternalNotificationEventCreate(
                event_type="PASSWORD_RESET_COMPLETED",
                category="account",
                priority="important",
                source_service="svc-core",
                source_ref_type="user",
                source_ref_id=str(row["user_id"]),
                actor_user_id=str(row["user_id"]),
                title="Password changed successfully",
                body="Your desifaces.ai password was updated and existing sessions were revoked.",
                action_route="/notifications",
                action_label="Review security",
                image_url=None,
                payload_json={"user_id": str(row["user_id"])},
                metadata_json={"user_id": str(row["user_id"])},
                dedupe_key=f"password-reset-completed:{row['user_id']}:{row['id']}",
                recipients=[InternalRecipient(user_id=str(row["user_id"]))],
            ),
            log_context={"user_id": str(row["user_id"]), "event_type": "PASSWORD_RESET_COMPLETED"},
        )

        return {"ok": True}
