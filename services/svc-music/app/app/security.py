from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from jose import jwt
from jose.exceptions import JWTError, ExpiredSignatureError, JWTClaimsError

from app.config import settings


class AuthError(Exception):
    pass


def _candidate_secrets() -> list[str]:
    cands: list[str] = []
    for v in [getattr(settings, "JWT_HMAC_SECRET", None), getattr(settings, "JWT_SECRET", None)]:
        if v and str(v).strip():
            cands.append(str(v).strip())
    out: list[str] = []
    for s in cands:
        if s not in out:
            out.append(s)
    return out


def decode_access_token(token: str) -> dict[str, Any]:
    tok = (token or "").strip()
    if not tok:
        raise AuthError("missing_token")

    alg = (getattr(settings, "JWT_ALG", None) or "HS256").strip()
    issuer = (getattr(settings, "JWT_ISSUER", None) or "").strip() or None
    audience = (getattr(settings, "JWT_AUDIENCE", None) or "").strip() or None

    last_err: Optional[Exception] = None

    for secret in _candidate_secrets():
        try:
            opts = {
                "verify_aud": bool(audience),
                "verify_iss": bool(issuer),
            }
            payload = jwt.decode(
                tok,
                secret,
                algorithms=[alg],
                issuer=issuer if issuer else None,
                audience=audience if audience else None,
                options=opts,
            )
            return payload

        except ExpiredSignatureError as e:
            last_err = e
            # if token expired, no need to try other secrets
            raise AuthError("token_expired") from e

        except JWTClaimsError as e:
            last_err = e
            # claims mismatch (aud/iss/nbf/etc)
            raise AuthError(f"token_claims_invalid:{e}") from e

        except JWTError as e:
            # could be signature, alg mismatch, malformed token, etc.
            last_err = e
            continue

        except Exception as e:
            last_err = e
            continue

    # if we got here, signature or token format is wrong for all secrets
    raise AuthError("invalid_token") from last_err


def user_id_from_payload(payload: dict[str, Any]) -> UUID:
    raw = payload.get("sub") or payload.get("user_id")
    if not raw:
        raise AuthError("token_missing_sub")
    try:
        return UUID(str(raw))
    except Exception as e:
        raise AuthError("token_sub_not_uuid") from e