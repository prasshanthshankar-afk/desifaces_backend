from __future__ import annotations

from typing import Any, Dict
import asyncpg
from fastapi import APIRouter, Depends, Query, HTTPException

from app.db import get_pool

router = APIRouter(prefix="/api/audio/catalog", tags=["audio-catalog"])


@router.get("/locales")
async def list_locales(
    end_to_end_only: bool = Query(True, description="If true, require both TTS + translation support"),
    enabled_only: bool = Query(True, description="If true, only is_enabled locales"),
    pool: asyncpg.Pool = Depends(get_pool),
) -> Dict[str, Any]:
    where = []
    if enabled_only:
        where.append("is_enabled = true")
    if end_to_end_only:
        where.append("tts_supported = true AND translate_supported = true")
    clause = "WHERE " + " AND ".join(where) if where else ""

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
            ORDER BY l.locale
            """
        )

    return {"items": [dict(r) for r in rows]}


@router.get("/voices")
async def list_voices(
    locale: str = Query(..., description="Locale like hi-IN, ta-IN, en-US, zh-CN"),
    pool: asyncpg.Pool = Depends(get_pool),
) -> Dict[str, Any]:
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