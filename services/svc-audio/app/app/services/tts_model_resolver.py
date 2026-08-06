from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.repos.tts_catalog_repo import (
    TTSCatalogRepository,
    TTSModelCandidate,
)


class TTSModelResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class TTSModelResolutionRequest:
    canonical_locale: str
    text_length: int
    output_format: str = "mp3"

    country_code: Optional[str] = None
    region_code: Optional[str] = None
    accent_code: Optional[str] = None

    requires_style: bool = False
    requires_emotion: bool = False
    requires_streaming: bool = False


@dataclass(frozen=True)
class ResolvedTTSModel:
    provider_code: str
    adapter_key: str

    model_code: str
    provider_model_id: Optional[str]

    canonical_locale: str
    language_code: str

    provider_locale_code: Optional[str]
    provider_language_code: Optional[str]
    capability_scope: str

    quality_class: Optional[str]
    quality_score: Optional[float]

    max_input_chars: Optional[int]

    routing_policy_code: str
    masterdata_revision: int

    country_code: Optional[str]
    region_code: Optional[str]
    accent_code: Optional[str]


class TTSModelResolver:
    """
    Resolve an executable TTS provider/model from SQL-backed masterdata.

    Multi-provider routing behavior:
      * zero candidates -> fail closed
      * eligible candidates are ranked by SQL-backed quality masterdata
      * the highest-ranked candidate is selected deterministically

    Provider preference is not encoded in application source.
    """

    def __init__(self, catalog: TTSCatalogRepository):
        self.catalog = catalog

    @staticmethod
    def _normalize_output_format(value: str) -> str:
        raw = str(value or "").strip().lower()

        if raw in {"", "mp3"}:
            return "mp3"

        if raw in {"wav", "wave", "pcm"}:
            return "wav"

        return raw

    async def resolve(
        self,
        request: TTSModelResolutionRequest,
    ) -> ResolvedTTSModel:
        locale = str(request.canonical_locale or "").strip()

        if not locale:
            raise TTSModelResolutionError(
                "missing_canonical_locale"
            )

        text_length = int(request.text_length)

        if text_length < 0:
            raise TTSModelResolutionError(
                "invalid_text_length"
            )

        policy = await self.catalog.get_default_routing_policy()

        if policy is None:
            raise TTSModelResolutionError(
                "missing_default_tts_routing_policy"
            )

        output_format = self._normalize_output_format(
            request.output_format
        )

        candidates = (
            await self.catalog.list_routing_enabled_model_candidates(
                canonical_locale=locale,
                text_length=text_length,
                output_format=output_format,
                requires_style=bool(request.requires_style),
                requires_emotion=bool(
                    request.requires_emotion
                ),
                requires_streaming=bool(
                    request.requires_streaming
                ),
                require_approved_capability=(
                    policy.require_approved_capability
                ),
                require_approved_quality=(
                    policy.require_approved_quality
                ),
            )
        )

        if not candidates:
            raise TTSModelResolutionError(
                f"no_eligible_tts_model:{locale}"
            )

        chosen: TTSModelCandidate = candidates[0]

        revision = await self.catalog.get_masterdata_revision()

        return ResolvedTTSModel(
            provider_code=chosen.provider_code,
            adapter_key=chosen.adapter_key,
            model_code=chosen.model_code,
            provider_model_id=chosen.provider_model_id,
            canonical_locale=chosen.canonical_locale,
            language_code=chosen.language_code,
            provider_locale_code=chosen.provider_locale_code,
            provider_language_code=(
                chosen.provider_language_code
            ),
            capability_scope=chosen.capability_scope,
            quality_class=chosen.quality_class,
            quality_score=chosen.quality_score,
            max_input_chars=chosen.max_input_chars,
            routing_policy_code=policy.policy_code,
            masterdata_revision=revision,
            country_code=request.country_code,
            region_code=request.region_code,
            accent_code=request.accent_code,
        )
