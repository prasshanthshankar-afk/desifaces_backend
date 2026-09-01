from __future__ import annotations

import asyncio
import logging

from app.services.providers.base import ProviderPrepareInput
from app.services.providers.omnihuman_adapter import OmniHumanAdapter

logger = logging.getLogger("fusion.performance_policy")
_INSTALLED = False


def install_fusion_performance_policy() -> None:
    """Pre-stage OmniHuman Face + Audio inputs concurrently.

    The existing adapter downloads/uploads those two independent inputs
    sequentially before provider submission. Pre-upload them with asyncio.gather,
    then pass the resulting FAL URLs into the untouched adapter. Its normal
    upload step becomes an immediate no-op because both URLs are already FAL
    hosted. On pre-stage failure, fall back to the original preparation path.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    original_prepare = OmniHumanAdapter.prepare

    async def wrapped(self: OmniHumanAdapter, spec: ProviderPrepareInput):
        if (
            bool(getattr(self, "upload_inputs_to_fal", False))
            and spec.resolved_face_url
            and spec.resolved_audio_url
        ):
            try:
                face_suffix = self._suffix_from_url(spec.resolved_face_url, ".png")
                audio_suffix = self._suffix_from_url(spec.resolved_audio_url, ".mp3")
                face_url, audio_url = await asyncio.gather(
                    self._upload_remote_file_to_fal(spec.resolved_face_url, suffix_hint=face_suffix),
                    self._upload_remote_file_to_fal(spec.resolved_audio_url, suffix_hint=audio_suffix),
                )
                staged = ProviderPrepareInput(
                    job_id=spec.job_id,
                    user_id=spec.user_id,
                    request_payload=dict(spec.request_payload or {}),
                    resolved_face_url=face_url,
                    resolved_audio_url=audio_url,
                    reference_image_urls=spec.reference_image_urls,
                )
                logger.info("omnihuman_parallel_input_stage_ok job_id=%s", spec.job_id)
                return await original_prepare(self, staged)
            except Exception:
                logger.exception("omnihuman_parallel_input_stage_failed_falling_back job_id=%s", spec.job_id)

        return await original_prepare(self, spec)

    OmniHumanAdapter.prepare = wrapped  # type: ignore[assignment]
    _INSTALLED = True
