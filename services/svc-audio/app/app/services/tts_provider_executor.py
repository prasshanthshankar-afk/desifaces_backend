from __future__ import annotations

from app.services.tts_provider_adapter import (
    TTSProviderSynthesisRequest,
    TTSProviderSynthesisResult,
)
from app.services.tts_provider_registry import (
    TTSProviderAdapterRegistry,
)
from app.services.tts_resolution_planner import (
    TTSResolutionPlan,
)


class TTSProviderExecutor:
    """
    Executes an already-resolved TTS plan.

    Resolution/routing remains DB-driven and outside this class.
    """

    def __init__(self, registry=None):
        self.registry = (
            registry
            if registry is not None
            else TTSProviderAdapterRegistry()
        )

    async def synthesize(
        self,
        *,
        plan: TTSResolutionPlan,
        text: str,
        output_format: str,
        ssml: str | None = None,
        style: str | None = None,
        emotion: str | None = None,
        rate: float = 1.0,
        pitch: float = 0.0,
        volume: float = 0.0,
    ) -> TTSProviderSynthesisResult:
        adapter = self.registry.create(
            plan.adapter_key
        )

        request = TTSProviderSynthesisRequest(
            provider_code=plan.provider_code,
            model_code=plan.model_code,
            provider_model_id=plan.provider_model_id,
            voice_name=plan.voice_name,
            canonical_locale=plan.canonical_locale,
            provider_locale_code=plan.provider_locale_code,
            text=text,
            output_format=output_format,
            ssml=ssml,
            style=style,
            emotion=emotion,
            rate=rate,
            pitch=pitch,
            volume=volume,
        )

        return await adapter.synthesize(request)
