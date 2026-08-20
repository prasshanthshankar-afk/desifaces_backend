from __future__ import annotations

from fastapi import APIRouter

from app.api.health import router as health_router
from app.api.routes import fusion_jobs as fusion_jobs_routes
from app.services.v3_pricing_bridge import (
    ConfirmedFusionJobCreate,
    ConfirmedPricingFusionOrchestrator,
)

# Additive V3 pricing bridge: the existing Fusion routes, pricing calculation,
# provider orchestration and recovery stay authoritative. Only the request model
# and orchestrator implementation are upgraded to carry the exact confirmed
# preview quote into reserve.
fusion_jobs_routes.FusionJobCreate = ConfirmedFusionJobCreate
fusion_jobs_routes.FusionOrchestrator = ConfirmedPricingFusionOrchestrator


def build_router() -> APIRouter:
    r = APIRouter()
    r.include_router(health_router)
    r.include_router(fusion_jobs_routes.router)
    return r
