#!/usr/bin/env python3
from __future__ import annotations

import json
import socket
import subprocess
from collections import Counter

EXPECTED_HOST = "desifaces-dev"
DB = "desifaces-v3-db"
WORKER = "df-v3-svc-fusion-extension-stitch-worker"


def run(args, *, check=True):
    return subprocess.run(
        [str(x) for x in args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
    )


def psql(sql: str) -> str:
    cp = run([
        "docker", "exec", "-e", f"DF_READONLY_SQL={sql}", DB,
        "sh", "-lc",
        'psql -X -A -t -F "|" -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${POSTGRES_DB:-desifaces}" -c "$DF_READONLY_SQL"',
    ], check=False)
    if cp.returncode != 0:
        raise RuntimeError("read-only query failed: " + cp.stdout[-1200:])
    return cp.stdout.strip()


def rows(text: str):
    return [line.split("|") for line in text.splitlines() if line.strip()]


def inspect_worker() -> dict[str, str]:
    cp = run([
        "docker", "inspect", WORKER,
        "--format",
        "{{json .State}}|{{json .Config.Env}}|{{.Image}}",
    ], check=False)
    if cp.returncode != 0:
        return {"present": "no", "error": cp.stdout.strip()[-500:]}
    raw = cp.stdout.strip()
    try:
        state_raw, env_raw, image = raw.split("|", 2)
        state = json.loads(state_raw)
        env = json.loads(env_raw)
    except Exception as exc:
        return {"present": "yes", "parse_error": str(exc)[:200]}
    selected = {}
    for item in env or []:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key in {
            "DF_V3_SCENE_COORDINATOR_ENABLED",
            "DF_V3_SCENE_COORDINATOR_POLL_SECONDS",
            "DF_V3_SCENE_COORDINATOR_BATCH_SIZE",
            "DF_V3_SCENE_COORDINATOR_STATUS_CONCURRENCY",
        }:
            selected[key] = value
    return {
        "present": "yes",
        "status": str(state.get("Status") or ""),
        "running": "yes" if state.get("Running") else "no",
        "started_at": str(state.get("StartedAt") or ""),
        "finished_at": str(state.get("FinishedAt") or ""),
        "exit_code": str(state.get("ExitCode") if state.get("ExitCode") is not None else ""),
        "error": str(state.get("Error") or ""),
        "image": image[:28],
        **selected,
    }


def main() -> int:
    host = socket.gethostname().split(".", 1)[0]
    if host != EXPECTED_HOST:
        raise SystemExit(f"FAIL: run only on {EXPECTED_HOST}; current={host}")

    print("============================================================")
    print(" desifaces DEV — MULTI-PERSON SCENE FINALIZER AUDIT")
    print("============================================================")
    print("environment=DEV_ONLY")
    print("database_writes=NONE")
    print("provider_calls=NONE")
    print("container_restarts=NONE")
    print("production_touch=NONE")

    latest = rows(psql("""
with candidate as (
  select w.workflow_id,s.stage_run_id,s.scene_id,s.state,w.current_stage,s.updated_at,
         case when s.state='generating' then 0 else 1 end as rank_state
  from public.v3_studio_stage_runs s
  join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
  where s.stage_type='fusion' and s.scope_type='scene'
  order by rank_state,s.updated_at desc
  limit 1
)
select workflow_id::text,stage_run_id::text,scene_id::text,state::text,current_stage::text,
       extract(epoch from(now()-updated_at))::int::text
from candidate;
"""))
    if not latest:
        raise SystemExit("FAIL: no Fusion Scene stage found")
    workflow_id, stage_id, scene_id, scene_state, current_stage, age = latest[0]

    print("\n===== TARGET SCENE =====")
    print(f"workflow_id={workflow_id}")
    print(f"stage_run_id={stage_id}")
    print(f"scene_id={scene_id}")
    print(f"scene_state={scene_state}")
    print(f"workflow_current_stage={current_stage}")
    print(f"stage_age_seconds={age}")

    attempt = rows(psql(f"""
select attempt_id::text,attempt_no::text,state::text,
       coalesce(error_code::text,''),coalesce(error_message::text,''),
       extract(epoch from(now()-updated_at))::int::text,
       coalesce(metadata_json #>> '{{background_coordinator,phase}}',''),
       coalesce(metadata_json #>> '{{background_coordinator,last_error}}',''),
       coalesce(metadata_json #>> '{{background_coordinator,status}}',''),
       coalesce(metadata_json #>> '{{background_coordinator,updated_at}}','')
from public.v3_studio_stage_attempts
where stage_run_id='{stage_id}'::uuid
order by attempt_no desc limit 1;
"""))

    print("\n===== LATEST SCENE ATTEMPT =====")
    if attempt:
        a = attempt[0]
        while len(a) < 10:
            a.append("")
        print(f"attempt_id={a[0]}")
        print(f"attempt_no={a[1]}")
        print(f"attempt_state={a[2]}")
        print(f"attempt_error_code={a[3] or 'NONE'}")
        print(f"attempt_error_message={a[4] or 'NONE'}")
        print(f"attempt_age_seconds={a[5]}")
        print(f"coordinator_phase={a[6] or 'NONE'}")
        print(f"coordinator_last_error={a[7] or 'NONE'}")
        print(f"coordinator_status={a[8] or 'NONE'}")
        print(f"coordinator_updated_at={a[9] or 'NONE'}")
    else:
        a = ["", "", "", "", "", "", "", "", "", ""]
        print("attempt=NONE")

    child_rows = rows(psql(f"""
select j.status::text,count(*)::text
from public.studio_jobs j
where j.studio_type='fusion' and (
 j.payload_json #>> '{{provider_options,billing_context,billing_parent_job_id}}'='{stage_id}' or
 j.payload_json #>> '{{provider_options,billing_context,parent_longform_job_id}}'='{stage_id}' or
 j.payload_json #>> '{{provider_options,billing_context,parent_job_id}}'='{stage_id}' or
 j.payload_json #>> '{{tags,billing_context,billing_parent_job_id}}'='{stage_id}' or
 j.payload_json #>> '{{tags,billing_context,parent_longform_job_id}}'='{stage_id}' or
 j.payload_json #>> '{{tags,billing_context,parent_job_id}}'='{stage_id}'
)
group by j.status order by j.status;
"""))
    counts = Counter({r[0]: int(r[1]) for r in child_rows})
    total = sum(counts.values())
    succeeded = sum(counts.get(s, 0) for s in ("succeeded", "completed", "complete", "ready"))
    active = sum(counts.get(s, 0) for s in ("queued", "processing", "running", "submitted", "pending"))
    failed = sum(counts.get(s, 0) for s in ("failed", "blocked", "canceled", "cancelled"))
    print("\n===== CHILD VIDEO FAN-IN =====")
    for state, count in child_rows:
        print(f"video_child_{state}={count}")
    print(f"video_child_total={total}")
    print(f"video_child_succeeded={succeeded}")
    print(f"video_child_active={active}")
    print(f"video_child_failed={failed}")
    print(f"FAN_IN_COMPLETE={'YES' if total > 0 and succeeded == total and active == 0 and failed == 0 else 'NO'}")

    output = rows(psql(f"""
select count(*)::text,
       count(*) filter (where o.is_active=true)::text,
       count(*) filter (where o.is_active=true and r.decision='approved')::text,
       count(*) filter (where o.is_active=true and coalesce(r.decision,'pending')='pending')::text
from public.v3_studio_stage_outputs o
left join public.v3_studio_review_items r
  on r.stage_run_id=o.stage_run_id and r.media_id=o.media_id
where o.stage_run_id='{stage_id}'::uuid;
"""))
    output_total, output_active, output_approved, output_pending_review = (output[0] if output else ["0", "0", "0", "0"])
    print("\n===== FINAL SCENE OUTPUT =====")
    print(f"scene_outputs_total={output_total}")
    print(f"scene_outputs_active={output_active}")
    print(f"scene_outputs_approved={output_approved}")
    print(f"scene_outputs_pending_review={output_pending_review}")

    worker = inspect_worker()
    print("\n===== BACKGROUND FINALIZER WORKER =====")
    for key in (
        "present", "status", "running", "exit_code", "started_at", "finished_at", "error", "image",
        "DF_V3_SCENE_COORDINATOR_ENABLED", "DF_V3_SCENE_COORDINATOR_POLL_SECONDS",
        "DF_V3_SCENE_COORDINATOR_BATCH_SIZE", "DF_V3_SCENE_COORDINATOR_STATUS_CONCURRENCY",
        "parse_error",
    ):
        if key in worker:
            print(f"{key}={worker[key] or 'NONE'}")

    fan_in = total > 0 and succeeded == total and active == 0 and failed == 0
    worker_ok = worker.get("present") == "yes" and worker.get("running") == "yes"
    coordinator_enabled = str(worker.get("DF_V3_SCENE_COORDINATOR_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}
    phase = (a[6] or "").strip().lower()
    attempt_state = (a[2] or "").strip().lower()
    outputs_active_n = int(output_active or 0)

    print("\n===== FINALIZER CLASSIFICATION =====")
    if scene_state in {"awaiting_review", "approved"}:
        print("FINALIZER_CLASSIFICATION=COMPLETE")
        rc = 0
    elif not worker_ok:
        print("FINALIZER_CLASSIFICATION=RELEASE_BLOCKER_STITCH_WORKER_NOT_RUNNING")
        rc = 4
    elif not coordinator_enabled:
        print("FINALIZER_CLASSIFICATION=RELEASE_BLOCKER_COORDINATOR_DISABLED")
        rc = 4
    elif outputs_active_n > 0 and scene_state == "generating":
        print("FINALIZER_CLASSIFICATION=RELEASE_BLOCKER_STATE_TRANSITION_DRIFT")
        rc = 4
    elif fan_in and scene_state == "generating" and phase in {"scene_stitch", "pricing_commit"}:
        print(f"FINALIZER_CLASSIFICATION=FINALIZATION_IN_PROGRESS_{phase.upper()}")
        rc = 2
    elif fan_in and scene_state == "generating":
        print("FINALIZER_CLASSIFICATION=RELEASE_BLOCKER_FAN_IN_COMPLETE_BUT_NOT_ADVANCING")
        rc = 4
    elif attempt_state == "failed" or failed > 0:
        print("FINALIZER_CLASSIFICATION=SCENE_ATTEMPT_FAILED")
        rc = 4
    else:
        print("FINALIZER_CLASSIFICATION=VIDEO_CHILDREN_STILL_IN_PROGRESS")
        rc = 2

    print("FINALIZER_EXPECTATION=SERVER_SIDE_FAN_IN_THEN_STITCH_THEN_PARENT_PRICING_COMMIT_THEN_HUMAN_REVIEW")
    print("BROWSER_REQUIRED_FOR_FINALIZATION=NO")
    print("\n============================================================")
    print(" FINALIZER AUDIT COMPLETE")
    print("============================================================")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
