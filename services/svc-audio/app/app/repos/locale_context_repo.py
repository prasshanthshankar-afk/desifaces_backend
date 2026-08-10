from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass(frozen=True)
class LocaleContextFacts:
    locale: str
    language_code: str
    region_code: Optional[str]
    country_code: Optional[str]


@dataclass(frozen=True)
class LocaleContextCandidate:
    locale: str
    language_code: str
    match_score: float
    matched_context_count: int


class LocaleContextRepository:
    """
    Read-only repository for locale context refinement.

    Geographic/accent knowledge is read from SQL masterdata.
    No language -> country or country -> locale mapping exists here.
    """

    def __init__(self, pool: Any):
        self.pool = pool

    async def get_locale_facts(
        self,
        locale: str,
    ) -> Optional[LocaleContextFacts]:
        row = await self.pool.fetchrow(
            """
            SELECT
                locale,
                language_code,
                region_code,
                country_code
            FROM public.tts_locales
            WHERE locale = $1
              AND is_enabled = true
            LIMIT 1
            """,
            locale,
        )

        if not row:
            return None

        return LocaleContextFacts(
            locale=str(row["locale"]),
            language_code=str(
                row["language_code"] or ""
            ).strip().lower(),
            region_code=(
                str(row["region_code"]).strip()
                if row["region_code"] is not None
                else None
            ),
            country_code=(
                str(row["country_code"]).strip()
                if row["country_code"] is not None
                else None
            ),
        )

    async def list_matching_candidates(
        self,
        *,
        language_code: str,
        country_code: Optional[str],
        region_code: Optional[str],
        accent_code: Optional[str],
        dialect_code: Optional[str],
        requested_context_count: int,
    ) -> List[LocaleContextCandidate]:
        rows = await self.pool.fetch(
            """
            SELECT
                l.locale,
                l.language_code,
                SUM(r.match_weight)::float8
                    AS match_score,
                COUNT(
                    DISTINCT r.context_type
                )::int AS matched_context_count

            FROM public.tts_locale_context_rules r

            JOIN public.tts_locales l
              ON l.locale = r.locale
             AND l.is_enabled = true

            WHERE l.language_code = $1

              -- Context refinement targets an exact/regional locale.
              AND (
                    l.region_code IS NOT NULL
                    OR l.country_code IS NOT NULL
                  )

              AND r.is_enabled = true

              AND (
                    (
                        $2::text IS NOT NULL
                        AND r.context_type = 'country'
                        AND lower(btrim(r.context_value))
                            = lower(btrim($2))
                    )
                    OR
                    (
                        $3::text IS NOT NULL
                        AND r.context_type = 'region'
                        AND lower(btrim(r.context_value))
                            = lower(btrim($3))
                    )
                    OR
                    (
                        $4::text IS NOT NULL
                        AND r.context_type = 'accent'
                        AND lower(btrim(r.context_value))
                            = lower(btrim($4))
                    )
                    OR
                    (
                        $5::text IS NOT NULL
                        AND r.context_type = 'dialect'
                        AND lower(btrim(r.context_value))
                            = lower(btrim($5))
                    )
                  )

            GROUP BY
                l.locale,
                l.language_code

            -- A candidate must satisfy every supplied context
            -- dimension. Partial geographic/accent matches do not
            -- silently override explicit user intent.
            HAVING COUNT(
                DISTINCT r.context_type
            ) = $6

            ORDER BY
                match_score DESC,
                matched_context_count DESC,
                l.locale ASC
            """,
            language_code,
            country_code,
            region_code,
            accent_code,
            dialect_code,
            int(requested_context_count),
        )

        return [
            LocaleContextCandidate(
                locale=str(row["locale"]),
                language_code=str(
                    row["language_code"]
                ),
                match_score=float(
                    row["match_score"]
                ),
                matched_context_count=int(
                    row["matched_context_count"]
                ),
            )
            for row in rows
        ]
