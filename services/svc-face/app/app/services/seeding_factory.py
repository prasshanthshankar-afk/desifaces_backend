from __future__ import annotations

import os
from app.services.seeding_service import SeedService

def get_seed_service() -> SeedService:
    hx = os.getenv("DF_SEED_SECRET_HEX", "").strip()
    if not hx:
        raise RuntimeError("DF_SEED_SECRET_HEX is required")
    secret = bytes.fromhex(hx)
    return SeedService(secret=secret)