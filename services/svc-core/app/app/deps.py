from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.security import decode_access_jwt

bearer = HTTPBearer(auto_error=False)

# -------------------------
# Dependency functions
# -------------------------
def get_current_claims(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    if not creds or not creds.credentials:
        raise HTTPException(status_code=401, detail="missing_token")
    try:
        return decode_access_jwt(creds.credentials)
    except Exception:
        raise HTTPException(status_code=401, detail="invalid_token")

# -------------------------
# Admin role required
# -------------------------
def require_admin(claims: dict = Depends(get_current_claims)) -> dict:
    roles = claims.get("roles") or []
    if "admin" not in roles:
        raise HTTPException(status_code=403, detail="admin_required")
    return claims