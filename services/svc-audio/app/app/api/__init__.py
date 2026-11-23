from __future__ import annotations

from fastapi import APIRouter


def build_router() -> APIRouter:
    router = APIRouter()

    from app.api.health import router as health_router
    from app.api.routes.tts_jobs import router as tts_jobs_router
    from app.api.routes.catalog import router as catalog_router
    from app.api.routes.catalog_admin import router as catalog_admin_router
    from app.api.routes.v3_audio_output import router as v3_audio_output_router

    router.include_router(health_router)
    router.include_router(tts_jobs_router)
    router.include_router(catalog_router)
    router.include_router(catalog_admin_router)
    router.include_router(v3_audio_output_router)

    # DEV-only: provider-authenticated localization draft preview.
    # The endpoint has no canonical approval authority.
    import os
    if (
        os.getenv("DF_RUNTIME_ENV", "").strip().lower()
        in {"dev", "development"}
    ):
        from app.api.routes.dev101_translation_preview import (
            router as dev101_preview_router,
        )
        router.include_router(dev101_preview_router)

    return router
