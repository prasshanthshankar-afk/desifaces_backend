from __future__ import annotations

import json
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

import asyncpg


class OpsCostLedgerRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def add_line_item(
        self,
        run_id: UUID,
        run_as_user_id: UUID,
        cost_bucket: str,
        cost_category: str,
        cost_owner: str,
        studio_type: str,
        provider: str,
        units: Optional[float],
        unit: Optional[str],
        cost_usd: float,
        credits: float,
        job_id: Optional[UUID] = None,
        artifact_id: Optional[UUID] = None,
        metadata_json: Optional[Dict[str, Any]] = None,
    ) -> UUID:
        ledger_id = uuid4()
        q = """
        insert into ops_cost_ledger (
          id, run_id, run_as_user_id,
          cost_bucket, cost_category, cost_owner,
          studio_type, provider,
          units, unit,
          cost_usd, credits,
          job_id, artifact_id,
          metadata_json
        ) values (
          $1, $2, $3,
          $4, $5, $6,
          $7, $8,
          $9, $10,
          $11, $12,
          $13, $14,
          $15::jsonb
        )
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                q,
                ledger_id,
                run_id,
                run_as_user_id,
                cost_bucket,
                cost_category,
                cost_owner,
                studio_type,
                provider,
                units,
                unit,
                cost_usd,
                credits,
                job_id,
                artifact_id,
                json.dumps(metadata_json or {}),
            )
        return ledger_id