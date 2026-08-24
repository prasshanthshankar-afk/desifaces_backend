"""Runtime installation of the parallel parent-priced V3 Story Fusion service."""

import httpx

from . import fusion_execution as _fusion_execution
from . import fusion_execution_parent_pricing as _parent_pricing
from . import fusion_execution_performance as _performance
from . import fusion_execution_parallel_dispatch as _parallel_dispatch
from .fusion_execution_parallel_dispatch import (
    ParallelOrphanReconciledParentPricedSceneFusionExecutionService,
)
from .fusion_execution_performance import PooledFusionStudioClient
from .fusion_input_performance import compile_children_performant


class V3ParallelFusionStudioClient(PooledFusionStudioClient):
    """Pooled internal client sized for a full V3 multi-person scene fan-out."""

    def __init__(self, *, base_url: str, timeout_seconds: float = 45.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            limits=httpx.Limits(
                max_connections=40,
                max_keepalive_connections=32,
            ),
        )


# Keep svc-fusion as provider owner while allowing all 28 current Story children to
# reach its API concurrently. Provider/account rate limits remain the only legitimate
# reason to lower execution concurrency in deployment configuration.
_fusion_execution.FusionStudioClient = V3ParallelFusionStudioClient

# Remove the serial N-by-2 media-resolution phase before provider fan-out. The
# performant compiler deduplicates repeated Face assets and resolves unique Face /
# Audio read URLs concurrently while preserving the canonical child payload.
_fusion_execution._compile_children = compile_children_performant
_parent_pricing._compile_children = compile_children_performant
_performance._compile_children = compile_children_performant
_parallel_dispatch._compile_children = compile_children_performant

# Align the coordination layer with the V3 28-turn workload. These affect internal
# preview/status coordination only; they do not change pricing or provider ownership.
ParallelOrphanReconciledParentPricedSceneFusionExecutionService.pricing_concurrency = 32
ParallelOrphanReconciledParentPricedSceneFusionExecutionService.status_concurrency = 32
ParallelOrphanReconciledParentPricedSceneFusionExecutionService.child_pricing_concurrency = 32

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
    "V3ParallelFusionStudioClient",
    "compile_children_performant",
]
