from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional
from uuid import UUID

from app.db import get_pool

_SCHEMA = "public"
_TABLE = f"{_SCHEMA}.commerce_quotes"


class CommerceQuotesRepo:
    async def upsert_quoted(
        self,
        *,
        quote_id: UUID,
        user_id: UUID,
        request_json: Dict[str, Any],
        response_json: Dict[str, Any],
        total_credits: int,
        total_usd: float,
        total_inr: float,
        expires_at: datetime,
        scope: str = "commerce",
    ) -> None:
        pool = await get_pool()

        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        usd = Decimal(str(total_usd)).quantize(Decimal("0.01"))
        inr = Decimal(str(total_inr)).quantize(Decimal("0.01"))

        await pool.execute(
            f"""
            INSERT INTO {_TABLE}(
              id, user_id, scope, request_json, response_json,
              total_credits, total_usd, total_inr, status, expires_at
            )
            VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6,$7,$8,'quoted',$9)
            ON CONFLICT (id) DO UPDATE SET
              request_json  = EXCLUDED.request_json,
              response_json = EXCLUDED.response_json,
              total_credits = EXCLUDED.total_credits,
              total_usd     = EXCLUDED.total_usd,
              total_inr     = EXCLUDED.total_inr,
              status        = 'quoted',
              expires_at    = EXCLUDED.expires_at,
              updated_at    = NOW()
            """,
            quote_id,
            user_id,
            scope,
            request_json,
            response_json,
            int(total_credits),
            usd,
            inr,
            expires_at,
        )

    async def get_active_for_user(self, *, quote_id: UUID, user_id: UUID) -> Optional[Dict[str, Any]]:
        pool = await get_pool()
        row = await pool.fetchrow(
            f"""
            SELECT id, user_id, status, expires_at, request_json, response_json,
                   total_credits, total_usd, total_inr
            FROM {_TABLE}
            WHERE id=$1 AND user_id=$2
              AND expires_at > NOW()
            """,
            quote_id,
            user_id,
        )
        return dict(row) if row else None

    async def mark_confirmed(self, *, quote_id: UUID, user_id: UUID) -> None:
        pool = await get_pool()
        await pool.execute(
            f"""
            UPDATE {_TABLE}
            SET status='confirmed', updated_at=NOW()
            WHERE id=$1 AND user_id=$2
            """,
            quote_id,
            user_id,
        )