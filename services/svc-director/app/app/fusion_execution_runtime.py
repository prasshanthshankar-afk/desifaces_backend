"""Runtime installation of the performant resilient V3 Story Fusion service."""

from . import fusion_execution as _fusion_execution
from .fusion_execution_performance import (
    PerformantResilientSceneFusionExecutionService,
    PooledFusionStudioClient,
)

# Keep svc-fusion as pricing/provider owner while removing avoidable per-child
# connection setup and heavy status payloads from Director orchestration.
_fusion_execution.FusionStudioClient = PooledFusionStudioClient

# studio_e2e_routes imports SceneFusionExecutionService after this module is loaded
# by studio_routes_runtime. The installed class preserves failed-child-only retry,
# stitch-only recovery, approved upstream lineage and existing pricing contracts.
_fusion_execution.SceneFusionExecutionService = PerformantResilientSceneFusionExecutionService

__all__ = [
    "PerformantResilientSceneFusionExecutionService",
    "PooledFusionStudioClient",
]
