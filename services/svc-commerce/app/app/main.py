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

    app.include_router(build_router())

    @app.on_event("startup")
    async def startup():
        await get_pool()

    @app.on_event("shutdown")
    async def shutdown():
        await close_pool()

    @app.get("/")
    async def root():
        return {"service": "svc-commerce", "status": "ok", "version": os.getenv("SERVICE_VERSION", "1.0.0")}

    return app


# IMPORTANT: uvicorn expects "app" here (app.main:app)
app = create_app()