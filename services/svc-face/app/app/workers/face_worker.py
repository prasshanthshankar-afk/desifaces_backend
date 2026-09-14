from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Optional, Set

from ..repos.face_jobs_repo import FaceJobsRepo
from ..services.creator_orchestrator import CreatorOrchestrator
from app.db import get_pool, close_pool

logger = logging.getLogger("face_worker")

MAX_TRIES_DEFAULT = 3
IDLE_SLEEP_SECONDS = 3


def _worker_concurrency() -> int:
    """Bound independent Face jobs while preserving per-job variant concurrency.

    A Face job already fans its variants out through DF_FACE_VARIANT_CONCURRENCY
    (default 3). Two jobs in parallel therefore permit two-person story casts to
    generate concurrently without opening unbounded provider fan-out.
    """
    raw = os.getenv("DF_FACE_JOB_CONCURRENCY", "2")
    try:
        return max(1, min(4, int(raw)))
    except Exception:
        return 2


class WorkerProcess:
    def __init__(self, worker_id: str = "face-worker-1"):
        self.worker_id = worker_id
        self.running = True
        self.repo: Optional[FaceJobsRepo] = None
        self.pool = None

    async def _get_max_tries(self, job) -> int:
        try:
            mj = getattr(job, "meta_json", None) or {}
            return int(mj.get("max_tries", MAX_TRIES_DEFAULT))
        except Exception:
            return MAX_TRIES_DEFAULT

    async def _count_outputs(self, job_id: str) -> int:
        q = "SELECT COUNT(*) FROM face_job_outputs WHERE job_id = $1::uuid"
        try:
            n = await self.repo.fetch_scalar(q, job_id)
            return int(n or 0)
        except Exception:
            return 0

    async def _count_artifacts(self, job_id: str) -> int:
        q = "SELECT COUNT(*) FROM artifacts WHERE job_id = $1::uuid AND kind = 'face_image'"
        try:
            n = await self.repo.fetch_scalar(q, job_id)
            return int(n or 0)
        except Exception:
            return 0

    def _norm_status(self, value: Optional[str]) -> str:
        return (value or "").strip().lower()

    async def _reschedule_or_fail(self, job_id: str, *, error_code: str, error_message: str) -> None:
        job = await self.repo.get_job(job_id)
        attempt = int(getattr(job, "attempt_count", 1) or 1)
        max_tries = await self._get_max_tries(job)
        if attempt < max_tries:
            delay = min(60, 5 * (2 ** (attempt - 1)))
            await self.repo.reschedule_job(
                job_id=job_id,
                delay_seconds=delay,
                error_code=error_code,
                error_message=error_message,
            )
            logger.info(
                "Job rescheduled",
                extra={"job_id": job_id, "delay": delay, "attempt": attempt, "error_code": error_code},
            )
        else:
            await self.repo.update_status(
                job_id,
                "failed",
                error_code=error_code,
                error_message=error_message,
                meta_patch={"worker_id": self.worker_id},
            )

    async def _process_job(self, job_id: str) -> None:
        # Do not share orchestration state between concurrent jobs. Repositories
        # and orchestrators share only the asyncpg pool, whose claims are guarded
        # by FOR UPDATE SKIP LOCKED.
        orchestrator = CreatorOrchestrator(self.pool)
        try:
            job = await self.repo.get_job(job_id)
            attempt = int(getattr(job, "attempt_count", 1) or 1)
            max_tries = await self._get_max_tries(job)
            logger.info(
                "Processing face job",
                extra={
                    "job_id": job_id,
                    "worker_id": self.worker_id,
                    "attempt": attempt,
                    "max_tries": max_tries,
                },
            )

            await orchestrator.process_job(job_id)

            job_after = await self.repo.get_job(job_id)
            status_after = self._norm_status(getattr(job_after, "status", None))
            err_after = getattr(job_after, "error_message", None)

            if status_after in ("running", "queued"):
                msg = f"Orchestrator returned but job still {status_after}"
                logger.error(msg, extra={"job_id": job_id, "status": status_after})
                await self._reschedule_or_fail(
                    job_id,
                    error_code="PROCESSING_INCOMPLETE",
                    error_message=msg,
                )
                return

            if status_after in ("succeeded", "success"):
                outputs = await self._count_outputs(job_id)
                artifacts = await self._count_artifacts(job_id)
                if outputs == 0 and artifacts == 0:
                    msg = "Job marked succeeded but produced zero outputs/artifacts"
                    logger.error(msg, extra={"job_id": job_id})
                    await self._reschedule_or_fail(
                        job_id,
                        error_code="NO_OUTPUTS",
                        error_message=msg,
                    )
                    return
                logger.info(
                    "Job succeeded",
                    extra={"job_id": job_id, "outputs": outputs, "artifacts": artifacts},
                )
            else:
                logger.info(
                    "Job finished",
                    extra={"job_id": job_id, "status": status_after, "error": err_after},
                )

        except asyncio.CancelledError:
            logger.warning("Face job task cancelled", extra={"job_id": job_id})
            raise
        except Exception as exc:
            logger.exception(
                "Job failed (worker exception)",
                extra={"job_id": job_id, "worker_id": self.worker_id, "error": str(exc)},
            )
            try:
                await self._reschedule_or_fail(
                    job_id,
                    error_code="worker_error",
                    error_message=str(exc),
                )
            except Exception:
                logger.exception("Failed to reschedule/mark failed", extra={"job_id": job_id})

    async def main(self):
        self.pool = await get_pool()
        self.repo = FaceJobsRepo(self.pool)
        concurrency = _worker_concurrency()
        inflight: Set[asyncio.Task] = set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop_worker)

        logger.info(
            "Face worker started",
            extra={"worker_id": self.worker_id, "job_concurrency": concurrency},
        )

        try:
            while self.running:
                finished = {task for task in inflight if task.done()}
                for task in finished:
                    inflight.discard(task)
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        logger.exception("face_worker_task_exception")

                capacity = max(0, concurrency - len(inflight))
                if capacity > 0:
                    job_ids = await self.repo.claim_next_jobs(
                        studio_type="face",
                        limit=capacity,
                    )
                    for job_id in job_ids:
                        task = asyncio.create_task(
                            self._process_job(job_id),
                            name=f"face-job-{job_id}",
                        )
                        inflight.add(task)

                if inflight:
                    await asyncio.wait(
                        inflight,
                        timeout=float(IDLE_SLEEP_SECONDS),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    await asyncio.sleep(IDLE_SLEEP_SECONDS)
        finally:
            # Controlled production deploys gate on zero running Face jobs, so
            # this should normally be empty. If shutdown arrives during work,
            # allow claimed jobs to finish rather than abandoning running rows.
            if inflight:
                await asyncio.gather(*inflight, return_exceptions=True)
            await close_pool()
            logger.info("Face worker stopped", extra={"worker_id": self.worker_id})

    def stop_worker(self):
        self.running = False
        logger.info("Face worker stopping", extra={"worker_id": self.worker_id})


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    asyncio.run(WorkerProcess().main())
