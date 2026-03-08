# services/svc-pricing/app/app/main.py
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import close_db_pool, ensure_db_pool

from app.api.routes.health import router as health_router
from app.api.routes.pricing import router as pricing_router
from app.api.routes.credits import router as credits_router
from app.api.routes.reservations import router as reservations_router

def _configure_logging() -> None:
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app() -> FastAPI:
    _configure_logging()

    app = FastAPI(title=settings.SERVICE_NAME)

    # CORS: keep permissive for now; tighten later if you have explicit origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def _startup() -> None:
        await ensure_db_pool()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await close_db_pool()

    app.include_router(health_router)
    app.include_router(pricing_router)
    app.include_router(credits_router)
    app.include_router(reservations_router)

    return app


app = create_app()
