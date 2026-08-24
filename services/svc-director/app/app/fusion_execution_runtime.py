"""Runtime installation of the performant resilient parent-priced V3 Story Fusion service."""

from . import fusion_execution as _fusion_execution
from .fusion_execution_orphan_recovery import (
    OrphanReconciledParentPricedSceneFusionExecutionService,
)
from .fusion_execution_performance import PooledFusionStudioClient

# Keep svc-fusion as provider owner while using pooled HTTP for multi-person scenes.
_fusion_execution.FusionStudioClient = PooledFusionStudioClient

# svc-fusion-extension owns one logical scene pricing lifecycle. Every svc-fusion
# dialogue child is internal/bill-to-parent and must prove pricing suppression before
# the single parent reservation can be created. Lost create responses are reconciled
# by persisted parent-stage/segment lineage so retries cannot duplicate provider work.
_fusion_execution.SceneFusionExecutionService = (
    OrphanReconciledParentPricedSceneFusionExecutionService
)

__all__ = [
    "OrphanReconciledParentPricedSceneFusionExecutionService",
    "PooledFusionStudioClient",
]
