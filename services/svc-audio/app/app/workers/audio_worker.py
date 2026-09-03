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
    raw = str(os.getenv("DF_AUDIO_WORKER_CONCURRENCY", "32") or "32").strip()
    try:
        return max(1, min(64, int(raw)))
    except Exception:
        return 32


def _stale_after_seconds() -> int:
    raw = str(os.getenv("DF_AUDIO_WORKER_STALE_SECONDS", "90") or "90").strip()
    try:
        return max(30, min(900, int(raw)))
    except Exception:
        return 90


def _max_attempts() -> int:
    raw = str(os.getenv("DF_AUDIO_WORKER_MAX_ATTEMPTS", "3") or "3").strip()
    try:
        return max(1, min(10, int(raw)))
    except Exception:
        return 3


class AudioWorker:
    def __init__(self):
        self.poll_secs = float(settings.WORKER_POLL_SECS)
        self.batch_size = max(1, int(settings.WORKER_BATCH_SIZE))
        self.concurrency = _worker_concurrency(self.batch_size)
        self.stale_after_seconds = _stale_after_seconds()
        self.max_attempts = _max_attempts()

        self.pool = None
        self.jobs = None

    async def _ensure_init(self) -> None:
        if self.pool is not None:
            return
        self.pool = await get_pool()
        self.jobs = TTSJobsRepo(self.pool, studio_type="audio")

    async def _recover_stale_jobs(self) -> None:
        recovered = await self.jobs.requeue_stale_running_jobs(
            stale_after_seconds=self.stale_after_seconds,
            max_attempts=self.max_attempts,
            limit=max(self.batch_size, self.concurrency),
        )
        if recovered:
            logger.warning(
                "Recovered %s stale Audio jobs after expired worker lease: %s",
                len(recovered),
                ",".join(recovered),
            )

    async def _process_one(self, job_id: str) -> None:
        orch = TTSOrchestrator(self.pool)
        try:
            logger.info("Processing audio job %s", job_id)
            await orch.process_job(job_id)
            logger.info("Audio job finished %s", job_id)
        except Exception as e:
            logger.exception("Audio job failed %s", job_id)
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
            "AudioWorker started poll_secs=%s batch_size=%s concurrency=%s stale_after_seconds=%s max_attempts=%s",
            self.poll_secs,
            self.batch_size,
            self.concurrency,
            self.stale_after_seconds,
            self.max_attempts,
        )

        # Recover abandoned claims immediately at worker startup. This is what
        # heals jobs stranded by a container restart without creating a new job id
        # or a second pricing reservation.
        await self._recover_stale_jobs()

        while True:
            try:
                await self._recover_stale_jobs()

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
