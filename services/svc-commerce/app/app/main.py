from __future__ import annotations

import os
from fastapi import FastAPI

from app.api import build_router
from app.db import get_pool, close_pool


def create_app() -> FastAPI:
    app = FastAPI(
        title="DesiFaces Commerce Studio",
        version=os.getenv("SERVICE_VERSION", "1.0.0"),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # Existing router builder (keeps your current behavior)
    app.include_router(build_router())

    # ✅ Hard-include commerce_assets router (prevents silent 404s)
    try:
        from app.api.routes.commerce_assets import router as commerce_assets_router

        app.include_router(commerce_assets_router)
        print("[svc-commerce] included commerce_assets_router")
    except Exception as e:
        # Keep service up, but print loud error (autoloader might also print)
        print(f"[svc-commerce] FAILED to include commerce_assets_router: {e!r}")

    @app.on_event("startup")
    async def startup():
        await get_pool()

        # ✅ Proof in logs that the route exists (or not)
        has_assets = any(
            getattr(r, "path", None) == "/api/commerce/assets/upload" for r in app.routes
        )
        print(f"[svc-commerce] assets_upload_route_registered={has_assets}")

    @app.on_event("shutdown")
    async def shutdown():
        await close_pool()

    @app.get("/")
    async def root():
        return {"service": "svc-commerce", "status": "ok", "version": os.getenv("SERVICE_VERSION", "1.0.0")}

    return app


# IMPORTANT: uvicorn expects "app" here (app.main:app)
app = create_app()