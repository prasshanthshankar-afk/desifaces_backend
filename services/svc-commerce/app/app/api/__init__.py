from __future__ import annotations

import importlib
import logging
from fastapi import APIRouter

from app.api.health import router as health_router

log = logging.getLogger(__name__)


def build_router() -> APIRouter:
    router = APIRouter()
    router.include_router(health_router)

    for mod_path in [
        "app.api.routes.commerce_products",
        "app.api.routes.commerce_looksets",
        "app.api.routes.commerce_campaigns",
        "app.api.routes.commerce_tryon",
        "app.api.routes.commerce_templates",
        "app.api.routes.commerce_exports",
        "app.api.routes.commerce_quotes",
    ]:
        try:
            m = importlib.import_module(mod_path)
            r = getattr(m, "router", None)
            if r is not None:
                router.include_router(r)
                log.info("Included router: %s", mod_path)
        except Exception as e:
            log.warning("Skipping router %s due to import error: %s", mod_path, e, exc_info=True)

    return router