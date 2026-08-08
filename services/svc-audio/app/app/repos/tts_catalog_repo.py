from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    import asyncpg


@dataclass(frozen=True)
class TTSRoutingPolicy:
    policy_code: str
    require_approved_capability: bool
    require_approved_quality: bool
    allow_provider_fallback: bool


@dataclass(frozen=True)
class TTSModelCandidate:
    provider_code: str
    adapter_key: str

    model_code: str
    provider_model_id: Optional[str]
    quality_class: Optional[str]

    canonical_locale: str
    language_code: str

    provider_locale_code: Optional[str]
    provider_language_code: Optional[str]
    capability_scope: str

    max_input_chars: Optional[int]

    supports_streaming: bool
    supports_multilingual: bool
    supports_ssml: bool
    supports_styles: bool
    supports_emotions: bool
    supports_pace: bool

    quality_score: Optional[float]


@dataclass(frozen=True)
class TTSVoiceCandidate:
    voice_id: str
    provider_code: str
    model_code: str

    voice_name: str
    home_locale: Optional[str]
    capability_locale: str
    accent_code: str

    gender: Optional[str]
    voice_type: Optional[str]

    is_default: bool
    supports_styles: bool

    is_native_fit: bool
    is_recommended: bool
    quality_score: Optional[float]
    selection_priority: int = 0


class TTSCatalogRepository:
    """
    Read-only access to SQL-backed TTS routing masterdata.

    No provider, geography, language, gender, or locale preference
    is encoded in application source.
    """

    def __init__(self, pool: Any):
        self.pool = pool

    async def get_default_routing_policy(
        self,
    ) -> Optional[TTSRoutingPolicy]:
        row = await self.pool.fetchrow(
            """
            SELECT
                policy_code,
                require_approved_capability,
                require_approved_quality,
                allow_provider_fallback
            FROM public.tts_routing_policies
            WHERE is_default = true
              AND is_enabled = true
            LIMIT 1
            """
        )

        if not row:
            return None

        return TTSRoutingPolicy(
            policy_code=str(row["policy_code"]),
            require_approved_capability=bool(
                row["require_approved_capability"]
            ),
            require_approved_quality=bool(
                row["require_approved_quality"]
            ),
            allow_provider_fallback=bool(
                row["allow_provider_fallback"]
            ),
        )

    async def get_masterdata_revision(self) -> int:
        row = await self.pool.fetchrow(
            """
            SELECT revision
            FROM public.masterdata_revision
            WHERE domain = 'tts'
            LIMIT 1
            """
        )

        return int(row["revision"]) if row else 0

    async def list_routing_enabled_model_candidates(
        self,
        *,
        canonical_locale: str,
        text_length: int,
        output_format: str,
        requires_style: bool,
        requires_emotion: bool,
        requires_streaming: bool,
        require_approved_capability: bool,
        require_approved_quality: bool,
    ) -> List[TTSModelCandidate]:
        rows = await self.pool.fetch(
            """
            WITH req AS (
                SELECT
                    locale,
                    language_code
                FROM public.tts_locales
                WHERE locale = $1
                  AND is_enabled = true
            )
            SELECT DISTINCT
                p.provider_code,
                p.adapter_key,

                m.model_code,
                m.provider_model_id,
                m.quality_class,

                r.locale AS canonical_locale,
                r.language_code,

                mlc.provider_locale_code,
                mlng.provider_language_code,

                CASE
                    WHEN mlc.locale IS NOT NULL
                        THEN 'locale'
                    ELSE 'language'
                END AS capability_scope,

                CASE
                    WHEN mlc.locale IS NOT NULL THEN 0
                    ELSE 1
                END AS capability_rank,

                m.max_input_chars,

                m.supports_streaming,
                m.supports_multilingual,
                m.supports_ssml,
                m.supports_styles,
                m.supports_emotions,
                m.supports_pace,

                COALESCE(
                    (
                        SELECT max(q.overall_score)
                        FROM public.tts_quality_profiles q
                        WHERE q.provider_code = p.provider_code
                          AND q.model_code = m.model_code
                          AND q.is_approved = true
                          AND (
                                q.locale = r.locale
                                OR q.locale IS NULL
                              )
                    ),
                    (
                        SELECT max(vl.quality_score)
                        FROM public.tts_voice_model_capabilities vm
                        JOIN public.tts_voice_locale_capabilities vl
                          ON vl.voice_id = vm.voice_id
                         AND vl.locale = r.locale
                         AND vl.is_enabled = true
                         AND vl.is_approved = true
                        WHERE vm.provider_code = p.provider_code
                          AND vm.model_code = m.model_code
                          AND vm.is_enabled = true
                          AND vm.is_approved = true
                    )
                ) AS quality_score

            FROM req r

            JOIN public.tts_providers p
              ON p.is_enabled = true
             AND p.routing_enabled = true

            JOIN public.tts_provider_models m
              ON m.provider_code = p.provider_code
             AND m.is_enabled = true
             AND m.routing_enabled = true

            LEFT JOIN public.tts_model_locale_capabilities mlc
              ON mlc.provider_code = m.provider_code
             AND mlc.model_code = m.model_code
             AND mlc.locale = r.locale
             AND mlc.is_enabled = true
             AND (
                    $7 = false
                    OR mlc.is_approved = true
                 )

            LEFT JOIN public.tts_model_language_capabilities mlng
              ON mlng.provider_code = m.provider_code
             AND mlng.model_code = m.model_code
             AND mlng.language_code = r.language_code
             AND mlng.is_enabled = true
             AND (
                    $7 = false
                    OR mlng.is_approved = true
                 )

            WHERE (
                    mlc.locale IS NOT NULL
                    OR mlng.language_code IS NOT NULL
                  )

              AND (
                    m.max_input_chars IS NULL
                    OR $2 <= m.max_input_chars
                  )

              AND (
                    $4 = false
                    OR m.supports_styles = true
                  )

              AND (
                    $5 = false
                    OR m.supports_emotions = true
                  )

              AND (
                    $6 = false
                    OR m.supports_streaming = true
                  )

              AND (
                    jsonb_array_length(m.output_formats_json) = 0
                    OR m.output_formats_json ? $3
                  )

              AND (
                    $8 = false
                    OR EXISTS (
                        SELECT 1
                        FROM public.tts_quality_profiles q2
                        WHERE q2.provider_code = p.provider_code
                          AND q2.model_code = m.model_code
                          AND q2.is_approved = true
                          AND (
                                q2.locale = r.locale
                                OR q2.locale IS NULL
                              )
                    )
                  )

            ORDER BY
                quality_score DESC NULLS LAST,
                capability_rank,
                p.provider_code,
                m.model_code
            """,
            canonical_locale,
            int(text_length),
            str(output_format).lower(),
            bool(requires_style),
            bool(requires_emotion),
            bool(requires_streaming),
            bool(require_approved_capability),
            bool(require_approved_quality),
        )

        result: List[TTSModelCandidate] = []

        for row in rows:
            result.append(
                TTSModelCandidate(
                    provider_code=str(row["provider_code"]),
                    adapter_key=str(row["adapter_key"]),
                    model_code=str(row["model_code"]),
                    provider_model_id=(
                        str(row["provider_model_id"])
                        if row["provider_model_id"] is not None
                        else None
                    ),
                    quality_class=(
                        str(row["quality_class"])
                        if row["quality_class"] is not None
                        else None
                    ),
                    canonical_locale=str(
                        row["canonical_locale"]
                    ),
                    language_code=str(row["language_code"]),
                    provider_locale_code=(
                        str(row["provider_locale_code"])
                        if row["provider_locale_code"] is not None
                        else None
                    ),
                    provider_language_code=(
                        str(row["provider_language_code"])
                        if row["provider_language_code"] is not None
                        else None
                    ),
                    capability_scope=str(
                        row["capability_scope"]
                    ),
                    max_input_chars=(
                        int(row["max_input_chars"])
                        if row["max_input_chars"] is not None
                        else None
                    ),
                    supports_streaming=bool(
                        row["supports_streaming"]
                    ),
                    supports_multilingual=bool(
                        row["supports_multilingual"]
                    ),
                    supports_ssml=bool(
                        row["supports_ssml"]
                    ),
                    supports_styles=bool(
                        row["supports_styles"]
                    ),
                    supports_emotions=bool(
                        row["supports_emotions"]
                    ),
                    supports_pace=bool(
                        row["supports_pace"]
                    ),
                    quality_score=(
                        float(row["quality_score"])
                        if row["quality_score"] is not None
                        else None
                    ),
                )
            )

        return result

    async def list_voice_candidates(
        self,
        *,
        provider_code: str,
        model_code: str,
        canonical_locale: str,
        requested_voice: Optional[str],
        requested_gender: Optional[str],
    ) -> List[TTSVoiceCandidate]:
        rows = await self.pool.fetch(
            """
            SELECT
                v.id::text AS voice_id,
                v.provider AS provider_code,
                vm.model_code,

                v.voice_name,
                v.locale AS home_locale,
                vl.locale AS capability_locale,
                vl.accent_code,

                v.gender,
                v.voice_type,

                v.is_default,
                v.supports_styles,

                vl.is_native_fit,
                vl.is_recommended,
                vl.quality_score,
                vl.selection_priority

            FROM public.tts_voices v

            JOIN public.tts_voice_model_capabilities vm
              ON vm.voice_id = v.id
             AND vm.provider_code = v.provider
             AND vm.model_code = $2
             AND vm.is_enabled = true
             AND vm.is_approved = true

            JOIN public.tts_voice_locale_capabilities vl
              ON vl.voice_id = v.id
             AND vl.locale = $3
             AND vl.is_enabled = true
             AND vl.is_approved = true

            WHERE v.provider = $1

              AND (
                    $4::text IS NULL
                    OR lower(v.voice_name) = lower($4)
                  )

              AND (
                    $5::text IS NULL
                    OR lower(coalesce(v.gender, '')) = lower($5)
                  )

            ORDER BY
                vl.is_recommended DESC,
                vl.selection_priority DESC,
                v.is_default DESC,
                vl.is_native_fit DESC,
                vl.quality_score DESC NULLS LAST,
                v.voice_name ASC
            """,
            provider_code,
            model_code,
            canonical_locale,
            requested_voice,
            requested_gender,
        )

        result: List[TTSVoiceCandidate] = []

        for row in rows:
            result.append(
                TTSVoiceCandidate(
                    voice_id=str(row["voice_id"]),
                    provider_code=str(
                        row["provider_code"]
                    ),
                    model_code=str(row["model_code"]),
                    voice_name=str(row["voice_name"]),
                    home_locale=(
                        str(row["home_locale"])
                        if row["home_locale"] is not None
                        else None
                    ),
                    capability_locale=str(
                        row["capability_locale"]
                    ),
                    accent_code=str(
                        row["accent_code"] or ""
                    ),
                    gender=(
                        str(row["gender"]).lower()
                        if row["gender"] is not None
                        else None
                    ),
                    voice_type=(
                        str(row["voice_type"])
                        if row["voice_type"] is not None
                        else None
                    ),
                    is_default=bool(row["is_default"]),
                    supports_styles=bool(
                        row["supports_styles"]
                    ),
                    is_native_fit=bool(
                        row["is_native_fit"]
                    ),
                    is_recommended=bool(
                        row["is_recommended"]
                    ),
                    selection_priority=int(row["selection_priority"] or 0),
                    quality_score=(
                        float(row["quality_score"])
                        if row["quality_score"] is not None
                        else None
                    ),
                )
            )

        return result
