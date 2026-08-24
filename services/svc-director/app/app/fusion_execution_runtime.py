"""Runtime installation of the resilient V3 Story Fusion execution service."""

from . import fusion_execution as _fusion_execution
from .fusion_execution_resilient import ResilientSceneFusionExecutionService

# studio_e2e_routes imports SceneFusionExecutionService after this module is loaded
# by studio_routes_runtime, so the additive V3 runtime receives failed-child-only
# retry behavior without altering owner-service responsibilities.
_fusion_execution.SceneFusionExecutionService = ResilientSceneFusionExecutionService

__all__ = ["ResilientSceneFusionExecutionService"]
