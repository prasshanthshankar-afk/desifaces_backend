#!/usr/bin/env bash
set -Eeuo pipefail

STORY_ID="${STORY_ID:-}"
[[ -n "$STORY_ID" ]] || { echo "FAIL: STORY_ID is required" >&2; exit 2; }
[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: run on desifaces-dev" >&2; exit 2; }

docker inspect df-svc-director >/dev/null 2>&1 || { echo "FAIL: df-svc-director missing" >&2; exit 2; }

docker exec -i -e STORY_ID="$STORY_ID" df-svc-director python - <<'PY'
import asyncio, json, os
from datetime import datetime, timezone
from app.db import get_pool

story_id = os.environ["STORY_ID"]

def dt(v):
    return v if isinstance(v, datetime) else None

def secs(a,b):
    if not a or not b:
        return None
    return round((b-a).total_seconds(), 3)

def fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3f}s"
    return str(v)

async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        workflow = await conn.fetchrow("""
            select workflow_id,story_id,state,current_stage,final_media_id,created_at,updated_at
            from public.v3_studio_workflows
            where story_id=$1::uuid
            order by updated_at desc
            limit 1
        """, story_id)
        if not workflow:
            raise SystemExit("FAIL: workflow not found for story_id")

        fusion = await conn.fetchrow("""
            select stage_run_id,state,scene_id,created_at,updated_at,metadata_json
            from public.v3_studio_stage_runs
            where workflow_id=$1 and stage_type='fusion'
            order by created_at desc
            limit 1
        """, workflow["workflow_id"])
        if not fusion:
            raise SystemExit("FAIL: fusion stage not found")

        attempt = await conn.fetchrow("""
            select *
            from public.v3_studio_stage_attempts
            where stage_run_id=$1
            order by attempt_no desc
            limit 1
        """, fusion["stage_run_id"])

        jobs = await conn.fetch("""
            select id,status,payload_json,meta_json,error_code,error_message,created_at,updated_at
            from public.studio_jobs
            where studio_type='fusion'
              and payload_json->'tags'->>'stage_run_id'=$1
            order by created_at
        """, str(fusion["stage_run_id"]))

        job_ids = [row["id"] for row in jobs]
        runs = []
        steps = []
        if job_ids:
            runs = await conn.fetch("""
                select job_id,provider,provider_job_id,provider_status,created_at,updated_at
                from public.provider_runs
                where job_id=any($1::uuid[])
                order by created_at
            """, job_ids)
            steps = await conn.fetch("""
                select job_id,step_code,status,attempt,created_at,updated_at
                from public.studio_job_steps
                where job_id=any($1::uuid[])
                order by job_id,created_at
            """, job_ids)

    w = dict(workflow)
    f = dict(fusion)
    a = dict(attempt) if attempt else {}
    meta = a.get("metadata_json") or {}
    if not isinstance(meta, dict):
        meta = {}
    perf = meta.get("dispatch_performance") or {}
    if not isinstance(perf, dict):
        perf = {}

    print("============================================================")
    print(" desifaces DEV — SHARED-SCENE VIDEO TIMING ANALYSIS")
    print("============================================================")
    print(f"story_id={story_id}")
    print(f"workflow_id={w['workflow_id']}")
    print(f"fusion_stage_run_id={f['stage_run_id']}")
    print(f"workflow_state={w['state']} current_stage={w['current_stage']}")
    print(f"fusion_stage_state={f['state']}")
    print()

    attempt_created = dt(a.get("created_at"))
    attempt_completed = dt(a.get("completed_at")) or dt(a.get("updated_at"))
    print("===== SCENE ATTEMPT =====")
    print(f"attempt_no={a.get('attempt_no','-')} state={a.get('state','-')}")
    print(f"attempt_created_at={attempt_created}")
    print(f"attempt_completed_at={attempt_completed}")
    print(f"attempt_wall={fmt(secs(attempt_created, attempt_completed))}")
    print(f"dispatch_elapsed_ms={perf.get('dispatch_elapsed_ms','-')}")
    print(f"dispatch_spread_ms={perf.get('dispatch_spread_ms','-')}")
    print(f"max_parallel_dispatch_observed={perf.get('max_parallel_dispatch_observed','-')}")
    print(f"dispatch_concurrency={perf.get('dispatch_concurrency','-')}")
    print()

    run_by_job = {str(r["job_id"]): dict(r) for r in runs}
    steps_by_job = {}
    for s in steps:
        steps_by_job.setdefault(str(s["job_id"]), []).append(dict(s))

    print("===== CHILD FUSION JOBS =====")
    child_end_times=[]
    child_start_times=[]
    provider_durations=[]
    queue_durations=[]
    for idx,row in enumerate(jobs,1):
        j=dict(row)
        jid=str(j["id"])
        tags=(j.get("payload_json") or {}).get("tags") or {}
        r=run_by_job.get(jid,{})
        created=dt(j.get("created_at"))
        updated=dt(j.get("updated_at"))
        provider_created=dt(r.get("created_at"))
        provider_updated=dt(r.get("updated_at"))
        child_start_times.append(created)
        child_end_times.append(updated)
        if provider_created and created:
            queue_durations.append((provider_created-created).total_seconds())
        if provider_created and provider_updated:
            provider_durations.append((provider_updated-provider_created).total_seconds())
        print(
            f"child={idx} seq={tags.get('segment_sequence','-')} "
            f"job={jid[:8]} status={j.get('status')} "
            f"provider={r.get('provider','-')} provider_status={r.get('provider_status','-')} "
            f"queue_to_provider={fmt(secs(created,provider_created))} "
            f"provider_wall={fmt(secs(provider_created,provider_updated))} "
            f"child_total={fmt(secs(created,updated))}"
        )
        for s in steps_by_job.get(jid,[]):
            print(
                f"  step={s.get('step_code')} status={s.get('status')} "
                f"wall={fmt(secs(dt(s.get('created_at')),dt(s.get('updated_at'))))}"
            )

    print()
    print("===== CRITICAL PATH =====")
    valid_starts=[x for x in child_start_times if x]
    valid_ends=[x for x in child_end_times if x]
    first_child=min(valid_starts) if valid_starts else None
    last_child=max(valid_ends) if valid_ends else None
    print(f"child_generation_wall={fmt(secs(first_child,last_child))}")
    if provider_durations:
        print(f"provider_avg={sum(provider_durations)/len(provider_durations):.3f}s")
        print(f"provider_min={min(provider_durations):.3f}s")
        print(f"provider_max={max(provider_durations):.3f}s")
    if queue_durations:
        print(f"queue_to_provider_avg={sum(queue_durations)/len(queue_durations):.3f}s")
        print(f"queue_to_provider_max={max(queue_durations):.3f}s")
    if last_child and attempt_completed:
        print(f"post_children_to_scene_ready={fmt(secs(last_child,attempt_completed))}")
    print(f"child_count={len(jobs)}")
    print("production=UNTOUCHED")
    print("============================================================")

asyncio.run(main())
PY
