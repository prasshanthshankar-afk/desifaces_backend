from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.db import get_pool
from app.repos.tts_jobs_repo import TTSJobsRepo
from app.services.tts_orchestrator import TTSOrchestrator

logger = logging.getLogger("audio_worker")


class AudioWorker:
    def __init__(self):
        self.poll_secs = float(settings.WORKER_POLL_SECS)
        self.batch_size = int(settings.WORKER_BATCH_SIZE)

        self.pool = None
        self.jobs = None
        self.orch = None

    async def _ensure_init(self) -> None:
        if self.pool is not None:
            return
        self.pool = await get_pool()
        self.jobs = TTSJobsRepo(self.pool, studio_type="audio")
        self.orch = TTSOrchestrator(self.pool)

    async def run_forever(self) -> None:
        await self._ensure_init()
        logger.info("AudioWorker started poll_secs=%s batch_size=%s", self.poll_secs, self.batch_size)

        while True:
            try:
                job_ids = await self.jobs.fetch_next_queued_jobs(limit=self.batch_size)
                if not job_ids:
                    await asyncio.sleep(self.poll_secs)
                    continue

                for job_id in job_ids:
                    try:
                        logger.info("Processing audio job %s", job_id)
                        await self.orch.process_job(job_id)
                        logger.info("Audio job finished %s", job_id)
                    except Exception as e:
                        logger.exception("Audio job failed %s", job_id)
                        # Optional: backoff + requeue
                        try:
                            await self.jobs.requeue_job(job_id, delay_seconds=15, error_code="worker_exception", error_message=str(e))
                        except Exception:
                            logger.exception("Failed to requeue job %s", job_id)

            except Exception:
                logger.exception("Worker loop error")
                await asyncio.sleep(self.poll_secs)


async def main() -> None:
    worker = AudioWorker()
    await worker.run_forever()


if __name__ == "__main__":
    logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    asyncio.run(main())