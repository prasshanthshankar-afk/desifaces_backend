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
            if isinstance(v, str):
                v2 = json.loads(v)
                return v2 if isinstance(v2, dict) else {}
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    try:
        v = dict(x)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


async def _set_payload_stage(con, job_id: UUID, stage: str, error_text: str | None = None) -> None:
    row = await con.fetchrow(
        "select payload_json from public.studio_jobs where id=$1 and studio_type='commerce'",
        job_id,
    )
    payload = _as_dict(row["payload_json"] if row else {})
    computed = _as_dict(payload.get("computed"))
    computed["stage"] = stage
    if error_text:
        computed["error"] = error_text
    payload["computed"] = computed
    payload["stage"] = stage
    await con.execute(
        """
        update public.studio_jobs
        set payload_json=$2::jsonb, updated_at=now()
        where id=$1 and studio_type='commerce'
        """,
        job_id,
        json.dumps(payload),
    )


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
    # ✅ reflect failure in payload_json too (so your SQL query isn't stuck on queued)
    await _set_payload_stage(con, job_id, "failed", error_text=(msg or "")[:300])


async def _process_job(*, job_id: UUID, payload: Dict[str, Any], meta: Dict[str, Any], user_id: UUID) -> None:
    processor = None

    try:
        from app.services.commerce_processor import process_commerce_job  # type: ignore
        processor = ("fn", process_commerce_job)
    except Exception:
        pass

    if processor is None:
        raise RuntimeError(
            "No commerce processor found. Implement app.services.commerce_processor.process_commerce_job"
        )

    kind, obj = processor
    if kind == "fn":
        await obj(job_id=job_id, payload=payload, meta=meta, user_id=user_id)  # type: ignore[misc]
        return


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

                # ✅ set running stage in payload_json early (even if processor fails fast)
                await _set_payload_stage(con, job_id, "running")

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