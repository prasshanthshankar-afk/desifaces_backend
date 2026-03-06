from __future__ import annotations

from fastapi import FastAPI

from app.api.routes.marketing_runs import router as marketing_router
from app.api.routes.schedules import router as schedules_router
from app.db import get_pool
from app.services.admin.admin_resolver import maybe_set_admin_marketing_user_id


def create_app() -> FastAPI:
    app = FastAPI(title="svc-marketing", version="1.0.0")
    app.include_router(marketing_router)
    app.include_router(schedules_router)

    @app.on_event("startup")
    async def _startup() -> None:
        pool = await get_pool()
        # Resolve ADMIN_MARKETING_USER_ID from ADMIN_MARKETING_EMAIL (core.users) if needed.
        await maybe_set_admin_marketing_user_id(pool)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "service": "svc-marketing"}

    return app


app = create_app()