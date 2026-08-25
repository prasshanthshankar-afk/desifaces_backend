"""Runtime installation of the parallel parent-priced V3 Story Fusion service."""

import os
from typing import Any
from uuid import UUID

import httpx

from . import fusion_execution as _fusion_execution
from . import fusion_execution_parent_pricing as _parent_pricing
from . import fusion_execution_performance as _performance
from . import fusion_execution_parallel_dispatch as _parallel_dispatch
from .fusion_execution_background_read import (
    BackgroundFinalizedParallelSceneFusionExecutionService,
)
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


class V3ResilientSceneStitchClient(_fusion_execution.SceneStitchClient):
    """Allow server-side scene assembly enough time for resilient segment download.

    The generic client historically used a 240 second HTTP timeout. A V3 scene can
    contain dozens of provider MP4s and the Fusion Extension now retries transient
    per-segment reads before FFmpeg. The coordinator must not abandon that legitimate
    recovery work while the extension is still assembling the scene.
    """

    def __init__(self, *, base_url: str, timeout_seconds: float | None = None) -> None:
        if timeout_seconds is None:
            try:
                timeout_seconds = float(
                    os.getenv("DF_V3_SCENE_STITCH_HTTP_TIMEOUT_SECONDS", "900")
                )
            except Exception:
                timeout_seconds = 900.0
        super().__init__(
            base_url=base_url,
            timeout_seconds=max(300.0, min(1800.0, float(timeout_seconds))),
        )


async def _verify_suppressed_child_pricing_without_generation_consent(
    self,
    *,
    headers: dict[str, str],
    child: dict[str, Any],
    stage_run_id: UUID,
) -> dict[str, Any]:
    """Verify internal child pricing without granting generation consent.

    svc-fusion's generic request validator requires external_provider_ok for both
    pricing preview and generation. For an internal bill-to-parent child, the
    pricing preview is a local no-charge/suppression contract check and does not
    submit media to the external provider. Set consent=True only on this ephemeral
    preview payload so payload/provider validation can run. Actual child creation
    remains unchanged and still requires the user's explicit external-provider
    consent at Director dispatch time and again at svc-fusion /jobs validation.
    """

    turn_id = child["dialogue_turn_id"]
    payload = _parent_pricing._stamp_internal_child(
        child["payload"],
        stage_run_id=stage_run_id,
        dialogue_turn_id=turn_id,
    )

    preview_payload = dict(payload)
    consent = _parent_pricing._as_dict(preview_payload.get("consent"))
    consent["external_provider_ok"] = True
    preview_payload["consent"] = consent

    preview = await self._fusion_post(
        "/jobs/pricing/preview",
        headers=headers,
        payload=preview_payload,
    )
    if not _parent_pricing._pricing_is_suppressed(preview):
        raise _parent_pricing.SceneFusionBridgeError(
            f"fusion_child_pricing_not_suppressed:{turn_id}"
        )

    return {
        "dialogue_turn_id": turn_id,
        "participant_id": child["participant_id"],
        "display_name": child["display_name"],
        "sequence_no": child["sequence_no"],
        "request_nonce": _parent_pricing._clean(
            _parent_pricing._as_dict(payload.get("provider_options")).get(
                "v3_request_nonce"
            )
        ),
        "pricing_suppressed": True,
        "pricing": _parent_pricing._as_dict(preview.get("pricing")),
        "pricing_summary": _parent_pricing._as_dict(
            preview.get("pricing_summary")
        ),
        "retry_scope": child.get("retry_scope"),
    }


_fusion_execution.FusionStudioClient = V3ParallelFusionStudioClient
_fusion_execution.SceneStitchClient = V3ResilientSceneStitchClient

_fusion_execution._compile_children = compile_children_performant
_parent_pricing._compile_children = compile_children_performant
_performance._compile_children = compile_children_performant
_parallel_dispatch._compile_children = compile_children_performant

# Pricing preview of an internal child must be possible before the user grants
# external-provider generation consent. This patches only the non-generating
# suppression verification. _create_internal_child and /jobs keep the original
# consent requirement intact.
_parent_pricing.ParentPricedSceneFusionExecutionService._verify_child_pricing_suppressed = (
    _verify_suppressed_child_pricing_without_generation_consent
)

ParallelOrphanReconciledParentPricedSceneFusionExecutionService.pricing_concurrency = 32
ParallelOrphanReconciledParentPricedSceneFusionExecutionService.status_concurrency = 32
ParallelOrphanReconciledParentPricedSceneFusionExecutionService.child_pricing_concurrency = 32
BackgroundFinalizedParallelSceneFusionExecutionService.pricing_concurrency = 32
BackgroundFinalizedParallelSceneFusionExecutionService.status_concurrency = 32
BackgroundFinalizedParallelSceneFusionExecutionService.child_pricing_concurrency = 32

# V3 Story Fusion invariants:
# - one logical parent pricing lifecycle in svc-fusion-extension
# - every dialogue child is internal/bill-to-parent with suppressed pricing
# - input resolution and child creates fan out concurrently
# - svc-fusion-extension owns deterministic scene assembly and final media
# - HTTP sync becomes read-only while the background coordinator is enabled
# - dispatch/progress/stitch telemetry remains durable for performance certification.
_fusion_execution.SceneFusionExecutionService = (
    BackgroundFinalizedParallelSceneFusionExecutionService
)

__all__ = [
    "BackgroundFinalizedParallelSceneFusionExecutionService",
    "ParallelOrphanReconciledParentPricedSceneFusionExecutionService",
    "V3ParallelFusionStudioClient",
    "V3ResilientSceneStitchClient",
    "compile_children_performant",
]
