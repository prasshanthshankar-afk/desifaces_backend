"""Read-only repository for explicitly approved translation directions.

This is NOT a TTS model/voice routing repository. An empty result prevents
provider-first activation; it does not silently select GPT or alter billing.
"""
from __future__ import annotations
from typing import Any

QUERY = """
SELECT provider_code, model_code, source_locale, target_locale,
       provider_source_code, provider_target_code, priority
FROM public.audio_translation_capabilities
WHERE source_locale=$1 AND target_locale=$2
  AND is_enabled=true AND is_approved=true
ORDER BY priority ASC, provider_code ASC, model_code ASC
"""


async def list_approved_translation_routes(pool: Any, *, source_locale: str, target_locale: str) -> list[dict]:
    if not source_locale or not target_locale or source_locale == target_locale:
        return []
    rows = await pool.fetch(QUERY, source_locale, target_locale)
    return [dict(row) for row in rows]
