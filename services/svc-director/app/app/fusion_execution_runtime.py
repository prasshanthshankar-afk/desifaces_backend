"""Runtime installation of the parallel parent-priced V3 Story Fusion service."""

from . import fusion_execution as _fusion_execution
from . import fusion_execution_parent_pricing as _parent_pricing
from . import fusion_execution_performance as _performance
from . import fusion_execution_parallel_dispatch as _parallel_dispatch
from .fusion_execution_parallel_dispatch import (
    ParallelOrphanReconciledParentPricedSceneFusionExecutionService,
)
from .fusion_execution_performance import PooledFusionStudioClient
from .fusion_input_performance import compile_children_performant

# Keep svc-fusion as provider owner while using pooled HTTP for multi-person scenes.
_fusion_execution.FusionStudioClient = PooledFusionStudioClient

# Remove the serial N-by-2 media-resolution phase before provider fan-out. The
# performant compiler deduplicates repeated Face assets and resolves unique Face /
# Audio read URLs concurrently while preserving the canonical child payload.
_fusion_execution._compile_children = compile_children_performant
_parent_pricing._compile_children = compile_children_performant
_performance._compile_children = compile_children_performant
_parallel_dispatch._compile_children = compile_children_performant

# V3 Story Fusion invariants:
# - one logical parent pricing lifecycle in svc-fusion-extension
# - every dialogue child is internal/bill-to-parent with suppressed pricing
# - lost create responses are reconciled by durable lineage
# - independent input resolution and child creates fan out concurrently after the
#   parent reserve / durable attempt boundary
# - dispatch-spread/progress telemetry is exposed by sync.
_fusion_execution.SceneFusionExecutionService = (
    ParallelOrphanReconciledParentPricedSceneFusionExecutionService
)

__all__ = [
    "ParallelOrphanReconciledParentPricedSceneFusionExecutionService",
    "PooledFusionStudioClient",
    "compile_children_performant",
]
