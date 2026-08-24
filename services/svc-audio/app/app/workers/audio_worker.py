from __future__ import annotations

import asyncio
import logging
import os

from app.config import settings
from app.db import get_pool
from app.repos.tts_jobs_repo import TTSJobsRepo
from app.services.tts_orchestrator import TTSOrchestrator

logger = logging.getLogger("audio_worker")


def _worker_concurrency(default_batch_size: int) -> int:
    raw = str(
        os.getenv(
            "DF_AUDIO_WORKER_CONCURRENCY",
            str(max(1, default_batch_size)),
        )
        or max(1, default_batch_size)
    ).strip()
    try:
        return max(1, min(64, int(raw)))
    except Exception:
        return max(1, default_batch_size)


class AudioWorker:
    def __init__(self):
        self.poll_secs = float(settings.WORKER_POLL_SECS)
        self.batch_size = max(1, int(settings.WORKER_BATCH_SIZE))
        self.concurrency = _worker_concurrency(self.batch_size)

        self.pool = None
        self.jobs = None

    async def _ensure_init(self) -> None:
        if self.pool is not None:
            return
        self.pool = await get_pool()
        self.jobs = TTSJobsRepo(self.pool, studio_type="audio")

    async def _process_one(self, job_id: str) -> None:
        # Use one orchestrator instance per concurrent job so no request/job-local
        # state can leak across sibling TTS executions while sharing the DB pool.
        orch = TTSOrchestrator(self.pool)
        try:
            logger.info("Processing audio job %s", job_id)
            await orch.process_job(job_id)
            logger.info("Audio job finished %s", job_id)
        except Exception as e:
            logger.exception("Audio job failed %s", job_id)

            # The orchestrator owns the business lifecycle. If it already
            # terminalized the job (for example after releasing pricing), never
            # resurrect that job by changing it back to queued.
            try:
                current = await self.jobs.get_job(job_id)
                current_status = str(
                    (current or {}).get("status") or ""
                ).strip().lower()

                if current_status == "running":
                    await self.jobs.requeue_job(
                        job_id,
                        delay_seconds=15,
                        error_code="worker_exception",
                        error_message=str(e),
                    )
                else:
                    logger.info(
                        "Not requeueing audio job %s status=%s",
                        job_id,
                        current_status or "unknown",
                    )
            except Exception:
                logger.exception(
                    "Failed to evaluate/requeue job %s",
                    job_id,
                )

    async def run_forever(self) -> None:
        await self._ensure_init()
        logger.info(
            "AudioWorker started poll_secs=%s batch_size=%s concurrency=%s",
            self.poll_secs,
            self.batch_size,
            self.concurrency,
        )

        while True:
            try:
                claim_limit = max(self.batch_size, self.concurrency)
                job_ids = await self.jobs.fetch_next_queued_jobs(limit=claim_limit)
                if not job_ids:
                    await asyncio.sleep(self.poll_secs)
                    continue

                semaphore = asyncio.Semaphore(self.concurrency)

                async def guarded(job_id: str) -> None:
                    async with semaphore:
                        await self._process_one(job_id)

                await asyncio.gather(*(guarded(job_id) for job_id in job_ids))

            except Exception:
                logger.exception("Worker loop error")
                await asyncio.sleep(self.poll_secs)


async def main() -> None:
    worker = AudioWorker()
    await worker.run_forever()


if __name__ == "__main__":
    logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    asyncio.run(main())
