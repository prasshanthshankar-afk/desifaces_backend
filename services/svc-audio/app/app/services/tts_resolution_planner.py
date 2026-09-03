from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.services.tts_model_resolver import (
    ResolvedTTSModel,
    TTSModelResolutionRequest,
    TTSModelResolver,
)
from app.services.tts_voice_resolver import (
    ResolvedTTSVoice,
    TTSVoiceResolutionRequest,
    TTSVoiceResolver,
)


class TTSResolutionPlanError(ValueError):
    pass


@dataclass(frozen=True)
class TTSResolutionPlanRequest:
    """
    Input to the provider-neutral resolution pipeline.

    requested_locale is user/client intent.
    Geography/accent values are context only and never imply a provider.
    """

    requested_locale: str
    text_length: int

    output_format: str = "mp3"

    requested_voice: Optional[str] = None
    requested_gender: Optional[str] = None

    country_code: Optional[str] = None
    region_code: Optional[str] = None
    accent_code: Optional[str] = None
    dialect_code: Optional[str] = None

    requires_style: bool = False
    requires_emotion: bool = False
    requires_streaming: bool = False


@dataclass(frozen=True)
class TTSResolutionPlan:
    requested_locale: str
    canonical_locale: str

    provider_code: str
    adapter_key: str

    model_code: str
    provider_model_id: Optional[str]

    voice_id: str
    voice_name: str
    voice_gender: Optional[str]
    voice_home_locale: Optional[str]

    provider_locale_code: Optional[str]
    provider_language_code: Optional[str]

    language_code: str
    capability_scope: str

    quality_class: Optional[str]
    model_quality_score: Optional[float]
    voice_quality_score: Optional[float]

    routing_policy_code: str
    masterdata_revision: int

    country_code: Optional[str]
    region_code: Optional[str]
    accent_code: Optional[str]


class TTSResolutionPlanner:
    """
    Compose the SQL-backed resolution stages:

        raw locale
          -> LocaleResolver
          -> TTSModelResolver
          -> TTSVoiceResolver
          -> immutable TTSResolutionPlan

    The planner contains no provider/country/language routing tables.

    It intentionally does NOT synthesize audio.
    """

    def __init__(
        self,
        *,
        locale_resolver,
        model_resolver: TTSModelResolver,
        voice_resolver: TTSVoiceResolver,
        context_resolver=None,
    ):
        self.locale_resolver = locale_resolver
        self.model_resolver = model_resolver
        self.voice_resolver = voice_resolver
        self.context_resolver = context_resolver

    async def _resolve_model(
        self,
        request: TTSResolutionPlanRequest,
        *,
        canonical_locale: str,
        requested_voice: Optional[str],
    ) -> ResolvedTTSModel:
        return await self.model_resolver.resolve(
            TTSModelResolutionRequest(
                canonical_locale=canonical_locale,
                text_length=int(request.text_length),
                output_format=request.output_format,
                country_code=request.country_code,
                region_code=request.region_code,
                accent_code=request.accent_code,
                requires_style=bool(request.requires_style),
                requires_emotion=bool(request.requires_emotion),
                requires_streaming=bool(request.requires_streaming),
                requested_voice=requested_voice,
                requested_gender=request.requested_gender,
            )
        )

    async def resolve(
        self,
        request: TTSResolutionPlanRequest,
    ) -> TTSResolutionPlan:
        raw_locale = str(request.requested_locale or "").strip()

        if not raw_locale:
            raise TTSResolutionPlanError("missing_requested_locale")

        try:
            resolved_locale = await self.locale_resolver.resolve(raw_locale)
        except Exception as exc:
            raise TTSResolutionPlanError(
                f"locale_resolution_failed:{exc}"
            ) from exc

        canonical_locale = str(
            getattr(resolved_locale, "locale", "") or ""
        ).strip()

        if not canonical_locale:
            raise TTSResolutionPlanError(
                "invalid_locale_resolution_result"
            )

        if self.context_resolver is not None:
            try:
                context_resolution = await self.context_resolver.resolve(
                    canonical_locale=canonical_locale,
                    country_code=request.country_code,
                    region_code=request.region_code,
                    accent_code=request.accent_code,
                    dialect_code=request.dialect_code,
                )
            except Exception as exc:
                raise TTSResolutionPlanError(
                    f"context_resolution_failed:{exc}"
                ) from exc

            canonical_locale = str(
                getattr(context_resolution, "locale", "") or ""
            ).strip()

            if not canonical_locale:
                raise TTSResolutionPlanError(
                    "invalid_context_resolution_result"
                )

        requested_voice = str(request.requested_voice or "").strip()
        if requested_voice.lower() in {"", "auto"}:
            requested_voice = ""

        effective_requested_voice: Optional[str] = requested_voice or None

        try:
            model = await self._resolve_model(
                request,
                canonical_locale=canonical_locale,
                requested_voice=effective_requested_voice,
            )
        except Exception as exc:
            # A voice chosen earlier can become stale or ineligible after the
            # user changes Face gender, locale, or catalog revision. Treat the
            # explicit voice as a preference and re-resolve to the best eligible
            # voice for the authoritative requested gender instead of surfacing
            # an internal model-resolution error when a compatible voice exists.
            if requested_voice and "requested_voice_not_eligible_for_any_model" in str(exc):
                effective_requested_voice = None
                try:
                    model = await self._resolve_model(
                        request,
                        canonical_locale=canonical_locale,
                        requested_voice=None,
                    )
                except Exception as fallback_exc:
                    raise TTSResolutionPlanError(
                        f"model_resolution_failed:{fallback_exc}"
                    ) from fallback_exc
            else:
                raise TTSResolutionPlanError(
                    f"model_resolution_failed:{exc}"
                ) from exc

        try:
            voice: ResolvedTTSVoice = await self.voice_resolver.resolve(
                TTSVoiceResolutionRequest(
                    provider_code=model.provider_code,
                    model_code=model.model_code,
                    canonical_locale=canonical_locale,
                    requested_voice=effective_requested_voice,
                    requested_gender=request.requested_gender,
                )
            )
        except Exception as exc:
            # Defensive second chance for catalog races or a voice that was
            # valid for model selection but became unavailable before voice
            # resolution. Preserve locale/gender intent and fall back only the
            # stale voice id.
            if effective_requested_voice and "requested_voice_not_eligible" in str(exc):
                try:
                    voice = await self.voice_resolver.resolve(
                        TTSVoiceResolutionRequest(
                            provider_code=model.provider_code,
                            model_code=model.model_code,
                            canonical_locale=canonical_locale,
                            requested_voice=None,
                            requested_gender=request.requested_gender,
                        )
                    )
                except Exception as fallback_exc:
                    raise TTSResolutionPlanError(
                        f"voice_resolution_failed:{fallback_exc}"
                    ) from fallback_exc
            else:
                raise TTSResolutionPlanError(
                    f"voice_resolution_failed:{exc}"
                ) from exc

        if voice.provider_code != model.provider_code:
            raise TTSResolutionPlanError(
                "provider_resolution_mismatch"
            )

        if voice.model_code != model.model_code:
            raise TTSResolutionPlanError(
                "model_resolution_mismatch"
            )

        if voice.canonical_locale != canonical_locale:
            raise TTSResolutionPlanError(
                "voice_locale_resolution_mismatch"
            )

        return TTSResolutionPlan(
            requested_locale=raw_locale,
            canonical_locale=canonical_locale,
            provider_code=model.provider_code,
            adapter_key=model.adapter_key,
            model_code=model.model_code,
            provider_model_id=model.provider_model_id,
            voice_id=voice.voice_id,
            voice_name=voice.voice_name,
            voice_gender=voice.gender,
            voice_home_locale=voice.home_locale,
            provider_locale_code=model.provider_locale_code,
            provider_language_code=model.provider_language_code,
            language_code=model.language_code,
            capability_scope=model.capability_scope,
            quality_class=model.quality_class,
            model_quality_score=model.quality_score,
            voice_quality_score=voice.quality_score,
            routing_policy_code=model.routing_policy_code,
            masterdata_revision=model.masterdata_revision,
            country_code=request.country_code,
            region_code=request.region_code,
            accent_code=request.accent_code,
        )
