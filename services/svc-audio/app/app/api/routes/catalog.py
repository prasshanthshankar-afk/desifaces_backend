from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import asyncpg
from fastapi import APIRouter, Depends, Query, HTTPException

from app.db import get_pool

router = APIRouter(prefix="/api/audio/catalog", tags=["audio-catalog"])


# --- Market rollout policy (small + safe) ------------------------------------
# Default rollout = India only:
#   - all locales ending with "-IN"
#   - plus en-US and en-GB (requested)
#
# Future expansion:
#   - you can add entries here without touching DB schema
#   - e.g. "us": extras=["en-GB"] and suffix="US"
MARKET_ALIASES: Dict[str, str] = {
    "global": "global",
    "world": "global",
    "all": "global",
    "in": "in",
    "india": "in",
    "us": "us",
    "usa": "us",
    "gb": "gb",
    "uk": "gb",
}

MARKET_SUFFIX: Dict[str, Optional[str]] = {
    "global": None,  # no filtering
    "in": "IN",
    "us": "US",
    "gb": "GB",
}

MARKET_EXTRAS: Dict[str, Tuple[str, ...]] = {
    # India rollout extras:
    "in": ("en-US", "en-GB"),
    # add more in future if needed
    "us": ("en-GB",),
    "gb": ("en-US",),
}


def _normalize_market(market: Optional[str]) -> str:
    m = (market or "in").strip().lower()
    return MARKET_ALIASES.get(m, m)


def _is_locale_allowed(locale: str, market: str) -> bool:
    loc = (locale or "").strip()
    if not loc:
        return False

    m = _normalize_market(market)

    # global => allow all
    if m == "global":
        return True

    extras = set(MARKET_EXTRAS.get(m, ()))
    if loc in extras:
        return True

    suffix = MARKET_SUFFIX.get(m)
    if suffix:
        return loc.upper().endswith(f"-{suffix.upper()}")

    # if unknown market and no suffix policy, fail closed (safe)
    return False


def _market_sql_filter(market: str) -> Tuple[str, List[Any]]:
    """
    Returns:
      (sql_fragment, args)
    Safe: uses parameter binding (no string injection).
    """
    m = _normalize_market(market)

    if m == "global":
        return "", []

    extras = list(MARKET_EXTRAS.get(m, ()))
    suffix = MARKET_SUFFIX.get(m)

    args: List[Any] = []
    parts: List[str] = []

    if suffix:
        # suffix match: % -IN, % -US etc.
        args.append(f"%-{suffix.upper()}")
        parts.append(f"l.locale ILIKE ${len(args)}")

    if extras:
        args.append(extras)
        parts.append(f"l.locale = ANY(${len(args)})")

    if not parts:
        # unknown market => return a filter that matches nothing (safe)
        return "AND 1=0", []

    return "AND (" + " OR ".join(parts) + ")", args


def _order_by_sql(market: str) -> str:
    """
    Premium ordering for India rollout (as you already defined in SQL).
    For other markets/global, keep stable alphabetical ordering.
    """
    m = _normalize_market(market)
    if m != "in":
        return "ORDER BY l.locale"

    return """
    ORDER BY
      CASE l.locale
        WHEN 'en-IN' THEN 0
        WHEN 'hi-IN' THEN 1
        WHEN 'ta-IN' THEN 2
        WHEN 'te-IN' THEN 3
        WHEN 'kn-IN' THEN 4
        WHEN 'ml-IN' THEN 5
        WHEN 'as-IN' THEN 6
        WHEN 'bn-IN' THEN 7
        WHEN 'gu-IN' THEN 8
        WHEN 'mr-IN' THEN 9
        WHEN 'or-IN' THEN 10
        WHEN 'pa-IN' THEN 11
        WHEN 'ur-IN' THEN 12
        WHEN 'en-US' THEN 90
        WHEN 'en-GB' THEN 91
        ELSE 50
      END,
      l.locale
    """


# --- Routes ------------------------------------------------------------------


@router.get("/locales")
async def list_locales(
    # NEW: market filter (default India rollout)
    market: str = Query(
        "in",
        description="Rollout market. Default 'in' returns only India locales + en-US/en-GB. Use 'global' for all enabled locales.",
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
                WHERE v.locale = l.locale AND v.provider='azure' AND v.is_default=true
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


@router.get("/voices")
async def list_voices(
    locale: str = Query(..., description="Locale like hi-IN, ta-IN, en-US, zh-CN"),
    # NEW: market (default India rollout) so UI can't request voices outside rollout
    market: str = Query(
        "in",
        description="Rollout market. Default 'in' only allows *-IN + en-US/en-GB. Use 'global' to allow all enabled locales.",
    ),
    pool: asyncpg.Pool = Depends(get_pool),
) -> Dict[str, Any]:
    # Safety guard: do not expose voices outside the rollout market
    if not _is_locale_allowed(locale, market):
        raise HTTPException(
            status_code=400,
            detail=f"locale_not_allowed_for_market: market={_normalize_market(market)} locale={locale}",
        )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
              voice_name,
              locale,
              gender,
              voice_type,
              is_default,
              supports_styles,
              meta_json
            FROM public.tts_voices
            WHERE provider='azure' AND locale=$1
            ORDER BY is_default DESC, voice_name ASC
            """,
            locale,
        )
    return {"items": [dict(r) for r in rows]}