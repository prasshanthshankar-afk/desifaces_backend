from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from app.config import settings
from app.db import get_db_pool
from app.repos.longform_jobs_repo import LongformJobsRepo
from app.repos.longform_segments_repo import LongformSegmentsRepo
from app.services.longform_orchestrator import stitch_if_ready


logger = logging.getLogger("svc_fusion_extension.stitch_worker")
jobs_repo = LongformJobsRepo()
segs_repo = LongformSegmentsRepo()


async def _claim_stitch_jobs(conn, limit: int) -> List[Dict[str, Any]]:
    """Claim independent parents for concurrent canonical stitching.

    `stitching_running` is a lease state: it prevents another stitch worker from
    claiming the same parent after the row lock is released. A stale claim is
    recoverable after 20 minutes if a process dies mid-stitch.
    """
    rows = await conn.fetch(
        """
        with candidates as (
          select id
          from public.longform_jobs
          where status = 'stitching'
             or (
                  status = 'stitching_running'
                  and updated_at < now() - interval '20 minutes'
                )
          order by created_at asc
          for update skip locked
          limit $1::int
        )
        update public.longform_jobs j
        set status = 'stitching_running',
            updated_at = now()
        from candidates c
        where j.id = c.id
        returning j.*
        """,
        max(1, int(limit)),
    )
    return [dict(row) for row in rows]


async def _process_stitch_job(job: Dict[str, Any], pool) -> None:
    job_id = str(job["id"])
    try:
        async with pool.acquire() as conn:
            # Canonical finalization remains in longform_orchestrator:
            # - validates every segment succeeded
            # - composes the ordered final video
            # - persists final video + thumbnail metadata
            # - commits parent pricing exactly once
            # - emits existing completion/failure notifications
            await stitch_if_ready(jobs_repo, segs_repo, conn, job)
        logger.info("canonical longform stitch completed job_id=%s", job_id)
    except Exception:
        # stitch_if_ready owns product failure/release semantics where applicable.
        # Keep the worker alive so one failed parent cannot stop other parents.
        logger.exception("canonical longform stitch task failed job_id=%s", job_id)


async def stitch_loop() -> None:
    if not settings.STITCH_WORKER_ENABLED:
        logger.info("STITCH_WORKER_ENABLED=false; exiting stitch worker")
        return

    pool = await get_db_pool()
    batch_size = max(1, int(settings.STITCH_WORKER_BATCH_SIZE))
    logger.info("stitch_worker started batch_size=%s", batch_size)

    while True:
        async with pool.acquire() as conn:
            jobs = await _claim_stitch_jobs(conn, batch_size)

        if not jobs:
            await asyncio.sleep(settings.STITCH_WORKER_POLL_SECONDS)
            continue

        # Each parent has independent segment files and a distinct final output.
        # Run those parent finalizations concurrently; ordering within each final
        # video remains deterministic inside stitch_if_ready().
        await asyncio.gather(
            *(_process_stitch_job(job, pool) for job in jobs),
            return_exceptions=False,
        )
        await asyncio.sleep(0.05)


if __name__ == "__main__":
    asyncio.run(stitch_loop())
