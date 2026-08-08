from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.repos.tts_catalog_repo import TTSCatalogRepository


class TTSVoiceResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class TTSVoiceResolutionRequest:
    provider_code: str
    model_code: str
    canonical_locale: str

    requested_voice: Optional[str] = None
    requested_gender: Optional[str] = None


@dataclass(frozen=True)
class ResolvedTTSVoice:
    voice_id: str

    provider_code: str
    model_code: str

    voice_name: str
    canonical_locale: str
    home_locale: Optional[str]
    accent_code: str

    gender: Optional[str]
    voice_type: Optional[str]

    is_default: bool
    is_native_fit: bool
    is_recommended: bool

    quality_score: Optional[float]


class TTSVoiceResolver:
    """
    Resolve a provider-native voice using SQL-backed capability data.

    No female-first/male-first policy exists here.

    Gender is used only when the caller explicitly supplies it.

    Automatic selection consumes the repository's deterministic
    SQL-backed candidate ordering. No provider or locale preference
    is encoded in application source.
    """

    def __init__(self, catalog: TTSCatalogRepository):
        self.catalog = catalog

    @staticmethod
    def _normalize_gender(
        value: Optional[str],
    ) -> Optional[str]:
        raw = str(value or "").strip().lower()

        if raw in {"female", "male", "neutral"}:
            return raw

        if raw in {"", "auto", "unspecified"}:
            return None

        raise TTSVoiceResolutionError(
            f"invalid_voice_gender:{raw}"
        )

    async def resolve(
        self,
        request: TTSVoiceResolutionRequest,
    ) -> ResolvedTTSVoice:
        provider = str(
            request.provider_code or ""
        ).strip()

        model = str(request.model_code or "").strip()

        locale = str(
            request.canonical_locale or ""
        ).strip()

        if not provider:
            raise TTSVoiceResolutionError(
                "missing_provider_code"
            )

        if not model:
            raise TTSVoiceResolutionError(
                "missing_model_code"
            )

        if not locale:
            raise TTSVoiceResolutionError(
                "missing_canonical_locale"
            )

        requested_voice = str(
            request.requested_voice or ""
        ).strip()

        if requested_voice.lower() in {"", "auto"}:
            requested_voice = None

        requested_gender = self._normalize_gender(
            request.requested_gender
        )

        candidates = await self.catalog.list_voice_candidates(
            provider_code=provider,
            model_code=model,
            canonical_locale=locale,
            requested_voice=requested_voice,
            requested_gender=requested_gender,
        )

        if not candidates:
            if requested_voice:
                raise TTSVoiceResolutionError(
                    "requested_voice_not_eligible:"
                    f"{provider}/{model}/"
                    f"{locale}/{requested_voice}"
                )

            raise TTSVoiceResolutionError(
                "no_eligible_tts_voice:"
                f"{provider}/{model}/{locale}"
            )

        if requested_voice and len(candidates) > 1:
            raise TTSVoiceResolutionError(
                "duplicate_requested_voice_candidates:"
                f"{provider}/{model}/"
                f"{requested_voice}"
            )

        chosen = candidates[0]

        return ResolvedTTSVoice(
            voice_id=chosen.voice_id,
            provider_code=chosen.provider_code,
            model_code=chosen.model_code,
            voice_name=chosen.voice_name,
            canonical_locale=chosen.capability_locale,
            home_locale=chosen.home_locale,
            accent_code=chosen.accent_code,
            gender=chosen.gender,
            voice_type=chosen.voice_type,
            is_default=chosen.is_default,
            is_native_fit=chosen.is_native_fit,
            is_recommended=chosen.is_recommended,
            quality_score=chosen.quality_score,
        )
