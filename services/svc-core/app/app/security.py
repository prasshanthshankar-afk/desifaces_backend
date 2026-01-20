from __future__ import annotations

import os
import time
import hmac
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any, Dict, Optional, List

from jose import jwt, JWTError
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

# -------------------------
# Password hashing (Argon2id)
# -------------------------
_pwd_hasher = PasswordHasher(
    time_cost=int(os.getenv("PWD_TIME_COST", "2")),
    memory_cost=int(os.getenv("PWD_MEMORY_COST", "102400")),  # ~100MB
    parallelism=int(os.getenv("PWD_PARALLELISM", "8")),
)

def hash_password(plain: str) -> str:
    return _pwd_hasher.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd_hasher.verify(hashed, plain)
    except VerifyMismatchError:
        return False

# -------------------------
# Refresh token (opaque) + HMAC hashing for storage
# -------------------------
# Never store refresh token raw. Store HMAC-SHA256(token) with server secret.
_REFRESH_HMAC_SECRET = os.getenv("REFRESH_TOKEN_HMAC_SECRET", "").strip()
if not _REFRESH_HMAC_SECRET:
    # Fail fast in prod; for dev you can set it in .env
    raise RuntimeError("REFRESH_TOKEN_HMAC_SECRET is required")

def mint_refresh_token() -> str:
    # Opaque token (high entropy)
    return secrets.token_urlsafe(48)

def hash_refresh_token(token: str) -> str:
    mac = hmac.new(_REFRESH_HMAC_SECRET.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
    return mac

# -------------------------
# Access JWT
# -------------------------
_JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
_JWT_ISSUER = os.getenv("JWT_ISSUER", "desifaces").strip()
_JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "desifaces_clients").strip()
_JWT_ALG = os.getenv("JWT_ALG", "HS256").strip()

if not _JWT_SECRET:
    raise RuntimeError("JWT_SECRET is required")

ACCESS_TTL_SECONDS = int(os.getenv("ACCESS_TTL_SECONDS", "900"))   # 15 min
REFRESH_TTL_SECONDS = int(os.getenv("REFRESH_TTL_SECONDS", "2592000"))  # 30 days

#-------------------------
# JWT minting and decoding
#-------------------------
def mint_access_jwt(*, user_id: str, email: str, tier: str, roles: List[str]) -> str:
    now = int(time.time())
    payload = {
        "iss": _JWT_ISSUER,
        "aud": _JWT_AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + ACCESS_TTL_SECONDS,
        "sub": user_id,
        "email": email,
        "tier": tier,
        "roles": roles,
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALG)

#-------------------------
# JWT decoding
#-------------------------
def decode_access_jwt(token: str) -> Dict[str, Any]:
    try:
        return jwt.decode(
            token,
            _JWT_SECRET,
            algorithms=[_JWT_ALG],
            audience=_JWT_AUDIENCE,
            issuer=_JWT_ISSUER,
        )
    except JWTError as e:
        raise ValueError(f"invalid_token: {e}") from e