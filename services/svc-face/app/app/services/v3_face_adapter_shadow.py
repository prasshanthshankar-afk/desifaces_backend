"""Read-only V3 canonical mapping for the current Face create API.

This module never creates jobs, reserves credits, writes media, or calls a
provider.  It resolves canonical identity and existing source-media ownership,
then runs the shared V3 Face adapter so compatibility requests can be validated
against the canonical model before execution cutover.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping
from uuid import UUID

from desifaces_shared.identity import AccountContextNotFound, resolve_account_context
from df_contracts.v3.face_adapter import (
    FaceGenerateAdapterResult,
    adapt_face_generate_request,
)

logger = logging.getLogger(__name__)


def face_v3_shadow_enabled() -> bool:
    return str(os.getenv("DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _studio_input(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = payload.get("studio_input")
    return nested if isinstance(nested, Mapping) else payload


async def _resolve_owned_source_media_id(
    conn: Any,
    *,
    user_id: UUID,
    payload: Mapping[str, Any],
) -> UUID | None:
    raw = _studio_input(payload).get("source_image_asset_id")
    if not raw:
        return None

    try:
        candidate = UUID(str(raw))
    except Exception:
        return None

    found = await conn.fetchval(
        """
        SELECT id
        FROM public.media_assets
        WHERE id = $1::uuid
          AND user_id = $2::uuid
        LIMIT 1
        """,
        candidate,
        user_id,
    )
    if not found:
        return None
    return found if isinstance(found, UUID) else UUID(str(found))


async def build_face_v3_shadow_mapping(
    pool: Any,
    *,
    user_id: str,
    payload: Mapping[str, Any],
) -> FaceGenerateAdapterResult | None:
    """Build canonical Face contracts without affecting current execution.

    Returns ``None`` when the shadow feature is disabled or when the caller is a
    service-token sentinel rather than a user UUID.  Missing account identity is
    surfaced to the caller so the route can log the migration gap while allowing
    the existing compatibility path to continue.
    """

    if not face_v3_shadow_enabled():
        return None

    try:
        canonical_user_id = UUID(str(user_id))
    except Exception:
        logger.info(
            "v3_face_shadow_skip reason=non_user_actor actor=%s",
            str(user_id),
        )
        return None

    async with pool.acquire() as conn:
        account = await resolve_account_context(conn, canonical_user_id)
        source_media_id = await _resolve_owned_source_media_id(
            conn,
            user_id=canonical_user_id,
            payload=payload,
        )

    result = adapt_face_generate_request(
        payload,
        account_id=account.account_id,
        user_id=canonical_user_id,
        resolved_source_media_id=source_media_id,
        client_app="svc-face-compat",
    )

    logger.info(
        "v3_face_shadow_ok user_id=%s account_id=%s generation_id=%s source_media_count=%s canonical_quote=%s legacy_quote=%s",
        canonical_user_id,
        account.account_id,
        result.generation_request.generation_id,
        len(result.generation_request.source_media_ids),
        result.generation_request.pricing_quote_id,
        result.pricing_confirmation.quote_id if result.pricing_confirmation else None,
    )
    return result


__all__ = [
    "AccountContextNotFound",
    "build_face_v3_shadow_mapping",
    "face_v3_shadow_enabled",
]
