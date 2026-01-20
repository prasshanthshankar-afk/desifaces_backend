# services/svc-face/app/app/main.py
from __future__ import annotations
import os
from fastapi import FastAPI
from app.api import build_router
from app.db import get_pool, close_pool

def create_app() -> FastAPI:
    app = FastAPI(
        title="DesiFaces Face Studio",
        version=os.getenv("SERVICE_VERSION", "1.0.0"),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    
    # Include API routes
    app.include_router(build_router())
    
    @app.on_event("startup")
    async def startup():
        """Initialize database pool on startup"""
        await get_pool()
    
    @app.on_event("shutdown")
    async def shutdown():
        """Close database pool on shutdown"""
        await close_pool()
    
    @app.get("/")
    async def root():
        return {
            "service": "svc-face",
            "status": "ok",
            "version": os.getenv("SERVICE_VERSION", "1.0.0")
        }
    
    return app

app = create_app()