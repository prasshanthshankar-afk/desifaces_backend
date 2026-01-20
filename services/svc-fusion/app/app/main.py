from __future__ import annotations

import os
from fastapi import FastAPI
from app.api import build_router


def create_app() -> FastAPI:
    app = FastAPI(
        title=os.getenv("SERVICE_NAME", "desifaces-service"),
        version=os.getenv("SERVICE_VERSION", os.getenv("GIT_SHA", "dev")),
        docs_url=os.getenv("DOCS_URL", "/docs"),
        redoc_url=os.getenv("REDOC_URL", "/redoc"),
        openapi_url=os.getenv("OPENAPI_URL", "/openapi.json"),
    )

    app.include_router(build_router())

    @app.get("/")
    async def root():
        return {"service": os.getenv("SERVICE_NAME", "desifaces-service"), "status": "ok"}

    return app


app = create_app()