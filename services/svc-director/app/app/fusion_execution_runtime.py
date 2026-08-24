"""Runtime installation of the performant resilient parent-priced V3 Story Fusion service."""

from . import fusion_execution as _fusion_execution
from .fusion_execution_parent_pricing import ParentPricedSceneFusionExecutionService
from .fusion_execution_performance import PooledFusionStudioClient

# Keep svc-fusion as provider owner while using pooled HTTP for multi-person scenes.
_fusion_execution.FusionStudioClient = PooledFusionStudioClient

# svc-fusion-extension owns one logical scene pricing lifecycle. Every svc-fusion
# dialogue child is internal/bill-to-parent and must prove pricing suppression before
# the single parent reservation can be created.
_fusion_execution.SceneFusionExecutionService = ParentPricedSceneFusionExecutionService

__all__ = [
    "ParentPricedSceneFusionExecutionService",
    "PooledFusionStudioClient",
]
