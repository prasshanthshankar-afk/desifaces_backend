from __future__ import annotations

import json
from typing import Any, Dict, Optional
from uuid import UUID

from app.db import get_pool


class ProviderRunsRepo:
    _T = "public.provider_runs"

    async def enqueue(
        self,
        *,
        job_id: UUID,  # studio_jobs.id (same as music_video_jobs.id)
        provider: str,
        idempotency_key: str,
        request_json: Dict[str, Any],
        meta_json: Dict[str, Any],
    ) -> UUID:
        """
        Insert a provider_run. Idempotent via idempotency_key.
        """
        pool = await get_pool()
        row = await pool.fetchrow(
            f"""
            insert into {self._T}(job_id, provider, idempotency_key, provider_status, request_json, response_json, meta_json)
            values($1,$2,$3,'created',$4::jsonb,'{{}}'::jsonb,$5::jsonb)
            on conflict (idempotency_key) do update
              set updated_at=now()
            returning id
            """,
            job_id,
            provider,
            idempotency_key,
            json.dumps(request_json or {}),
            json.dumps(meta_json or {}),
        )
        return UUID(str(row["id"]))

    async def claim_next(self) -> Optional[Dict[str, Any]]:
        """
        Claim one pending run for svc-music (FOR UPDATE SKIP LOCKED).
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    select *
                    from {self._T}
                    where provider_status in ('created','queued')
                      and coalesce(meta_json->>'svc','')='svc-music'
                    order by updated_at asc
                    for update skip locked
                    limit 1
                    """
                )
                if not row:
                    return None
                await conn.execute(
                    f"""
                    update {self._T}
                    set provider_status='running', updated_at=now()
                    where id=$1
                    """,
                    row["id"],
                )
                return dict(row)

    async def set_result(
        self,
        *,
        run_id: UUID,
        provider_status: str,
        response_json: Dict[str, Any],
        meta_patch: Optional[Dict[str, Any]] = None,
        provider_job_id: Optional[str] = None,
    ) -> None:
        pool = await get_pool()
        sets = ["provider_status=$2", "response_json=$3::jsonb", "updated_at=now()"]
        args = [run_id, provider_status, json.dumps(response_json or {})]
        if provider_job_id is not None:
            sets.append("provider_job_id=$4")
            args.append(provider_job_id)
        if meta_patch:
            sets.append(f"meta_json=coalesce(meta_json,'{{}}'::jsonb) || ${len(args)+1}::jsonb")
            args.append(json.dumps(meta_patch))
        await pool.execute(
            f"update {self._T} set {', '.join(sets)} where id=$1",
            *args,
        )