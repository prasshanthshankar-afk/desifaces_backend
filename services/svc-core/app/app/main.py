from __future__ import annotations

import os
from fastapi import FastAPI

from app.routes.health import router as health_router
from app.routes.auth import router as auth_router
from fastapi.responses import RedirectResponse


def create_app() -> FastAPI:
    app = FastAPI(
        title=os.getenv("SERVICE_NAME", "svc-core"),
        version=os.getenv("SERVICE_VERSION", os.getenv("GIT_SHA", "dev")),
        docs_url=os.getenv("DOCS_URL", "/docs"),
        redoc_url=os.getenv("REDOC_URL", "/redoc"),
        openapi_url=os.getenv("OPENAPI_URL", "/openapi.json"),
    )

    app.include_router(health_router)
    app.include_router(auth_router)

    @app.get("/swagger", include_in_schema=False)
    def swagger_redirect():
        return RedirectResponse(url="/docs")

    @app.get("/api/swagger", include_in_schema=False)
    def api_swagger_redirect():
        return RedirectResponse(url="/docs")
        
    @app.get("/")
    async def root():
        return {"service": os.getenv("SERVICE_NAME", "svc-core"), "status": "ok"}

    return app


app = create_app()