from __future__ import annotations

from typing import Any, Dict, List, Optional

import asyncpg


class LocaleCatalogRepository:
    """
    Data-access boundary for canonical TTS locale resolution.

    This repository contains no country, language, locale, or provider
    decisions. All mappings come from public.tts_locales and
    public.tts_locale_aliases.
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_enabled_locale(
        self,
        *,
        normalized_key: str,
    ) -> Optional[Dict[str, Any]]:
        row = await self.pool.fetchrow(
            """
            SELECT
                locale,
                translator_lang,
                tts_supported,
                translate_supported,
                is_enabled,
                display_name,
                native_name,
                meta_json
            FROM public.tts_locales
            WHERE lower(replace(locale, '_', '-')) = $1
              AND is_enabled = true
            LIMIT 1
            """,
            normalized_key,
        )

        return dict(row) if row else None

    async def get_enabled_alias(
        self,
        *,
        alias_key: str,
    ) -> Optional[Dict[str, Any]]:
        row = await self.pool.fetchrow(
            """
            SELECT
                alias_key,
                locale,
                language_code,
                alias_type,
                priority,
                meta_json
            FROM public.tts_locale_aliases
            WHERE alias_key = $1
              AND is_enabled = true
            ORDER BY priority ASC, alias_key ASC
            LIMIT 1
            """,
            alias_key,
        )

        return dict(row) if row else None

    async def list_enabled_locales_for_language(
        self,
        *,
        language_code: str,
    ) -> List[Dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT
                locale,
                translator_lang,
                tts_supported,
                translate_supported,
                is_enabled,
                display_name,
                native_name,
                meta_json
            FROM public.tts_locales
            WHERE lower(translator_lang) = $1
              AND is_enabled = true
            ORDER BY locale ASC
            """,
            language_code,
        )

        return [dict(row) for row in rows]
