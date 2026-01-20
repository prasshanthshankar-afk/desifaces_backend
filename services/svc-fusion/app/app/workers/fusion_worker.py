from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.config import settings
from app.db import get_pool
from app.repos.fusion_jobs_repo import FusionJobsRepo
from app.services.fusion_orchestrator import FusionOrchestrator

logger = logging.getLogger("fusion_worker")


async def run_forever() -> None:
    pool = await get_pool()
    jobs_repo = FusionJobsRepo(pool)
    orch = FusionOrchestrator(pool)

    while True:
        current_job_id: Optional[str] = None
        try:
            job_ids = await jobs_repo.claim_next_jobs(studio_type="fusion", limit=1)
            if not job_ids:
                await asyncio.sleep(settings.WORKER_IDLE_SLEEP_SECONDS)
                continue

            for job_id in job_ids:
                current_job_id = job_id
                try:
                    await orch.run_job(job_id)
                except Exception as e:
                    msg = str(e)
                    logger.exception("job_unhandled_exception", extra={"job_id": job_id, "error": msg})
                    # HARD safety: ensure job isn't stuck in running
                    try:
                        await orch.jobs.set_status(job_id, "failed", error_code="WORKER_CRASH", error_message=msg)
                    except Exception:
                        logger.exception("job_fail_marking_failed", extra={"job_id": job_id})
                finally:
                    current_job_id = None

        except Exception as e:
            msg = str(e)
            logger.exception("worker_loop_exception", extra={"job_id": current_job_id, "error": msg})
            # Only mark failed if we actually have a job id in flight
            if current_job_id:
                try:
                    await orch.jobs.set_status(current_job_id, "failed", error_code="WORKER_CRASH", error_message=msg)
                except Exception:
                    logger.exception("job_fail_marking_failed", extra={"job_id": current_job_id})
            await asyncio.sleep(1.0)  # small backoff to avoid tight crash loops


if __name__ == "__main__":
    asyncio.run(run_forever())