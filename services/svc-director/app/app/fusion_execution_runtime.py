"""Runtime installation of the parallel parent-priced V3 Story Fusion service."""

from . import fusion_execution as _fusion_execution
from .fusion_execution_parallel_dispatch import (
    ParallelOrphanReconciledParentPricedSceneFusionExecutionService,
)
from .fusion_execution_performance import PooledFusionStudioClient

# Keep svc-fusion as provider owner while using pooled HTTP for multi-person scenes.
_fusion_execution.FusionStudioClient = PooledFusionStudioClient

# V3 Story Fusion invariants:
# - one logical parent pricing lifecycle in svc-fusion-extension
# - every dialogue child is internal/bill-to-parent with suppressed pricing
# - lost create responses are reconciled by durable lineage
# - independent child creates fan out concurrently after the parent reserve and durable
#   attempt creation, with dispatch-spread/progress telemetry exposed by sync.
_fusion_execution.SceneFusionExecutionService = (
    ParallelOrphanReconciledParentPricedSceneFusionExecutionService
)

__all__ = [
    "ParallelOrphanReconciledParentPricedSceneFusionExecutionService",
    "PooledFusionStudioClient",
]
