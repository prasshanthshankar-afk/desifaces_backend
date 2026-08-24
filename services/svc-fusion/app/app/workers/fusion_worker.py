from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from app.config import settings
from app.db import get_pool
from app.repos.fusion_jobs_repo import FusionJobsRepo
from app.services.fusion_orchestrator import FusionOrchestrator

logger = logging.getLogger("fusion_worker")


def _worker_concurrency() -> int:
    """Bounded parallelism for independent Fusion jobs.

    Multi-person V3 scenes fan out independent dialogue-turn renders. A worker must
    therefore claim and execute a batch concurrently instead of waiting for each
    provider render to finish before starting the next one. The bound remains
    configurable so provider/account rate limits can be respected without reverting
    to serial execution.
    """
    raw = str(os.getenv("DF_FUSION_WORKER_CONCURRENCY", "8") or "8").strip()
    try:
        return max(1, min(32, int(raw)))
    except Exception:
        return 8


async def run_forever() -> None:
    pool = await get_pool()
    jobs_repo = FusionJobsRepo(pool)
    concurrency = _worker_concurrency()

    logger.info("Fusion worker started concurrency=%s", concurrency)

    async def run_one(job_id: str) -> None:
        orch = FusionOrchestrator(pool)
        try:
            await orch.run_job(job_id)
        except Exception as e:
            msg = str(e)
            logger.exception("job_unhandled_exception", extra={"job_id": job_id, "error": msg})
            # HARD safety: ensure job isn't stuck in running.
            try:
                await orch.jobs.set_status(
                    job_id,
                    "failed",
                    error_code="WORKER_CRASH",
                    error_message=msg,
                )
            except Exception:
                logger.exception("job_fail_marking_failed", extra={"job_id": job_id})

    while True:
        current_job_id: Optional[str] = None
        try:
            job_ids = await jobs_repo.claim_next_jobs(
                studio_type="fusion",
                limit=concurrency,
            )
            if not job_ids:
                await asyncio.sleep(settings.WORKER_IDLE_SLEEP_SECONDS)
                continue

            logger.info(
                "Fusion worker claimed batch size=%s concurrency=%s",
                len(job_ids),
                concurrency,
            )

            # All claimed independent jobs begin together. asyncio.gather keeps one
            # child failure from serializing or delaying submission/execution of the
            # remaining children; run_one owns each child's terminal safety handling.
            await asyncio.gather(*(run_one(job_id) for job_id in job_ids))

        except Exception as e:
            msg = str(e)
            logger.exception(
                "worker_loop_exception",
                extra={"job_id": current_job_id, "error": msg},
            )
            if current_job_id:
                try:
                    orch = FusionOrchestrator(pool)
                    await orch.jobs.set_status(
                        current_job_id,
                        "failed",
                        error_code="WORKER_CRASH",
                        error_message=msg,
                    )
                except Exception:
                    logger.exception(
                        "job_fail_marking_failed",
                        extra={"job_id": current_job_id},
                    )
            await asyncio.sleep(1.0)


if __name__ == "__main__":
    asyncio.run(run_forever())
