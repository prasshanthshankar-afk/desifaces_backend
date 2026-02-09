from __future__ import annotations

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.routes.music_assets import router as music_assets_router
from app.api.routes.music_catalog import router as music_catalog_router
from app.api.routes.music_jobs import router as music_jobs_router
from app.api.routes.music_projects import router as music_projects_router
from app.api.routes.support_router import router as support_router

app = FastAPI(title="svc-music", version="dev")

API_PREFIX = "/api"
app.include_router(health_router, prefix=API_PREFIX)
app.include_router(music_catalog_router, prefix=API_PREFIX)
app.include_router(music_projects_router, prefix=API_PREFIX)
app.include_router(music_jobs_router, prefix=API_PREFIX)
app.include_router(music_assets_router, prefix=API_PREFIX)
app.include_router(support_router, prefix=API_PREFIX)