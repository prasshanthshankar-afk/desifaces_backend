from __future__ import annotations

from typing import Any, Dict, Optional
from uuid import UUID

from app.db import get_pool


class ProviderRunsService:
    async def create_run(
        self,
        *,
        job_id: UUID,  # MUST be studio_jobs.id (your trigger/envelope ensures this)
        provider: str,
        idempotency_key: str,
        request_json: Dict[str, Any],
        meta_json: Dict[str, Any],
        provider_status: str = "queued",
    ) -> UUID:
        pool = await get_pool()
        row = await pool.fetchrow(
            """
            insert into public.provider_runs(job_id, provider, idempotency_key, provider_status, request_json, meta_json)
            values($1,$2,$3,$4,coalesce($5,'{}'::jsonb),coalesce($6,'{}'::jsonb))
            on conflict (idempotency_key) do update
              set updated_at=now()
            returning id
            """,
            job_id,
            provider,
            idempotency_key,
            provider_status,
            request_json,
            meta_json,
        )
        return UUID(str(row["id"]))

    async def claim_next(
        self,
        *,
        provider: str = "svc-music",
        statuses: tuple[str, ...] = ("queued", "created"),
    ) -> Optional[Dict[str, Any]]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    select *
                    from public.provider_runs
                    where provider=$1 and provider_status = any($2::text[])
                    order by created_at asc
                    for update skip locked
                    limit 1
                    """,
                    provider,
                    list(statuses),
                )
                if not row:
                    return None

                await conn.execute(
                    "update public.provider_runs set provider_status='running', updated_at=now() where id=$1",
                    row["id"],
                )
                return dict(row)

    async def finish(
        self,
        *,
        run_id: UUID,
        provider_status: str,
        response_json: Optional[Dict[str, Any]] = None,
        provider_job_id: Optional[str] = None,
        meta_patch: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        pool = await get_pool()
        sets = ["provider_status=$2", "updated_at=now()"]
        args: list[Any] = [run_id, provider_status]
        i = 3

        if provider_job_id is not None:
            sets.append(f"provider_job_id=${i}")
            args.append(provider_job_id)
            i += 1
        if response_json is not None:
            sets.append(f"response_json=${i}")
            args.append(response_json)
            i += 1
        if error:
            # store error in meta_json to avoid schema changes
            meta_patch = dict(meta_patch or {})
            meta_patch["error"] = error

        if meta_patch:
            sets.append(f"meta_json=coalesce(meta_json,'{{}}'::jsonb) || ${i}::jsonb")
            args.append(meta_patch)
            i += 1

        await pool.execute(
            f"update public.provider_runs set {', '.join(sets)} where id=$1",
            *args,
        )