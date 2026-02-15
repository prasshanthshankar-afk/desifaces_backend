from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict
from uuid import UUID

from app.db import get_pool

logger = logging.getLogger("svc-commerce-worker")

POLL_SECS = float(os.getenv("COMMERCE_WORKER_POLL_SECS") or "1.5")
HEARTBEAT_SECS = float(os.getenv("COMMERCE_WORKER_HEARTBEAT_SECS") or "30")

CLAIM_SQL = """
with candidate as (
  select id
  from public.studio_jobs
  where studio_type = 'commerce'
    and status = 'queued'
    and (next_run_at is null or next_run_at <= now())
  order by
    next_run_at nulls first,
    created_at asc
  for update skip locked
  limit 1
)
update public.studio_jobs j
set
  status = 'running',
  attempt_count = coalesce(j.attempt_count, 0) + 1,
  updated_at = now()
from candidate c
where j.id = c.id
returning j.id, j.user_id, j.payload_json, j.meta_json, j.attempt_count;
"""


async def _due_count(con) -> int:
    v = await con.fetchval(
        """
        select count(*)
        from public.studio_jobs
        where studio_type='commerce'
          and status='queued'
          and (next_run_at is null or next_run_at <= now())
        """
    )
    return int(v or 0)


def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, (bytes, bytearray)):
        x = x.decode("utf-8", errors="ignore")
    if isinstance(x, str):
        try:
            v = json.loads(x)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    try:
        v = dict(x)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


async def _mark_succeeded(con, job_id: UUID) -> None:
    await con.execute(
        """
        update public.studio_jobs
        set status='succeeded', updated_at=now()
        where id=$1
        """,
        job_id,
    )


async def _mark_failed(con, job_id: UUID, code: str, msg: str) -> None:
    await con.execute(
        """
        update public.studio_jobs
        set status='failed', error_code=$2, error_message=$3, updated_at=now()
        where id=$1
        """,
        job_id,
        code,
        (msg or "")[:900],
    )


async def _process_job(*, job_id: UUID, payload: Dict[str, Any], meta: Dict[str, Any], user_id: UUID) -> None:
    """
    Hook point: call your real commerce pipeline here.

    Supported processors:
    - app.services.commerce_processor.process_commerce_job(job_id=..., payload=..., meta=..., user_id=...)
    - app.services.commerce_orchestrator.CommerceOrchestrator().{run|process|handle|execute}(...)  (with or without job_id)
    - app.services.commerce_service.CommerceService().{run|process|handle|execute}(...)            (with or without job_id)
    """
    processor = None

    # Prefer function processor first (most explicit / least ambiguity)
    try:
        from app.services.commerce_processor import process_commerce_job  # type: ignore

        processor = ("fn", process_commerce_job)
    except Exception:
        pass

    # Then orchestrator/service object-based
    if processor is None:
        try:
            from app.services.commerce_orchestrator import CommerceOrchestrator  # type: ignore

            processor = ("obj", CommerceOrchestrator())
        except Exception:
            pass

    if processor is None:
        try:
            from app.services.commerce_service import CommerceService  # type: ignore

            processor = ("obj", CommerceService())
        except Exception:
            pass

    if processor is None:
        raise RuntimeError(
            "No commerce processor found. Implement one of: "
            "app.services.commerce_processor.process_commerce_job, "
            "app.services.commerce_orchestrator.CommerceOrchestrator, "
            "app.services.commerce_service.CommerceService"
        )

    kind, obj = processor

    if kind == "fn":
        # required signature
        await obj(job_id=job_id, payload=payload, meta=meta, user_id=user_id)  # type: ignore[misc]
        return

    # object-based processor: try common method names; support with or without job_id
    for meth in ("run", "process", "handle", "execute"):
        fn = getattr(obj, meth, None)
        if not fn:
            continue

        # Try passing job_id first
        try:
            out = fn(job_id=job_id, payload=payload, meta=meta, user_id=user_id)  # type: ignore[misc]
        except TypeError:
            # Fallback: older signatures without job_id
            out = fn(payload=payload, meta=meta, user_id=user_id)  # type: ignore[misc]

        if asyncio.iscoroutine(out):
            await out
        return

    raise RuntimeError(f"Processor {obj.__class__.__name__} has no known method (run/process/handle/execute).")


async def run_worker_forever() -> None:
    pool = await get_pool()

    logger.info("commerce_worker_started poll_secs=%.2f", POLL_SECS)

    last_hb = 0.0
    while True:
        try:
            async with pool.acquire() as con:
                now = time.time()
                if now - last_hb >= HEARTBEAT_SECS:
                    due = await _due_count(con)
                    logger.info("commerce_worker_heartbeat due=%s", due)
                    last_hb = now

                row = await con.fetchrow(CLAIM_SQL)
                if not row:
                    await asyncio.sleep(POLL_SECS)
                    continue

                job_id = UUID(str(row["id"]))
                user_id = UUID(str(row["user_id"]))
                payload = _as_dict(row["payload_json"])
                meta = _as_dict(row["meta_json"])
                attempt_count = int(row["attempt_count"] or 0)

                logger.info("commerce_worker_claimed job_id=%s attempt=%s", job_id, attempt_count)

                try:
                    await _process_job(job_id=job_id, payload=payload, meta=meta, user_id=user_id)
                    await _mark_succeeded(con, job_id)
                    logger.info("commerce_worker_done job_id=%s status=succeeded", job_id)
                except Exception as e:  # noqa: BLE001
                    await _mark_failed(con, job_id, "commerce_worker_error", str(e))
                    logger.exception("commerce_worker_failed job_id=%s", job_id)

        except Exception:
            logger.exception("commerce_worker_loop_error")
            await asyncio.sleep(POLL_SECS)


def main() -> None:
    asyncio.run(run_worker_forever())


if __name__ == "__main__":
    main()