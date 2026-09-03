from __future__ import annotations

import asyncio
import logging
import os
from typing import Set

from app.config import settings
from app.db import get_pool
from app.repos.fusion_jobs_repo import FusionJobsRepo
from app.services.fusion_orchestrator import FusionOrchestrator

logger = logging.getLogger("fusion_worker")


def _worker_concurrency() -> int:
    """Bounded core-Fusion concurrency.

    Longform parent segments are already independent child jobs. Core Fusion must
    not serialize their provider submissions one-at-a-time. Keep this bounded so
    provider/account limits and the DB pool remain protected.
    """
    raw = os.getenv("DF_FUSION_WORKER_CONCURRENCY", "4")
    try:
        return max(1, min(8, int(raw)))
    except Exception:
        return 4


async def _run_one(pool, job_id: str) -> None:
    orch = FusionOrchestrator(pool)
    try:
        await orch.run_job(job_id)
    except Exception as exc:
        msg = str(exc)
        logger.exception("job_unhandled_exception", extra={"job_id": job_id, "error": msg})
        try:
            await orch.jobs.set_status(
                job_id,
                "failed",
                error_code="WORKER_CRASH",
                error_message=msg,
            )
        except Exception:
            logger.exception("job_fail_marking_failed", extra={"job_id": job_id})


async def run_forever() -> None:
    pool = await get_pool()
    jobs_repo = FusionJobsRepo(pool)
    concurrency = _worker_concurrency()
    inflight: Set[asyncio.Task] = set()

    logger.info("fusion_worker_started", extra={"concurrency": concurrency})

    while True:
        try:
            # Drop completed tasks and surface unexpected task-level failures.
            finished = {task for task in inflight if task.done()}
            for task in finished:
                inflight.discard(task)
                try:
                    task.result()
                except Exception:
                    logger.exception("fusion_worker_task_exception")

            capacity = max(0, concurrency - len(inflight))
            if capacity > 0:
                job_ids = await jobs_repo.claim_next_jobs(
                    studio_type="fusion",
                    limit=capacity,
                )
                for job_id in job_ids:
                    task = asyncio.create_task(
                        _run_one(pool, job_id),
                        name=f"fusion-job-{job_id}",
                    )
                    inflight.add(task)

            if inflight:
                # Refill immediately when any provider-backed child job finishes.
                await asyncio.wait(
                    inflight,
                    timeout=max(0.05, float(settings.WORKER_IDLE_SLEEP_SECONDS)),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                await asyncio.sleep(settings.WORKER_IDLE_SLEEP_SECONDS)

        except Exception as exc:
            logger.exception("worker_loop_exception", extra={"error": str(exc)})
            await asyncio.sleep(1.0)


if __name__ == "__main__":
    asyncio.run(run_forever())
