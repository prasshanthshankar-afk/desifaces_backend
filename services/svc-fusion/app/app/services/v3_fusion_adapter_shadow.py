"""Read-only V3 canonical mapping for the current Fusion create API.

This module never creates jobs, reserves credits, writes media, or calls a
provider. It resolves canonical account identity and optionally verifies
explicit V3 canonical source media IDs, then runs the pure Fusion adapter.

Current legacy ``artifacts`` UUIDs are not treated as canonical ``MediaAsset``
identity; they remain compatibility metadata until V3-C4 freezes physical media
lineage/migration.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping
from uuid import UUID

from desifaces_shared.identity import AccountContextNotFound, resolve_account_context
from df_contracts.v3.fusion_adapter import (
    FusionGenerateAdapterResult,
    adapt_fusion_generate_request,
)

logger = logging.getLogger(__name__)


def fusion_v3_shadow_enabled() -> bool:
    return str(os.getenv("DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def _resolve_owned_explicit_media_ids(
    conn: Any,
    *,
    user_id: UUID,
    payload: Mapping[str, Any],
) -> tuple[UUID, ...]:
    """Resolve only explicit V3 canonical media IDs supplied to the probe.

    Legacy Face/Audio artifact IDs are intentionally ignored here because their
    UUID namespace belongs to ``public.artifacts``, not canonical MediaAsset.
    """

    raw_values = payload.get("source_media_ids")
    if not isinstance(raw_values, (list, tuple)):
        return ()

    candidates: list[UUID] = []
    for raw in raw_values:
        try:
            candidates.append(UUID(str(raw)))
        except Exception:
            continue
    if not candidates:
        return ()

    rows = await conn.fetch(
        """
        SELECT id
        FROM public.media_assets
        WHERE user_id = $1::uuid
          AND id = ANY($2::uuid[])
        """,
        user_id,
        candidates,
    )
    found = {UUID(str(row["id"])) for row in rows}
    return tuple(candidate for candidate in candidates if candidate in found)


async def build_fusion_v3_shadow_mapping(
    pool: Any,
    *,
    user_id: str,
    payload: Mapping[str, Any],
) -> FusionGenerateAdapterResult | None:
    if not fusion_v3_shadow_enabled():
        return None

    try:
        canonical_user_id = UUID(str(user_id))
    except Exception:
        logger.info("v3_fusion_shadow_skip reason=non_user_actor actor=%s", str(user_id))
        return None

    async with pool.acquire() as conn:
        account = await resolve_account_context(conn, canonical_user_id)
        source_media_ids = await _resolve_owned_explicit_media_ids(
            conn,
            user_id=canonical_user_id,
            payload=payload,
        )

    result = adapt_fusion_generate_request(
        payload,
        account_id=account.account_id,
        user_id=canonical_user_id,
        resolved_source_media_ids=source_media_ids,
        client_app="svc-fusion-compat",
    )

    provider_hints = result.compatibility_metadata.get("provider_hints") or {}
    logger.info(
        "v3_fusion_shadow_ok user_id=%s account_id=%s generation_id=%s source_media_count=%s provider_hint=%s canonical_quote=%s legacy_quote=%s",
        canonical_user_id,
        account.account_id,
        result.generation_request.generation_id,
        len(result.generation_request.source_media_ids),
        provider_hints.get("provider") or provider_hints.get("provider_hint"),
        result.generation_request.pricing_quote_id,
        result.pricing_confirmation.quote_id if result.pricing_confirmation else None,
    )
    return result


__all__ = [
    "AccountContextNotFound",
    "build_fusion_v3_shadow_mapping",
    "fusion_v3_shadow_enabled",
]
