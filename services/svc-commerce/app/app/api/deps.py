from __future__ import annotations

import os
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import Header, HTTPException

try:
    import jwt  # PyJWT
except Exception:  # pragma: no cover
    jwt = None  # type: ignore


def _jwt_secrets() -> list[str]:
    secrets: list[str] = []
    for k in ("JWT_HMAC_SECRET", "JWT_SECRET"):
        v = (os.getenv(k) or "").strip()
        if v:
            secrets.append(v)
    return secrets


def _jwt_meta() -> tuple[Optional[str], Optional[str], str]:
    issuer = (os.getenv("JWT_ISSUER") or "").strip() or None
    audience = (os.getenv("JWT_AUDIENCE") or "").strip() or None
    alg = (os.getenv("JWT_ALG") or "HS256").strip()
    return issuer, audience, alg


def _allow_unverified_jwt() -> bool:
    # Explicit opt-in for local/dev only
    v = (os.getenv("JWT_ALLOW_UNVERIFIED") or os.getenv("DF_JWT_ALLOW_UNVERIFIED") or "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def _allow_expired_jwt() -> bool:
    # Explicit opt-in for local/dev only
    v = (os.getenv("JWT_ALLOW_EXPIRED") or os.getenv("DF_JWT_ALLOW_EXPIRED") or "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def _decode_user_id_from_bearer(authorization: str | None) -> Optional[UUID]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None

    if jwt is None:
        # Make this explicit (otherwise you’ll chase “unauthorized” forever)
        raise HTTPException(status_code=500, detail="PyJWT not installed in svc-commerce image")

    issuer, audience, alg = _jwt_meta()
    secrets = _jwt_secrets()

    last_err: Exception | None = None

    # 1) Verified decode (preferred)
    if secrets:
        for secret in secrets:
            try:
                options: Dict[str, Any] = {
                    "require": ["sub"],
                    "verify_signature": True,
                    "verify_aud": bool(audience),
                    "verify_iss": bool(issuer),
                    "verify_exp": not _allow_expired_jwt(),
                }
                kwargs: Dict[str, Any] = {"algorithms": [alg], "options": options}
                if issuer:
                    kwargs["issuer"] = issuer
                if audience:
                    kwargs["audience"] = audience

                payload = jwt.decode(token, secret, **kwargs)  # type: ignore[misc]
                sub = payload.get("sub")
                return UUID(str(sub))
            except Exception as e:  # noqa: BLE001
                last_err = e

    # 2) Unverified decode (dev only, explicit opt-in)
    if _allow_unverified_jwt():
        try:
            payload = jwt.decode(  # type: ignore[misc]
                token,
                options={
                    "verify_signature": False,
                    "verify_aud": False,
                    "verify_iss": False,
                    "verify_exp": not _allow_expired_jwt(),
                },
            )
            sub = payload.get("sub")
            return UUID(str(sub))
        except Exception as e:  # noqa: BLE001
            last_err = e

    # No valid decode
    if last_err:
        # keep response small (don’t print token / big traces)
        raise HTTPException(status_code=401, detail="unauthorized") from last_err
    return None


def get_current_user_id(
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> UUID:
    """
    Auth order:
    1) Authorization: Bearer <jwt>  (preferred)
    2) X-User-Id: <uuid>           (local/dev convenience)
    """
    uid = _decode_user_id_from_bearer(authorization)
    if uid:
        return uid

    if x_user_id:
        try:
            return UUID(x_user_id)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=401, detail="invalid_x_user_id") from e

    raise HTTPException(status_code=401, detail="unauthorized")


# Backward-compatible alias used across route modules
require_user = get_current_user_id