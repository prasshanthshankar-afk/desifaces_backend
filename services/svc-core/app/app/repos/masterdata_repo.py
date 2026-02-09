from __future__ import annotations

from typing import Any, Dict, List, Tuple
import asyncpg

def _iso_z(dt) -> str:
    return dt.isoformat().replace("+00:00", "Z")

class MasterdataRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_revision(self, domain: str) -> Tuple[int, str]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT revision, updated_at FROM public.masterdata_revision WHERE domain=$1",
                domain,
            )
            if not row:
                raise KeyError(domain)
            return int(row["revision"]), _iso_z(row["updated_at"])

    async def get_face_masterdata(self, lang: str) -> Dict[str, List[Dict[str, Any]]]:
        async with self.pool.acquire() as conn:
            regions = await conn.fetch(
                """
                SELECT
                  code,
                  COALESCE(
                    display_name ->> $1,
                    display_name ->> 'en',
                    (SELECT value FROM jsonb_each_text(display_name) LIMIT 1),
                    code
                  ) AS label,
                  sub_region,
                  is_active,
                  sort_order
                FROM public.face_generation_regions
                WHERE is_active = true
                ORDER BY sort_order ASC, label ASC;
                """,
                lang,
            )

            contexts = await conn.fetch(
                """
                SELECT
                  code,
                  COALESCE(
                    display_name ->> $1,
                    display_name ->> 'en',
                    (SELECT value FROM jsonb_each_text(display_name) LIMIT 1),
                    code
                  ) AS label,
                  glamour_level,
                  is_active
                FROM public.face_generation_contexts
                WHERE is_active = true
                ORDER BY COALESCE(glamour_level, 0) DESC, label ASC;
                """,
                lang,
            )

            use_cases = await conn.fetch(
                """
                SELECT
                  code,
                  COALESCE(
                    display_name ->> $1,
                    display_name ->> 'en',
                    (SELECT value FROM jsonb_each_text(display_name) LIMIT 1),
                    code
                  ) AS label,
                  category,
                  is_active,
                  sort_order
                FROM public.face_generation_use_cases
                WHERE is_active = true
                ORDER BY sort_order ASC, category ASC, label ASC;
                """,
                lang,
            )

        return {
            "regions": [dict(r) for r in regions],
            "contexts": [dict(r) for r in contexts],
            "use_cases": [dict(r) for r in use_cases],
        }

    async def get_tts_masterdata(self) -> Dict[str, List[Dict[str, Any]]]:
        async with self.pool.acquire() as conn:
            locales = await conn.fetch(
                """
                SELECT
                  locale,
                  display_name,
                  native_name,
                  tts_supported,
                  translate_supported,
                  is_enabled
                FROM public.tts_locales
                WHERE is_enabled = true
                ORDER BY tts_supported DESC, translate_supported DESC, locale ASC;
                """
            )

            voices = await conn.fetch(
                """
                SELECT
                  provider,
                  voice_name,
                  locale,
                  gender,
                  voice_type,
                  is_default,
                  supports_styles
                FROM public.tts_voices
                WHERE locale IN (SELECT locale FROM public.tts_locales WHERE is_enabled = true)
                ORDER BY locale ASC, is_default DESC, provider ASC, voice_name ASC;
                """
            )

        return {
            "locales": [dict(r) for r in locales],
            "voices": [dict(r) for r in voices],
        }