from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Optional, Tuple

import httpx

from app.services.orchestration.utils.config import cfg_int, cfg_str


_lock = asyncio.Lock()
_cached_bearer: str = ""
_cached_user_id: str = ""
_cached_exp: int = 0


def _decode_jwt_sub_exp(token: str) -> Tuple[str, int]:
    """
    Returns (sub, exp). If decode fails, returns ("", 0).
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return "", 0
        payload = parts[1]
        pad = "=" * ((4 - len(payload) % 4) % 4)
        payload = payload.replace("-", "+").replace("_", "/") + pad
        data = json.loads(base64.b64decode(payload).decode("utf-8"))
        sub = str(data.get("sub") or "")
        exp = int(data.get("exp") or 0)
        return sub, exp
    except Exception:
        return "", 0


async def _login_service_account() -> Tuple[str, str, int]:
    core_url = cfg_str("CORE_URL", "http://svc-core:8000").rstrip("/")
    email = cfg_str("DF_SERVICE_EMAIL", "").strip()
    password = cfg_str("DF_SERVICE_PASSWORD", "").strip()

    if not email or not password:
        raise RuntimeError("DF_SERVICE_EMAIL/DF_SERVICE_PASSWORD not set")

    url = f"{core_url}/api/auth/login"
    payload = {"email": email, "password": password}

    async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()

    tok = str(data.get("access_token") or data.get("token") or "").strip()
    tok = tok.replace("Bearer ", "").strip()
    if not tok:
        raise RuntimeError("svc-core login returned no access_token")

    sub, exp = _decode_jwt_sub_exp(tok)
    bearer = f"Bearer {tok}"

    # exp might be missing; assume 30 minutes if so
    if exp <= 0:
        exp = int(time.time()) + cfg_int("DF_SERVICE_TOKEN_FALLBACK_TTL_S", 1800)

    if not sub:
        # if sub missing, caller can still use bearer, but X-User-Id may be required by downstream
        sub = cfg_str("DF_SERVICE_USER_ID", "").strip()

    return bearer, sub, exp


async def get_service_bearer_and_user_id() -> Tuple[str, str]:
    """
    In-memory only cache; never persisted.
    Refresh when expiring within 5 minutes.
    """
    global _cached_bearer, _cached_user_id, _cached_exp

    async with _lock:
        now = int(time.time())
        if _cached_bearer and _cached_user_id and _cached_exp > (now + 300):
            return _cached_bearer, _cached_user_id

        bearer, user_id, exp = await _login_service_account()
        _cached_bearer = bearer
        _cached_user_id = user_id
        _cached_exp = exp
        return bearer, user_id