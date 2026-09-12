#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DB_CONTAINER="${DF_V3_DB_CONTAINER:-desifaces-v3-db}"
DB_USER="${POSTGRES_USER:-desifaces_v3_admin}"
DB_NAME="${POSTGRES_DB:-desifaces_v3}"
LOG="${TMPDIR:-/tmp}/v3-fusion-extension-runtime-safe-$(date +%Y%m%dT%H%M%S).log"

exec > >(tee -a "$LOG") 2>&1

psqlq() {
  docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -At -P pager=off -c "$1"
}

running() {
  docker ps --format '{{.Names}}' | grep -qx "$1"
}

echo "V3_FUSION_EXTENSION_RUNTIME_SAFE=START"
echo "LOG_FILE=$LOG"

for c in \
  df-v3-svc-fusion-extension \
  df-v3-svc-fusion-extension-worker \
  df-v3-svc-fusion-extension-stitch-worker \
  df-v3-svc-fusion \
  df-v3-svc-fusion-worker
 do
  if running "$c"; then
    echo "CONTAINER $c=RUNNING"
  else
    echo "CONTAINER $c=NOT_RUNNING"
  fi
done

CANDIDATES="$(psqlq "
SELECT id::text
FROM longform_jobs
WHERE created_at >= now() - interval '12 hours'
  AND status NOT IN ('succeeded','failed','canceled','cancelled')
  AND COALESCE(tags->>'source','')='fusion_studio'
  AND COALESCE(tags->>'longform_profile','')='talking_video'
ORDER BY created_at DESC
LIMIT 3;
")"

COUNT="$(printf '%s\n' "$CANDIDATES" | sed '/^$/d' | wc -l | tr -d ' ')"
echo "ACTIVE_SINGLE_FACE_FUSION_CANDIDATES=$COUNT"

if [[ "$COUNT" != "1" ]]; then
  echo "AUTO_RECOVERY=REFUSED"
  echo "REASON=Expected exactly one active recent single-face Fusion Extension job"
  [[ -n "$CANDIDATES" ]] && printf 'CANDIDATE_JOB=%s\n' $CANDIDATES
  exit 3
fi

JOB_ID="$(printf '%s\n' "$CANDIDATES" | head -1 | tr -d '[:space:]')"
echo "JOB_ID=$JOB_ID"

summary() {
  local parent seg child
  parent="$(psqlq "
SELECT status || '|' || completed_segments || '/' || total_segments || '|pricing=' || COALESCE(tags->'pricing'->>'state','')
FROM longform_jobs WHERE id='$JOB_ID'::uuid;
")"
  seg="$(psqlq "
SELECT COALESCE(string_agg(status || ':' || count::text, ',' ORDER BY status),'none')
FROM (
  SELECT status, count(*)::int AS count
  FROM longform_segments
  WHERE job_id='$JOB_ID'::uuid
  GROUP BY status
) s;
")"
  child="$(psqlq "
SELECT COALESCE(string_agg(sj.status || ':' || count::text, ',' ORDER BY sj.status),'none')
FROM (
  SELECT j.status, count(*)::int AS count
  FROM studio_jobs j
  WHERE j.id IN (
    SELECT fusion_job_id FROM longform_segments
    WHERE job_id='$JOB_ID'::uuid AND fusion_job_id IS NOT NULL
  )
  GROUP BY j.status
) sj;
")"
  echo "STATE parent=$parent segments=$seg child_fusion=$child"
}

summary

MISSING=()
running df-v3-svc-fusion-extension-worker || MISSING+=(svc-fusion-extension-worker)
running df-v3-svc-fusion-extension-stitch-worker || MISSING+=(svc-fusion-extension-stitch-worker)

if ((${#MISSING[@]})); then
  echo "MISSING_WORKERS=${MISSING[*]}"
  echo "STARTING_MISSING_CANONICAL_WORKERS=YES"
  ./scripts/v3-compose.sh --profile v3-execution up -d "${MISSING[@]}" >/dev/null
  sleep 6
else
  echo "MISSING_WORKERS=NONE"
fi

if running df-v3-svc-fusion-extension-worker; then
  docker exec df-v3-svc-fusion-extension-worker sh -lc '
    echo "WORKER_ENABLED=${WORKER_ENABLED:-<unset>}";
    echo "SVC_FUSION_BASE_URL=${SVC_FUSION_BASE_URL:-<unset>}";
    if [ -n "${SVC_TO_SVC_BEARER:-}" ]; then echo "SVC_TO_SVC_BEARER_PRESENT=YES"; else echo "SVC_TO_SVC_BEARER_PRESENT=NO"; fi
  ' 2>/dev/null || true
fi

for i in $(seq 1 12); do
  PARENT_STATUS="$(psqlq "SELECT status FROM longform_jobs WHERE id='$JOB_ID'::uuid;")"
  SEG_STATE="$(psqlq "
SELECT COALESCE(string_agg(status || ':' || count::text, ',' ORDER BY status),'none')
FROM (
  SELECT status, count(*)::int AS count
  FROM longform_segments WHERE job_id='$JOB_ID'::uuid GROUP BY status
) s;
")"
  CHILD_COUNT="$(psqlq "
SELECT count(*)::int FROM studio_jobs
WHERE id IN (
  SELECT fusion_job_id FROM longform_segments
  WHERE job_id='$JOB_ID'::uuid AND fusion_job_id IS NOT NULL
);
")"
  echo "POLL_$i parent=$PARENT_STATUS segments=$SEG_STATE child_jobs=$CHILD_COUNT"
  if [[ "$PARENT_STATUS" =~ ^(succeeded|failed|canceled|cancelled)$ ]]; then
    break
  fi
  if [[ "$CHILD_COUNT" != "0" ]]; then
    break
  fi
  sleep 5
done

summary

CHILD_DETAIL="$(psqlq "
SELECT COALESCE(string_agg(
  j.id::text || ':' || j.status || ':provider=' || COALESCE(j.meta_json->>'provider','') || ':provider_job=' || COALESCE(j.meta_json->>'provider_job_id',''),
  ';'
),'none')
FROM studio_jobs j
WHERE j.id IN (
  SELECT fusion_job_id FROM longform_segments
  WHERE job_id='$JOB_ID'::uuid AND fusion_job_id IS NOT NULL
);
")"
echo "CHILD_DETAIL=$CHILD_DETAIL"

for c in df-v3-svc-fusion-extension-worker df-v3-svc-fusion-extension-stitch-worker; do
  if running "$c"; then
    docker logs --tail 250 "$c" >"${LOG}.${c}.log" 2>&1 || true
    HINTS="$(grep -Ei 'error|exception|traceback|failed|401|403|unauthor|forbidden|timeout' "${LOG}.${c}.log" | tail -n 6 | cut -c1-260 || true)"
    if [[ -n "$HINTS" ]]; then
      while IFS= read -r line; do echo "LOG_HINT $c $line"; done <<<"$HINTS"
    else
      echo "LOG_HINT $c NONE"
    fi
  fi
done

echo "V3_FUSION_EXTENSION_RUNTIME_SAFE=DONE"
