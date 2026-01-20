from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Optional

from ..repos.face_jobs_repo import FaceJobsRepo
from ..services.creator_orchestrator import CreatorOrchestrator
from app.db import get_pool, close_pool

logger = logging.getLogger("face_worker")

MAX_TRIES_DEFAULT = 3
IDLE_SLEEP_SECONDS = 3


class WorkerProcess:
    def __init__(self, worker_id: str = "face-worker-1"):
        self.worker_id = worker_id
        self.running = True
        self.repo: Optional[FaceJobsRepo] = None
        self.orchestrator: Optional[CreatorOrchestrator] = None

    async def _get_max_tries(self, job) -> int:
        try:
            mj = getattr(job, "meta_json", None) or {}
            return int(mj.get("max_tries", MAX_TRIES_DEFAULT))
        except Exception:
            return MAX_TRIES_DEFAULT

    async def _count_outputs(self, job_id: str) -> int:
        # face_job_outputs is the canonical “did anything get produced” table for Face jobs
        q = "SELECT COUNT(*) FROM face_job_outputs WHERE job_id = $1::uuid"
        try:
            n = await self.repo.fetch_scalar(q, job_id)  # BaseRepository.fetch_scalar
            return int(n or 0)
        except Exception:
            return 0

    async def _count_artifacts(self, job_id: str) -> int:
        # fallback: artifacts can also indicate successful output
        q = "SELECT COUNT(*) FROM artifacts WHERE job_id = $1::uuid AND kind = 'face_image'"
        try:
            n = await self.repo.fetch_scalar(q, job_id)
            return int(n or 0)
        except Exception:
            return 0

    def _norm_status(self, s: Optional[str]) -> str:
        return (s or "").strip().lower()

    async def main(self):
        pool = await get_pool()
        self.repo = FaceJobsRepo(pool)
        self.orchestrator = CreatorOrchestrator(pool)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop_worker)

        logger.info("Face worker started", extra={"worker_id": self.worker_id})

        try:
            while self.running:
                job_ids = await self.repo.claim_next_jobs(studio_type="face", limit=1)
                if not job_ids:
                    await asyncio.sleep(IDLE_SLEEP_SECONDS)
                    continue

                job_id = job_ids[0]

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

                    # Run orchestrator
                    await self.orchestrator.process_job(job_id)

                    # Re-read job after orchestrator returns (source of truth)
                    job_after = await self.repo.get_job(job_id)
                    status_after = self._norm_status(getattr(job_after, "status", None))
                    err_after = getattr(job_after, "error_message", None)

                    # If orchestrator returned but job is still running/queued -> that's a bug.
                    if status_after in ("running", "queued"):
                        msg = f"Orchestrator returned but job still {status_after}"
                        logger.error(msg, extra={"job_id": job_id, "status": status_after})

                        # Treat as a worker error and reschedule/fail
                        if attempt < max_tries:
                            delay = min(60, 5 * (2 ** (attempt - 1)))
                            await self.repo.reschedule_job(
                                job_id=job_id,
                                delay_seconds=delay,
                                error_code="PROCESSING_INCOMPLETE",
                                error_message=msg,
                            )
                            logger.info("Job rescheduled", extra={"job_id": job_id, "delay": delay, "attempt": attempt})
                        else:
                            await self.repo.update_status(
                                job_id,
                                "failed",
                                error_code="PROCESSING_INCOMPLETE",
                                error_message=msg,
                                meta_patch={"worker_id": self.worker_id},
                            )

                        # Don’t print “finished” as success
                        await asyncio.sleep(0)
                        continue

                    # If succeeded, sanity-check we actually produced outputs
                    if status_after in ("succeeded", "success"):
                        outputs = await self._count_outputs(job_id)
                        artifacts = await self._count_artifacts(job_id)

                        if outputs == 0 and artifacts == 0:
                            msg = "Job marked succeeded but produced zero outputs/artifacts"
                            logger.error(msg, extra={"job_id": job_id})

                            # treat like a failure, reschedule if allowed
                            if attempt < max_tries:
                                delay = min(60, 5 * (2 ** (attempt - 1)))
                                await self.repo.reschedule_job(
                                    job_id=job_id,
                                    delay_seconds=delay,
                                    error_code="NO_OUTPUTS",
                                    error_message=msg,
                                )
                                logger.info(
                                    "Job rescheduled",
                                    extra={"job_id": job_id, "delay": delay, "attempt": attempt, "reason": "NO_OUTPUTS"},
                                )
                            else:
                                await self.repo.update_status(
                                    job_id,
                                    "failed",
                                    error_code="NO_OUTPUTS",
                                    error_message=msg,
                                    meta_patch={"worker_id": self.worker_id},
                                )

                            await asyncio.sleep(0)
                            continue

                        logger.info(
                            "Job succeeded",
                            extra={"job_id": job_id, "outputs": outputs, "artifacts": artifacts},
                        )
                    else:
                        # failed/cancelled/etc.
                        logger.info(
                            "Job finished",
                            extra={"job_id": job_id, "status": status_after, "error": err_after},
                        )

                except Exception as e:
                    logger.exception(
                        "Job failed (worker exception)",
                        extra={"job_id": job_id, "worker_id": self.worker_id, "error": str(e)},
                    )

                    # Load job again to get attempt_count after claim increment
                    job = await self.repo.get_job(job_id)
                    attempt = int(getattr(job, "attempt_count", 1) or 1)
                    max_tries = await self._get_max_tries(job)

                    if attempt < max_tries:
                        delay = min(60, 5 * (2 ** (attempt - 1)))
                        await self.repo.reschedule_job(
                            job_id=job_id,
                            delay_seconds=delay,
                            error_code="worker_error",
                            error_message=str(e),
                        )
                        logger.info("Job rescheduled", extra={"job_id": job_id, "delay": delay, "attempt": attempt})
                    else:
                        await self.repo.update_status(
                            job_id,
                            "failed",
                            error_code="worker_error",
                            error_message=str(e),
                            meta_patch={"worker_id": self.worker_id},
                        )

                await asyncio.sleep(0)

        finally:
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