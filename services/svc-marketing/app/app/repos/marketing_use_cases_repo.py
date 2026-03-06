# services/svc-marketing/app/app/repos/marketing_use_cases_repo.py
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg


class MarketingUseCasesRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_use_case(self, use_case_id: UUID) -> Optional[asyncpg.Record]:
        q = "select * from marketing_use_cases where use_case_id=$1"
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(q, use_case_id)

    async def list_candidates(
        self,
        persona: Optional[str],
        industry: Optional[str],
        tags: List[str],
        season_event: Optional[str],
        recipe: Optional[str],
        limit: int = 20,
        approved_only: bool = True,
    ) -> List[asyncpg.Record]:
        q = """
        select *
        from marketing_use_cases
        where enabled=true
          and ($1::bool = false or approved=true)
          and ($2::text is null or persona=$2)
          and ($3::text is null or industry=$3)
          and ($4::text is null or recipe=$4)
          and (cardinality($5::text[]) = 0 or tags && $5::text[])
          and ($6::text is null or season_event=$6 or season_event is null)
        order by weight desc, updated_at desc
        limit $7
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(q, approved_only, persona, industry, recipe, tags, season_event, limit)

    async def bump_usage(self, use_case_id: UUID) -> None:
        q = """
        update marketing_use_cases
        set usage_count = usage_count + 1,
            last_used_at = now(),
            updated_at = now()
        where use_case_id = $1
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, use_case_id)

    async def approve(self, use_case_id: UUID, approved: bool, updated_by: Optional[UUID]) -> None:
        q = """
        update marketing_use_cases
        set approved=$2,
            updated_by=$3,
            updated_at=now()
        where use_case_id=$1
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, use_case_id, approved, updated_by)

    async def update_weight_and_metrics(self, use_case_id: UUID, weight: float, last_metrics_json: Dict[str, Any]) -> None:
        q = """
        update marketing_use_cases
        set weight=$2,
            last_metrics_json=$3::jsonb,
            updated_at=now()
        where use_case_id=$1
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, use_case_id, weight, json.dumps(last_metrics_json))

    async def insert_suggestion(self, use_case_id: UUID, created_by: Optional[UUID], payload: Dict[str, Any]) -> None:
        q = """
        insert into marketing_use_cases (
          use_case_id, enabled, approved, source, version, parent_use_case_id, created_by, updated_by,
          persona, industry, recipe, campaign_type, season_event, tags,
          product_anchor, default_offer, default_seconds,
          default_hook, base_overlay_lines, base_script, default_music_prompt,
          required_assets_json, notes, weight
        ) values (
          $1, true, false, 'llm_curated', 1, $2, $3, $3,
          $4, $5, $6, $7, $8, $9::text[],
          $10, $11, $12,
          $13, $14::jsonb, $15, $16,
          $17::jsonb, $18, 1.0
        )
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                q,
                use_case_id,
                payload.get("parent_use_case_id"),
                created_by,
                payload["persona"],
                payload["industry"],
                payload["recipe"],
                payload.get("campaign_type", "evergreen"),
                payload.get("season_event"),
                payload.get("tags", []),
                payload.get("product_anchor"),
                payload.get("default_offer"),
                int(payload.get("default_seconds") or 10),
                payload.get("default_hook"),
                json.dumps(payload.get("base_overlay_lines") or []),
                payload.get("base_script"),
                payload.get("default_music_prompt"),
                json.dumps(payload.get("required_assets_json") or {}),
                payload.get("notes"),
            )

    async def list_use_cases(
        self,
        approved: Optional[bool],
        source: Optional[str],
        q: Optional[str],
        limit: int = 100,
    ) -> List[asyncpg.Record]:
        # Basic search across industry/product_anchor/default_hook/tags
        sql = """
        select *
        from marketing_use_cases
        where enabled=true
          and ($1::bool is null or approved=$1)
          and ($2::text is null or source=$2)
          and (
            $3::text is null
            or industry ilike ('%'||$3||'%')
            or coalesce(product_anchor,'') ilike ('%'||$3||'%')
            or coalesce(default_hook,'') ilike ('%'||$3||'%')
            or array_to_string(tags, ',') ilike ('%'||$3||'%')
          )
        order by approved desc, weight desc, updated_at desc
        limit $4
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(sql, approved, source, q, limit)