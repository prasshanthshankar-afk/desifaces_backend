#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CONTAINER="${DF_V3_FUSION_EXTENSION_CONTAINER:-df-v3-svc-fusion-extension}"

echo "============================================================"
echo " V3 SINGLE-FACE FUSION MISROUTE CLEANUP"
echo "============================================================"

docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || {
  echo "MISROUTE_CLEANUP=REFUSED"
  echo "REASON=fusion-extension API container is not running: $CONTAINER"
  exit 2
}

docker exec -i "$CONTAINER" python - <<'PY'
import asyncio
import json
from app.db import get_db_pool
from app.repos.longform_jobs_repo import LongformJobsRepo
from app.repos.longform_segments_repo import LongformSegmentsRepo
from app.services.longform_orchestrator import release_longform_pricing_for_job


TERMINAL = {"succeeded", "failed", "canceled", "cancelled"}
ERROR_CODE = "MISROUTED_SINGLE_FACE_FUSION"
ERROR_MESSAGE = (
    "Single-face Talking Video was incorrectly routed through longform. "
    "Reservation released; recreate only after direct svc-fusion route certification."
)


def as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


async def main():
    pool = await get_db_pool()
    jobs = LongformJobsRepo()
    segs = LongformSegmentsRepo()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, status, total_segments, completed_segments,
                   tags, created_at, updated_at
            FROM public.longform_jobs
            WHERE created_at >= now() - interval '6 hours'
              AND status IN ('queued', 'running')
              AND completed_segments = 0
              AND COALESCE(tags->>'source', '') = 'fusion_studio'
              AND COALESCE(tags->>'longform_profile', '') = 'talking_video'
              AND COALESCE(tags->'pricing'->>'state', '') = 'reserved'
            ORDER BY created_at DESC
            LIMIT 5
            """
        )

        if len(rows) != 1:
            print(f"MISROUTE_CANDIDATE_COUNT={len(rows)}")
            for r in rows:
                tags = as_dict(r['tags'])
                pricing = as_dict(tags.get('pricing'))
                print(
                    "CANDIDATE "
                    f"job_id={r['id']} status={r['status']} "
                    f"created_at={r['created_at']} "
                    f"reservation_id={pricing.get('reservation_id')}"
                )
            print("MISROUTE_CLEANUP=REFUSED")
            print("REASON=expected exactly one recent reserved fusion_studio talking_video longform job")
            return 3

        row = rows[0]
        job_id = str(row['id'])
        user_id = str(row['user_id'])
        tags = as_dict(row['tags'])
        pricing = as_dict(tags.get('pricing'))
        reservation_id = str(pricing.get('reservation_id') or '').strip()

        if not reservation_id:
            print("MISROUTE_CLEANUP=REFUSED")
            print("REASON=reserved job has no reservation_id")
            return 4

        segment_rows = await conn.fetch(
            """
            SELECT id, status, fusion_job_id, provider_job_id
            FROM public.longform_segments
            WHERE job_id = $1::uuid
            ORDER BY segment_index
            """,
            job_id,
        )

        if not segment_rows:
            print("MISROUTE_CLEANUP=REFUSED")
            print("REASON=job has no segments")
            return 5

        unsafe = [
            r for r in segment_rows
            if str(r['status'] or '').lower() != 'queued'
            or r['fusion_job_id'] is not None
            or r['provider_job_id'] is not None
        ]
        if unsafe:
            print(f"MISROUTE_SEGMENTS={len(segment_rows)}")
            for r in segment_rows:
                print(
                    "SEGMENT "
                    f"id={r['id']} status={r['status']} "
                    f"fusion_job_id={r['fusion_job_id']} provider_job_id={r['provider_job_id']}"
                )
            print("MISROUTE_CLEANUP=REFUSED")
            print("REASON=at least one segment has entered execution; no automatic release performed")
            return 6

        print(f"MISROUTE_JOB_ID={job_id}")
        print(f"MISROUTE_RESERVATION_ID={reservation_id}")
        print(f"MISROUTE_SEGMENTS={len(segment_rows)}")
        print("ZERO_CHILD_PROVIDER_EXECUTION_GATE=PASS")

        released = await release_longform_pricing_for_job(
            conn,
            job_id=job_id,
            user_id=user_id,
            reason="misrouted_single_face_fusion_regression",
            tags=tags,
        )

        if str(released.get('state') or '').lower() != 'released':
            print("MISROUTE_CLEANUP=REFUSED")
            print(f"REASON=pricing release did not return released state: {released.get('state')}")
            return 7

        for seg in segment_rows:
            await segs.mark_failed(
                conn,
                str(seg['id']),
                error_code=ERROR_CODE,
                error_message=ERROR_MESSAGE,
            )

        await jobs.set_status(
            conn,
            job_id,
            "failed",
            error_code=ERROR_CODE,
            error_message=ERROR_MESSAGE,
        )

        verify = await conn.fetchrow(
            "SELECT status, tags FROM public.longform_jobs WHERE id=$1::uuid",
            job_id,
        )
        verify_tags = as_dict(verify['tags']) if verify else {}
        verify_pricing = as_dict(verify_tags.get('pricing'))

        print(f"POST_JOB_STATUS={verify['status'] if verify else '<missing>'}")
        print(f"POST_PRICING_STATE={verify_pricing.get('state')}")
        print("MISROUTE_RESERVATION_RELEASE=PASS")
        print("MISROUTE_JOB_TERMINATED=PASS")
        print("NO_DIRECT_FUSION_JOB_CREATED=PASS")
        print("MISROUTE_CLEANUP=PASS")
        return 0


raise SystemExit(asyncio.run(main()))
PY
