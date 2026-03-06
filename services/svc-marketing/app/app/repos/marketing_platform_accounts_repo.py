# services/svc-marketing/app/app/repos/marketing_platform_accounts_repo.py
from __future__ import annotations

import json
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg


class MarketingPlatformAccountsRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_account(self, platform_account_id: UUID) -> Optional[asyncpg.Record]:
        q = "select * from marketing_platform_accounts where platform_account_id=$1 and enabled=true"
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(q, platform_account_id)

    async def get_default_account(self, platform: str) -> Optional[asyncpg.Record]:
        q = """
        select *
        from marketing_platform_accounts
        where platform=$1 and enabled=true
        order by updated_at desc
        limit 1
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(q, platform)

    async def update_access_token_cache(self, platform_account_id: UUID, cache_json: Dict[str, Any]) -> None:
        q = """
        update marketing_platform_accounts
        set access_token_cache_json=$2::jsonb, updated_at=now()
        where platform_account_id=$1
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, platform_account_id, json.dumps(cache_json))