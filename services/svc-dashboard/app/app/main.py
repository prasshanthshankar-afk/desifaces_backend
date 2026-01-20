from __future__ import annotations

import os
from fastapi import FastAPI
from app.api import build_router
from app.settings import settings
from app.db import init_db_pool, close_db_pool
from app.api.health import router as health_router
from app.api.routes.dashboard import router as dashboard_router

def create_app() -> FastAPI:
    app = FastAPI(
        title=os.getenv("SERVICE_NAME", "desifaces-service"),
        version=os.getenv("SERVICE_VERSION", os.getenv("GIT_SHA", "dev")),
        docs_url=os.getenv("DOCS_URL", "/docs"),
        redoc_url=os.getenv("REDOC_URL", "/redoc"),
        openapi_url=os.getenv("OPENAPI_URL", "/openapi.json"),
    )

    app.include_router(build_router())
    app.include_router(health_router)
    app.include_router(dashboard_router)
    
    @app.on_event("startup")
    async def on_startup():
        await init_db_pool(settings.DATABASE_URL)


    @app.on_event("shutdown")
    async def on_shutdown():
        await close_db_pool()

    @app.get("/")
    async def root():
        return {"service": os.getenv("SERVICE_NAME", "desifaces-service"), "status": "ok"}

    return app


app = create_app()