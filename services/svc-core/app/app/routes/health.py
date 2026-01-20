from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter

router = APIRouter(prefix="/api/health", tags=["health"])

_STARTED_AT = time.time()


@router.get("")
@router.get("/")
async def health() -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "status": "ok",
        "service": os.getenv("SERVICE_NAME", "svc-core"),
        "version": os.getenv("SERVICE_VERSION", os.getenv("GIT_SHA", "dev")),
        "time_utc": now.isoformat(),
        "uptime_s": round(time.time() - _STARTED_AT, 3),
    }


@router.get("/ready")
async def ready() -> Dict[str, Any]:
    # Keep dependency-free for now. Add DB/Redis checks later if desired.
    return {"status": "ready"}