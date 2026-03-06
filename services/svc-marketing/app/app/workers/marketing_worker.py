# services/svc-marketing/app/app/workers/marketing_worker.py
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import traceback

from app.config import settings
from app.db import get_pool
from app.repos.marketing_runs_repo import MarketingRunsRepo
from app.services.orchestration.run_executor import RunExecutor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("svc-marketing-worker")


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def _heartbeat_task(
    repo: MarketingRunsRepo,
    run_id,
    worker_id: str,
    heartbeat_s: int,
    lease_s: int,
    stop_evt: asyncio.Event,
) -> None:
    while not stop_evt.is_set():
        try:
            await asyncio.wait_for(stop_evt.wait(), timeout=float(heartbeat_s))
            break
        except asyncio.TimeoutError:
            pass
        try:
            await repo.heartbeat(run_id, worker_id=worker_id, lease_seconds=lease_s)
        except Exception as e:
            # best-effort; don't crash worker
            logger.warning("heartbeat failed run_id=%s err=%s", run_id, e)


async def main() -> None:
    wid = _worker_id()
    pool = await get_pool()
    repo = MarketingRunsRepo(pool)
    executor = RunExecutor(pool)

    poll_s = int(getattr(settings, "WORKER_POLL_SECONDS", 2))
    reap_every_s = int(getattr(settings, "WORKER_REAP_EVERY_SECONDS", 30))
    heartbeat_s = int(getattr(settings, "WORKER_HEARTBEAT_SECONDS", 15))
    lease_s = int(getattr(settings, "WORKER_LEASE_SECONDS", 60))

    planning_to = int(getattr(settings, "MARKETING_PLANNING_TIMEOUT_S", 600))
    generate_to = int(getattr(settings, "MARKETING_GENERATE_TIMEOUT_S", 1200))
    publish_to = int(getattr(settings, "MARKETING_PUBLISH_TIMEOUT_S", 900))

    # stale should exceed any stage timeout + buffer
    stale_after_s = int(getattr(settings, "WORKER_STALE_AFTER_SECONDS", max(planning_to, generate_to, publish_to) + 120))
    run_hard_timeout_s = int(getattr(settings, "WORKER_RUN_HARD_TIMEOUT_SECONDS", max(planning_to, generate_to, publish_to) + 300))

    logger.info(
        "worker started wid=%s poll=%ss reap_every=%ss stale_after=%ss heartbeat=%ss lease=%ss run_hard_timeout=%ss",
        wid,
        poll_s,
        reap_every_s,
        stale_after_s,
        heartbeat_s,
        lease_s,
        run_hard_timeout_s,
    )

    last_reap = 0.0

    # Start fresh: reap stale running runs immediately
    try:
        n = await repo.reap_stuck_runs(stale_after_seconds=stale_after_s, limit=200)
        if n:
            logger.warning("startup reaped=%s stale running runs -> failed", n)
    except Exception as e:
        logger.warning("startup reap failed err=%s", e)

    while True:
        now = time.time()
        if (now - last_reap) >= float(reap_every_s):
            last_reap = now
            try:
                n = await repo.reap_stuck_runs(stale_after_seconds=stale_after_s, limit=50)
                if n:
                    logger.warning("periodic reaped=%s stale running runs -> failed", n)
            except Exception as e:
                logger.warning("periodic reap failed err=%s", e)

        try:
            run_id = await repo.claim_next_run(worker_id=wid, lease_seconds=lease_s)
        except Exception as e:
            logger.exception("claim_next_run crashed err=%s", e)
            await asyncio.sleep(float(poll_s))
            continue

        if not run_id:
            await asyncio.sleep(float(poll_s))
            continue

        logger.info("claimed run_id=%s wid=%s", run_id, wid)

        stop_evt = asyncio.Event()
        hb = asyncio.create_task(_heartbeat_task(repo, run_id, wid, heartbeat_s, lease_s, stop_evt))

        t0 = time.time()
        try:
            await asyncio.wait_for(executor.execute(run_id), timeout=float(run_hard_timeout_s))
            logger.info("done run_id=%s elapsed=%.2fs", run_id, time.time() - t0)
        except asyncio.TimeoutError:
            msg = f"worker hard-timeout after {run_hard_timeout_s}s"
            logger.error("run timeout run_id=%s %s", run_id, msg)
            try:
                await repo.mark_failed(run_id, stage="worker", error_code="WORKER_TIMEOUT", error_message=msg)
            except Exception as e:
                logger.warning("failed to mark_failed run_id=%s err=%s", run_id, e)
        except Exception as e:
            tb = traceback.format_exc()
            logger.error("run crashed run_id=%s err=%s\n%s", run_id, e, tb)
            try:
                await repo.mark_failed(run_id, stage="worker", error_code="WORKER_EXCEPTION", error_message=str(e))
            except Exception as e2:
                logger.warning("failed to mark_failed run_id=%s err=%s", run_id, e2)
        finally:
            stop_evt.set()
            hb.cancel()
            try:
                await hb
            except Exception:
                pass

    # not reached


if __name__ == "__main__":
    asyncio.run(main())