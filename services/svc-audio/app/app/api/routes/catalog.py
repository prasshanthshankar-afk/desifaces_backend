from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import asyncpg
from fastapi import APIRouter, Depends, Query, HTTPException

from app.db import get_pool

router = APIRouter(prefix="/api/audio/catalog", tags=["audio-catalog"])


# --- Catalog compatibility ----------------------------------------------------
# Locale/provider availability is controlled entirely by DB masterdata.
# `market` remains accepted for backward compatibility but does not
# impose geography-specific filtering in application source.


def _normalize_market(market: Optional[str]) -> str:
    return str(market or "global").strip().lower()


def _is_locale_allowed(locale: str, market: str) -> bool:
    return bool(str(locale or "").strip())


def _market_sql_filter(market: str) -> Tuple[str, List[Any]]:
    return "", []


def _order_by_sql(market: str) -> str:
    return "ORDER BY l.locale"


# --- Routes ------------------------------------------------------------------


@router.get("/locales")
async def list_locales(
        market: str = Query(
        "global",
        description="Deprecated compatibility parameter; locale availability is DB-driven.",
    ),
    end_to_end_only: bool = Query(True, description="If true, require both TTS + translation support"),
    enabled_only: bool = Query(True, description="If true, only is_enabled locales"),
    pool: asyncpg.Pool = Depends(get_pool),
) -> Dict[str, Any]:
    where = []
    args: List[Any] = []

    if enabled_only:
        where.append("l.is_enabled = true")
    if end_to_end_only:
        where.append("l.tts_supported = true AND l.translate_supported = true")

    clause = "WHERE " + " AND ".join(where) if where else "WHERE 1=1"

    market_filter_sql, market_args = _market_sql_filter(market)
    args.extend(market_args)

    order_by = _order_by_sql(market)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
              l.locale,
              l.translator_lang,
              l.tts_supported,
              l.translate_supported,
              l.is_enabled,
              l.display_name,
              l.native_name,
              (
                SELECT v.voice_name
                FROM public.tts_voices v
                JOIN public.tts_voice_locale_capabilities vl
                  ON vl.voice_id = v.id
                 AND vl.locale = l.locale
                 AND vl.is_enabled = true
                 AND vl.is_approved = true
                WHERE EXISTS (
                    SELECT 1
                    FROM public.tts_voice_model_capabilities vm
                    JOIN public.tts_provider_models m
                      ON m.provider_code = vm.provider_code
                     AND m.model_code = vm.model_code
                     AND m.is_enabled = true
                     AND m.routing_enabled = true
                    JOIN public.tts_providers p
                      ON p.provider_code = vm.provider_code
                     AND p.is_enabled = true
                     AND p.routing_enabled = true
                    WHERE vm.voice_id = v.id
                      AND vm.provider_code = v.provider
                      AND vm.is_enabled = true
                      AND vm.is_approved = true
                )
                ORDER BY
                  v.is_default DESC,
                  vl.is_recommended DESC,
                  COALESCE(vl.quality_score, 0) DESC,
                  v.voice_name ASC
                LIMIT 1
              ) AS default_voice
            FROM public.tts_locales l
            {clause}
            {market_filter_sql}
            {order_by}
            """,
            *args,
        )

    return {"items": [dict(r) for r in rows]}



@router.get("/countries")
async def list_countries(
    pool: asyncpg.Pool = Depends(get_pool),
) -> Dict[str, Any]:
    """
    Return countries having at least one currently usable TTS target locale.

    Country availability is derived entirely from locale and provider
    capability masterdata.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                l.country_code,
                COALESCE(
                    c.display_name ->> 'en',
                    (SELECT value FROM jsonb_each_text(c.display_name) LIMIT 1),
                    l.country_code
                ) AS display_name,
                COUNT(*) AS locale_count
            FROM public.tts_locales l
            LEFT JOIN public.face_generation_regions c
              ON c.country_code = l.country_code
             AND c.geography_type = 'country'
             AND c.is_active = TRUE
            WHERE l.is_enabled = TRUE
              AND l.tts_supported = TRUE
              AND l.is_user_selectable = TRUE
              AND l.country_code IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM public.tts_voice_locale_capabilities vl
                  JOIN public.tts_voices v
                    ON v.id = vl.voice_id
                  JOIN public.tts_voice_model_capabilities vm
                    ON vm.voice_id = v.id
                   AND vm.provider_code = v.provider
                   AND vm.is_enabled = TRUE
                   AND vm.is_approved = TRUE
                  JOIN public.tts_provider_models m
                    ON m.provider_code = vm.provider_code
                   AND m.model_code = vm.model_code
                   AND m.is_enabled = TRUE
                   AND m.routing_enabled = TRUE
                  JOIN public.tts_providers p
                    ON p.provider_code = vm.provider_code
                   AND p.is_enabled = TRUE
                   AND p.routing_enabled = TRUE
                  WHERE vl.locale = l.locale
                    AND vl.is_enabled = TRUE
                    AND vl.is_approved = TRUE
              )
            GROUP BY l.country_code, c.display_name
            ORDER BY display_name, l.country_code
            """
        )

    return {
        "items": [
            {
                "country_code": r["country_code"],
                "display_name": r["display_name"],
                "locale_count": int(r["locale_count"]),
            }
            for r in rows
        ]
    }


@router.get("/target-languages")
async def list_target_languages(
    country_code: str = Query(
        ...,
        description="ISO 3166-1 alpha-2 country code",
    ),
    pool: asyncpg.Pool = Depends(get_pool),
) -> Dict[str, Any]:
    """
    Return currently usable TTS target locales for the selected country.

    Country/language/provider decisions are not encoded in application
    source; eligibility comes from database masterdata and capabilities.
    """
    normalized_country = str(country_code or "").strip().upper()

    if len(normalized_country) != 2 or not normalized_country.isalpha():
        raise HTTPException(
            status_code=400,
            detail="invalid_country_code",
        )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                l.locale,
                l.language_code,
                l.country_code,
                l.translator_lang,
                l.display_name,
                l.native_name,
                l.tts_supported,
                l.translate_supported,
                l.is_user_selectable
            FROM public.tts_locales l
            WHERE l.country_code = $1
              AND l.is_enabled = TRUE
              AND l.tts_supported = TRUE
              AND l.is_user_selectable = TRUE
              AND EXISTS (
                  SELECT 1
                  FROM public.tts_voice_locale_capabilities vl
                  JOIN public.tts_voices v
                    ON v.id = vl.voice_id
                  JOIN public.tts_voice_model_capabilities vm
                    ON vm.voice_id = v.id
                   AND vm.provider_code = v.provider
                   AND vm.is_enabled = TRUE
                   AND vm.is_approved = TRUE
                  JOIN public.tts_provider_models m
                    ON m.provider_code = vm.provider_code
                   AND m.model_code = vm.model_code
                   AND m.is_enabled = TRUE
                   AND m.routing_enabled = TRUE
                  JOIN public.tts_providers p
                    ON p.provider_code = vm.provider_code
                   AND p.is_enabled = TRUE
                   AND p.routing_enabled = TRUE
                  WHERE vl.locale = l.locale
                    AND vl.is_enabled = TRUE
                    AND vl.is_approved = TRUE
              )
            ORDER BY l.display_name, l.locale
            """,
            normalized_country,
        )

    return {
        "country_code": normalized_country,
        "items": [dict(r) for r in rows],
    }


@router.get("/voices")
async def list_voices(
    locale: str = Query(..., description="BCP 47 locale code"),
        market: str = Query(
        "global",
        description="Deprecated compatibility parameter; locale availability is DB-driven.",
    ),
    pool: asyncpg.Pool = Depends(get_pool),
) -> Dict[str, Any]:
    # Validate the required locale value; eligibility remains DB-driven.
    if not _is_locale_allowed(locale, market):
        raise HTTPException(
            status_code=400,
            detail=f"locale_not_allowed_for_market: market={_normalize_market(market)} locale={locale}",
        )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
              v.voice_name,
              v.voice_name AS voice_id,
              COALESCE(
                  NULLIF(v.meta_json->>'display_name',''),
                  v.voice_name
              ) AS display_name,
              vl.locale AS locale,
              v.gender,
              v.voice_type,
              v.is_default,
              v.supports_styles,
              v.meta_json
            FROM public.tts_voices v
            JOIN public.tts_voice_locale_capabilities vl
              ON vl.voice_id = v.id
             AND vl.locale = $1
             AND vl.is_enabled = true
             AND vl.is_approved = true
            WHERE EXISTS (
                SELECT 1
                FROM public.tts_voice_model_capabilities vm
                JOIN public.tts_provider_models m
                  ON m.provider_code = vm.provider_code
                 AND m.model_code = vm.model_code
                 AND m.is_enabled = true
                 AND m.routing_enabled = true
                JOIN public.tts_providers p
                  ON p.provider_code = vm.provider_code
                 AND p.is_enabled = true
                 AND p.routing_enabled = true
                WHERE vm.voice_id = v.id
                  AND vm.provider_code = v.provider
                  AND vm.is_enabled = true
                  AND vm.is_approved = true
            )
            ORDER BY
              v.is_default DESC,
              vl.is_recommended DESC,
              COALESCE(vl.quality_score, 0) DESC,
              v.voice_name ASC
            """,
            locale,
        )
    return {"items": [dict(r) for r in rows]}