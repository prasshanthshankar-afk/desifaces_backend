#!/usr/bin/env python3
from __future__ import annotations

import re
import socket
import subprocess
from datetime import datetime, timezone

EXPECTED_HOST = "desifaces-dev"
DB = "desifaces-v3-db"
SERVICES = (
    "df-v3-svc-director",
    "df-v3-svc-fusion-extension",
    "df-v3-svc-fusion",
)


def run(args, *, env=None, check=True):
    return subprocess.run(
        [str(x) for x in args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        check=check,
    )


def sanitize(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"(?i)authorization:\s*bearer\s+\S+", "Authorization: Bearer <redacted>", value)
    value = re.sub(r"(?i)(https?://[^\s?]+)\?[^\s]+", r"\1?<query-redacted>", value)
    value = re.sub(
        r'(?i)("?(?:password|secret|token|api[_-]?key|connection[_-]?string)"?\s*[:=]\s*)[^,\s}]+',
        r"\1<redacted>",
        value,
    )
    value = re.sub(r"\b[A-Za-z0-9_\-]{80,}\b", "<long-token-redacted>", value)
    return value


def psql(sql: str) -> str:
    cp = run([
        "docker", "exec", "-e", f"DF_READONLY_SQL={sql}", DB,
        "sh", "-lc",
        'psql -X -A -t -F "|" -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${POSTGRES_DB:-desifaces}" -c "$DF_READONLY_SQL"',
    ], check=False)
    if cp.returncode != 0:
        raise RuntimeError("read-only database query failed: " + sanitize(cp.stdout)[-1200:])
    return sanitize(cp.stdout).strip()


def logs(name: str, since: str = "45m") -> list[str]:
    cp = run(["docker", "logs", "--since", since, "--timestamps", name], check=False)
    text = sanitize(cp.stdout)
    interesting = re.compile(
        r"(?i)(fusion|scene-pricing|pricing|/dispatch|/reserve|/jobs|traceback|exception|error|\s4\d\d\s|\s5\d\d\s)"
    )
    rows = []
    for raw in text.splitlines():
        if interesting.search(raw):
            rows.append(raw[:900])
    return rows[-100:]


def main() -> int:
    host = socket.gethostname().split(".", 1)[0]
    print("============================================================")
    print(" desifaces DEV — FUSION DISPATCH READ-ONLY DIAGNOSTIC")
    print("============================================================")
    print(f"host={host}")
    print("environment=DEV_ONLY")
    print("runtime_mutations=NONE")
    print("database_writes=NONE")
    print("provider_calls=NONE")
    print("container_restarts=NONE")
    print("production_touch=NONE")
    print("secret_values_output=FORBIDDEN")
    print(f"captured_at={datetime.now(timezone.utc).isoformat()}")

    if host != EXPECTED_HOST:
        raise SystemExit(f"FAIL: run only on {EXPECTED_HOST}; current={host}")

    for name in (DB, *SERVICES):
        cp = run(["docker", "inspect", name, "--format", "{{.State.Status}}"], check=False)
        if cp.returncode != 0:
            raise SystemExit(f"FAIL: required dev container unavailable: {name}")
        print(f"container={name} status={cp.stdout.strip()}")

    print("\n===== 1. LATEST FUSION STAGE DURABLE STATE =====")
    stage_sql = r"""
select s.workflow_id::text,
       s.stage_run_id::text,
       s.state::text,
       coalesce(s.metadata_json->>'aspect_ratio',''),
       coalesce(s.metadata_json->'fusion_parent_pricing'->>'state',''),
       case when coalesce(s.metadata_json->'fusion_parent_pricing'->>'quote_id','')<>'' then 'yes' else 'no' end,
       case when coalesce(s.metadata_json->'fusion_parent_pricing'->>'preview_fingerprint','')<>'' then 'yes' else 'no' end,
       to_char(s.updated_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"')
from public.v3_studio_stage_runs s
where s.stage_type='fusion'
order by s.updated_at desc
limit 5;
"""
    stage_rows = psql(stage_sql)
    print("columns=workflow_id|stage_run_id|state|aspect_ratio|parent_pricing_state|quote_present|fingerprint_present|updated_at")
    print(stage_rows or "NO_FUSION_STAGES")

    print("\n===== 2. LATEST FUSION ATTEMPTS =====")
    attempt_sql = r"""
select a.stage_run_id::text,
       a.attempt_no::text,
       a.state::text,
       coalesce(a.error_code,''),
       replace(left(coalesce(a.error_message,''),700),E'\n',' '),
       case when coalesce(a.provider_job_ref,'')<>'' then 'yes' else 'no' end,
       to_char(a.created_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"')
from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.stage_type='fusion'
order by a.created_at desc
limit 8;
"""
    attempts = psql(attempt_sql)
    print("columns=stage_run_id|attempt_no|state|error_code|error_message|provider_job_present|created_at")
    print(attempts or "NO_FUSION_ATTEMPTS")

    print("\n===== 3. RECENT DIRECTOR FUSION/DISPATCH LOGS =====")
    director = logs("df-v3-svc-director")
    print("\n".join(director) if director else "NO_RELEVANT_DIRECTOR_LOG_LINES")

    print("\n===== 4. RECENT FUSION EXTENSION PRICING LOGS =====")
    extension = logs("df-v3-svc-fusion-extension")
    print("\n".join(extension) if extension else "NO_RELEVANT_EXTENSION_LOG_LINES")

    print("\n===== 5. RECENT FUSION CHILD JOB LOGS =====")
    fusion = logs("df-v3-svc-fusion")
    print("\n".join(fusion) if fusion else "NO_RELEVANT_FUSION_LOG_LINES")

    all_director = "\n".join(director)
    all_extension = "\n".join(extension)
    all_fusion = "\n".join(fusion)

    dispatch_seen = bool(re.search(r"(?i)fusion-stages/.+/dispatch", all_director))
    reserve_seen = bool(re.search(r"(?i)scene-pricing/reserve", all_extension))
    child_create_seen = bool(re.search(r"(?i)(POST\s+[^\s]*?/jobs(?:\s|\?|$)|\"POST /jobs)", all_fusion))

    latest = stage_rows.splitlines()[0].split("|") if stage_rows else []
    latest_state = latest[2] if len(latest) > 2 else "unknown"
    parent_state = latest[4] if len(latest) > 4 else "unknown"
    quote_present = latest[5] if len(latest) > 5 else "unknown"
    fingerprint_present = latest[6] if len(latest) > 6 else "unknown"

    print("\n===== 6. CLASSIFICATION =====")
    print(f"DIRECTOR_DISPATCH_ACCESS_SEEN={'YES' if dispatch_seen else 'NO'}")
    print(f"FUSION_EXTENSION_RESERVE_ACCESS_SEEN={'YES' if reserve_seen else 'NO'}")
    print(f"FUSION_CHILD_CREATE_ACCESS_SEEN={'YES' if child_create_seen else 'NO'}")
    print(f"LATEST_STAGE_STATE={latest_state}")
    print(f"LATEST_PARENT_PRICING_STATE={parent_state}")
    print(f"LATEST_PARENT_QUOTE_PRESENT={quote_present}")
    print(f"LATEST_PARENT_FINGERPRINT_PRESENT={fingerprint_present}")

    if latest_state == "generating" or attempts:
        classification = "DISPATCH_REACHED_DURABLE_EXECUTION"
    elif parent_state == "quoted" and quote_present == "yes" and fingerprint_present == "yes" and not dispatch_seen:
        classification = "CLIENT_SIDE_OR_GATEWAY_BEFORE_DIRECTOR_DISPATCH"
    elif dispatch_seen and not reserve_seen:
        classification = "DIRECTOR_DISPATCH_REJECTED_BEFORE_PARENT_RESERVE"
    elif reserve_seen and not child_create_seen:
        classification = "PARENT_RESERVE_OR_PRE_CHILD_DISPATCH_FAILURE"
    elif child_create_seen:
        classification = "CHILD_DISPATCH_STARTED_CHECK_DURABLE_ATTEMPT"
    else:
        classification = "INSUFFICIENT_LOG_SIGNAL_USE_DURABLE_STATE_AND_ERRORS_ABOVE"
    print(f"CLASSIFICATION={classification}")

    print("\n============================================================")
    print(" FUSION DISPATCH DIAGNOSTIC COMPLETE")
    print("============================================================")
    print("RUNTIME_MUTATIONS=NONE")
    print("PRODUCTION_TOUCH=NONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
