from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

# Keep imports defensive so missing modules never crash the app
for mod_path in [
    "app.api.routes.commerce_quotes",
    "app.api.routes.commerce_products",
    "app.api.routes.commerce_looksets",
    "app.api.routes.commerce_campaigns",
    "app.api.routes.commerce_tryon",
    "app.api.routes.commerce_templates",
    "app.api.routes.commerce_exports",
]:
    try:
        m = __import__(mod_path, fromlist=["router"])
        r = getattr(m, "router", None)
        if r is not None:
            router.include_router(r)
    except Exception:
        pass