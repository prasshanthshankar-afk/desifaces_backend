#!/usr/bin/env python3
from __future__ import annotations

import re
import socket
import subprocess
from datetime import datetime, timezone

EXPECTED_HOST = "desifaces-dev"
DB = "desifaces-v3-db"
STAGE_ID = "7d13c846-a9a4-468d-9bac-6295ead91667"
WORKFLOW_ID = "f9e0581d-c5cb-40de-955e-cf8da4296c3b"


def run(args, *, check=True):
    return subprocess.run([str(x) for x in args], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check)


def sanitize(text: str) -> str:
    s = str(text or "")
    s = re.sub(r"(?i)authorization:\s*bearer\s+\S+", "Authorization: Bearer <redacted>", s)
    s = re.sub(r"(?i)(https?://[^\s?]+)\?[^\s]+", r"\1?<query-redacted>", s)
    s = re.sub(r"\b[A-Za-z0-9_\-]{80,}\b", "<long-token-redacted>", s)
    return s


def psql(sql: str) -> str:
    cp = run([
        "docker", "exec", "-e", f"DF_READONLY_SQL={sql}", DB,
        "sh", "-lc",
        'psql -X -A -t -F "|" -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${POSTGRES_DB:-desifaces}" -c "$DF_READONLY_SQL"',
    ], check=False)
    if cp.returncode != 0:
        raise RuntimeError("read-only DB query failed: " + sanitize(cp.stdout)[-1200:])
    return sanitize(cp.stdout).strip()


def container_state(name: str) -> str:
    cp = run(["docker", "inspect", name, "--format", "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.RestartCount}}|{{.State.StartedAt}}"], check=False)
    return cp.stdout.strip() if cp.returncode == 0 else "missing"


def filtered_logs(name: str, since: str = "12h") -> list[str]:
    cp = run(["docker", "logs", "--since", since, "--timestamps", name], check=False)
    rows: list[str] = []
    for raw in sanitize(cp.stdout).splitlines():
        low = raw.lower()
        if STAGE_ID.lower() in low or "background_coordinator" in low or "scene_stitch" in low or "fusion_child" in low or "error" in low or "exception" in low or "failed" in low:
            rows.append(raw[:1000])
    return rows[-80:]


def main() -> int:
    host = socket.gethostname().split(".", 1)[0]
    print("============================================================")
    print(" desifaces DEV — MULTI-PERSON SCENE STATUS")
    print("============================================================")
    print(f"host={host}")
    print("environment=DEV_ONLY")
    print("runtime_mutations=NONE")
    print("database_writes=NONE")
    print("provider_calls=NONE")
    print("container_restarts=NONE")
    print("production_touch=NONE")
    print(f"workflow_id={WORKFLOW_ID}")
    print(f"stage_run_id={STAGE_ID}")
    print(f"captured_at={datetime.now(timezone.utc).isoformat()}")
    if host != EXPECTED_HOST:
        raise SystemExit(f"FAIL: run only on {EXPECTED_HOST}; current={host}")

    print("\n===== 1. RUNTIME COMPONENTS =====")
    for name in (
        "df-v3-svc-director",
        "df-v3-svc-fusion",
        "df-v3-svc-fusion-worker",
        "df-v3-svc-fusion-extension",
        "df-v3-svc-fusion-extension-stitch-worker",
    ):
        print(f"{name}={container_state(name)}")

    print("\n===== 2. DURABLE SCENE STAGE =====")
    stage_sql = f"""
select s.workflow_id::text,
       s.stage_run_id::text,
       s.state::text,
       coalesce(s.metadata_json->>'aspect_ratio',''),
       coalesce(s.metadata_json->'fusion_parent_pricing'->>'state',''),
       coalesce(s.metadata_json->'fusion_parent_pricing'->>'reservation_id','') <> '' as reservation_present,
       extract(epoch from (now()-s.updated_at))::int as age_seconds,
       to_char(s.updated_at at time zone 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')
from public.v3_studio_stage_runs s
where s.stage_run_id='{STAGE_ID}'::uuid and s.workflow_id='{WORKFLOW_ID}'::uuid;
"""
    stage = psql(stage_sql)
    print("columns=workflow_id|stage_run_id|state|aspect_ratio|parent_pricing_state|reservation_present|age_seconds|updated_at")
    print(stage or "STAGE_NOT_FOUND")

    print("\n===== 3. LATEST DIRECTOR ATTEMPT =====")
    attempt_sql = f"""
select a.attempt_no::text,
       a.state::text,
       coalesce(a.error_code,''),
       replace(left(coalesce(a.error_message,''),900),E'\\n',' '),
       coalesce(a.metadata_json->'background_coordinator'->>'phase',''),
       coalesce(a.metadata_json->'background_coordinator'->>'next_phase',''),
       jsonb_array_length(coalesce(a.metadata_json->'children','[]'::jsonb))::text,
       extract(epoch from (now()-a.updated_at))::int as age_seconds,
       to_char(a.updated_at at time zone 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')
from public.v3_studio_stage_attempts a
where a.stage_run_id='{STAGE_ID}'::uuid
order by a.attempt_no desc
limit 1;
"""
    attempt = psql(attempt_sql)
    print("columns=attempt_no|state|error_code|error_message|coordinator_phase|next_phase|recorded_children|age_seconds|updated_at")
    print(attempt or "NO_ATTEMPT")

    print("\n===== 4. RECORDED CHILDREN IN DIRECTOR ATTEMPT =====")
    children_sql = f"""
with latest as (
  select metadata_json from public.v3_studio_stage_attempts
  where stage_run_id='{STAGE_ID}'::uuid order by attempt_no desc limit 1
), c as (
  select value as child from latest, jsonb_array_elements(coalesce(metadata_json->'children','[]'::jsonb))
)
select coalesce(child->>'sequence_no',''),
       coalesce(child->>'dialogue_turn_id',''),
       coalesce(child->>'fusion_job_id',''),
       coalesce(child->>'status',''),
       case when coalesce(child->>'video_url','')<>'' then 'yes' else 'no' end,
       coalesce(child->>'reused_from_prior_attempt','false'),
       coalesce(child->>'reconciled_orphan','false')
from c
order by nullif(child->>'sequence_no','')::int nulls last, child->>'dialogue_turn_id';
"""
    children = psql(children_sql)
    print("columns=seq|dialogue_turn_id|fusion_job_id|status|video_present|reused|orphan_reconciled")
    print(children or "NO_RECORDED_CHILDREN")

    print("\n===== 5. ALL FUSION CHILD JOBS FOR THIS SCENE =====")
    child_jobs_sql = f"""
select j.id::text,
       j.status::text,
       coalesce(j.error_code,''),
       replace(left(coalesce(j.error_message,''),500),E'\\n',' '),
       coalesce(j.meta_json->'light_status'->>'provider_status',''),
       coalesce(j.meta_json->'light_status'->>'progress_pct',''),
       case when coalesce(j.meta_json->'light_status'->>'primary_video_url','')<>'' then 'yes' else 'no' end,
       extract(epoch from (now()-j.updated_at))::int as age_seconds,
       to_char(j.updated_at at time zone 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')
from public.studio_jobs j
where j.studio_type='fusion'
  and (
    j.payload_json #>> '{{provider_options,billing_context,billing_parent_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{provider_options,billing_context,parent_longform_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{provider_options,billing_context,parent_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{provider_options,pricing_context,billing_parent_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{provider_options,pricing_context,parent_longform_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{provider_options,pricing_context,parent_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{tags,billing_context,billing_parent_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{tags,billing_context,parent_longform_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{tags,billing_context,parent_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{tags,pricing_context,billing_parent_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{tags,pricing_context,parent_longform_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{tags,pricing_context,parent_job_id}}' = '{STAGE_ID}'
  )
order by j.updated_at desc;
"""
    child_jobs = psql(child_jobs_sql)
    print("columns=job_id|status|error_code|error_message|provider_status|progress_pct|video_present|age_seconds|updated_at")
    print(child_jobs or "NO_CHILD_JOBS")

    print("\n===== 6. CHILD STATUS SUMMARY =====")
    summary_sql = f"""
select j.status::text,count(*)::text
from public.studio_jobs j
where j.studio_type='fusion'
  and (
    j.payload_json #>> '{{provider_options,billing_context,billing_parent_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{provider_options,billing_context,parent_longform_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{provider_options,billing_context,parent_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{tags,billing_context,billing_parent_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{tags,billing_context,parent_longform_job_id}}' = '{STAGE_ID}'
    or j.payload_json #>> '{{tags,billing_context,parent_job_id}}' = '{STAGE_ID}'
  )
group by j.status order by j.status;
"""
    summary = psql(summary_sql)
    print(summary or "NO_CHILD_STATUS")

    print("\n===== 7. STITCH / BACKGROUND WORKER LOGS =====")
    stitch_logs = filtered_logs("df-v3-svc-fusion-extension-stitch-worker")
    print("\n".join(stitch_logs) if stitch_logs else "NO_RELEVANT_STITCH_WORKER_LOGS")

    print("\n===== 8. DIRECTOR / FUSION ERROR LOGS =====")
    for name in ("df-v3-svc-director", "df-v3-svc-fusion", "df-v3-svc-fusion-worker"):
        rows = filtered_logs(name)
        print(f"--- {name} ---")
        print("\n".join(rows[-30:]) if rows else "NO_RELEVANT_LINES")

    stage_parts = stage.split("|") if stage else []
    attempt_parts = attempt.split("|") if attempt else []
    stage_state = stage_parts[2] if len(stage_parts) > 2 else "unknown"
    stage_age = int(stage_parts[6]) if len(stage_parts) > 6 and stage_parts[6].isdigit() else -1
    attempt_state = attempt_parts[1] if len(attempt_parts) > 1 else "unknown"
    error_code = attempt_parts[2] if len(attempt_parts) > 2 else ""
    phase = attempt_parts[4] if len(attempt_parts) > 4 else ""

    print("\n===== 9. CLASSIFICATION =====")
    print(f"STAGE_STATE={stage_state}")
    print(f"ATTEMPT_STATE={attempt_state}")
    print(f"COORDINATOR_PHASE={phase or 'none'}")
    print(f"ATTEMPT_ERROR_CODE={error_code or 'none'}")
    print(f"STAGE_AGE_SECONDS={stage_age}")
    if stage_state in {"awaiting_review", "approved"}:
        classification = "SCENE_COMPLETE_REVIEWABLE"
    elif stage_state == "failed" or attempt_state == "failed":
        classification = "SCENE_FAILED"
    elif stage_state == "generating":
        if stage_age >= 1800:
            classification = "SCENE_GENERATING_STALE_OVER_30_MINUTES"
        elif phase:
            classification = f"SCENE_GENERATING_PHASE_{phase.upper()}"
        else:
            classification = "SCENE_GENERATING_CHILDREN_OR_PROVIDER"
    else:
        classification = f"SCENE_STATE_{stage_state.upper()}"
    print(f"CLASSIFICATION={classification}")

    print("\n============================================================")
    print(" SCENE STATUS PROBE COMPLETE")
    print("============================================================")
    print("RUNTIME_MUTATIONS=NONE")
    print("PRODUCTION_TOUCH=NONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
