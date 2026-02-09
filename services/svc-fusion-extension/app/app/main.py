import asyncio
from fastapi import FastAPI

from app.logging import setup_logging
from app.db import init_db
from app.api.health import router as health_router
from app.api.routes.longform import router as longform_router
from app.config import settings

from app.workers.longform_worker import worker_loop


def create_app() -> FastAPI:
    app = FastAPI(title="desifaces-service", version="dev")
    app.include_router(health_router)
    app.include_router(longform_router)
    return app


app = create_app()


@app.on_event("startup")
async def on_startup():
    setup_logging()
    pool = await init_db()
    if settings.WORKER_ENABLED:
        asyncio.create_task(worker_loop())