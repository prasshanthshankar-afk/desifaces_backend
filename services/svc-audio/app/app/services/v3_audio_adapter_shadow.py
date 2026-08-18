"""Read-only V3 canonical mapping for the current Audio TTS create API.

This module never creates jobs, reserves credits, writes media, translates text,
synthesizes speech, or calls a provider. It resolves canonical account identity
and runs the shared pure Audio adapter so current requests can be certified
against V3 contracts before execution cutover.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping
from uuid import UUID

from desifaces_shared.identity import AccountContextNotFound, resolve_account_context
from df_contracts.v3.audio_adapter import (
    AudioGenerateAdapterResult,
    adapt_audio_tts_request,
)

logger = logging.getLogger(__name__)


def audio_v3_shadow_enabled() -> bool:
    return str(os.getenv("DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def build_audio_v3_shadow_mapping(
    pool: Any,
    *,
    user_id: str,
    payload: Mapping[str, Any],
) -> AudioGenerateAdapterResult | None:
    """Build canonical Audio contracts without affecting current execution."""

    if not audio_v3_shadow_enabled():
        return None

    canonical_user_id = UUID(str(user_id))

    async with pool.acquire() as conn:
        account = await resolve_account_context(conn, canonical_user_id)

    result = adapt_audio_tts_request(
        payload,
        account_id=account.account_id,
        user_id=canonical_user_id,
        client_app="svc-audio-compat",
    )

    logger.info(
        "v3_audio_shadow_ok user_id=%s account_id=%s generation_id=%s canonical_quote=%s legacy_quote=%s voice_id=%s target_locale=%s",
        canonical_user_id,
        account.account_id,
        result.generation_request.generation_id,
        result.generation_request.pricing_quote_id,
        result.pricing_confirmation.quote_id if result.pricing_confirmation else None,
        result.parameters.voice_id,
        result.parameters.target_locale,
    )
    return result


__all__ = [
    "AccountContextNotFound",
    "audio_v3_shadow_enabled",
    "build_audio_v3_shadow_mapping",
]
