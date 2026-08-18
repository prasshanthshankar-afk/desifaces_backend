"""Read-only V3 canonical mapping for current pricing preview responses."""

from __future__ import annotations

import os
from typing import Any, Mapping
from uuid import UUID

from desifaces_shared.identity import AccountContextNotFound, resolve_account_context
from df_contracts.v3.pricing_adapter import (
    PricingQuoteBridgeResult,
    adapt_pricing_preview_response,
)


def pricing_v3_shadow_enabled() -> bool:
    return str(os.getenv("DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def build_pricing_v3_shadow_mapping(
    pool: Any,
    *,
    user_id: UUID,
    payload: Mapping[str, Any],
) -> PricingQuoteBridgeResult | None:
    """Map a current preview response to a canonical PricingQuote without mutation."""

    if not pricing_v3_shadow_enabled():
        return None

    async with pool.acquire() as conn:
        account = await resolve_account_context(conn, user_id)

    return adapt_pricing_preview_response(
        payload,
        account_id=account.account_id,
        user_id=user_id,
    )


__all__ = [
    "AccountContextNotFound",
    "build_pricing_v3_shadow_mapping",
    "pricing_v3_shadow_enabled",
]
