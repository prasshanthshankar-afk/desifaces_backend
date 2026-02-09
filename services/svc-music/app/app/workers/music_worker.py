from __future__ import annotations

import asyncio
import os

from app.repos.music_jobs_repo import MusicJobsRepo
from app.services.music_orchestrator import run_music_video_job


POLL_SECS = float(os.getenv("WORKER_POLL_SECS", "1.5"))
BATCH_LIMIT = int(os.getenv("MUSIC_WORKER_BATCH_LIMIT", "5"))

# ✅ Optional: reclaim jobs stuck in running (e.g., worker crash).
# Set to 0 or unset to disable reclaim.
STALE_SECS = int(os.getenv("MUSIC_JOB_STALE_SECS", "1800"))  # 30 minutes


async def tick_once(limit: int = BATCH_LIMIT) -> None:
    repo = MusicJobsRepo()

    # ✅ Atomically claim jobs so multiple workers won't double-process
    if STALE_SECS and STALE_SECS > 0:
        claimed = await repo.claim_video_jobs(limit=limit, stale_after_secs=STALE_SECS)
    else:
        claimed = await repo.claim_video_jobs(limit=limit)

    for j in claimed:
        await run_music_video_job(j["id"])


async def main() -> None:
    while True:
        try:
            await tick_once()
        except Exception as e:
            # keep worker alive; log to stdout (docker)
            print(f"[music-worker] tick error: {e}", flush=True)
        await asyncio.sleep(POLL_SECS)


if __name__ == "__main__":
    asyncio.run(main())