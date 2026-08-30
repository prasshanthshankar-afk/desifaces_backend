#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE="$ROOT/scripts/v3-compose.sh"
MIGRATION="$ROOT/migrations/2026_08_30_multi_person_premium_pricing.sql"
DB_CONTAINER="desifaces-v3-db"
PRICING_CONTAINER="df-v3-svc-pricing"
AUDIO_CONTAINER="df-v3-svc-audio"
AUDIO_WORKER_CONTAINER="df-v3-svc-audio-worker"
FACE_CONTAINER="df-v3-svc-face"
DIRECTOR_CONTAINER="df-v3-svc-director"
DIRECTOR_WORKER_CONTAINER="df-v3-svc-director-worker"

banner() {
  printf '\n============================================================\n %s\n============================================================\n' "$1"
}

die() {
  echo "FAIL: $*" >&2
  exit 1
}

wait_http() {
  local name="$1" url="$2"
  local i
  for i in $(seq 1 90); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      echo "PASS: $name healthy -> $url"
      return 0
    fi
    sleep 2
  done
  die "$name did not become healthy: $url"
}

wait_container_running() {
  local name="$1"
  local i state
  for i in $(seq 1 60); do
    state="$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)"
    if [ "$state" = "true" ]; then
      echo "PASS: $name is running"
      return 0
    fi
    sleep 2
  done
  die "$name is not running"
}

[ -x "$COMPOSE" ] || die "missing executable $COMPOSE"
[ -f "$MIGRATION" ] || die "missing migration $MIGRATION"
command -v docker >/dev/null 2>&1 || die "docker is required"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v git >/dev/null 2>&1 || die "git is required"

banner "V3 FACE + AUDIO HOTFIX CERTIFICATION"
echo "repo=$ROOT"
echo "branch=$(git branch --show-current)"
echo "HEAD=$(git rev-parse HEAD)"

if ! git diff --quiet -- .; then
  die "tracked working tree has unstaged modifications; certify a committed tree only"
fi
if ! git diff --cached --quiet -- .; then
  die "tracked working tree has staged modifications; certify a committed tree only"
fi

banner "1. VERIFY V3 DATABASE / PRICING RUNTIME"
"$COMPOSE" up -d desifaces-db desifaces-redis svc-pricing

for _ in $(seq 1 60); do
  if docker exec "$DB_CONTAINER" sh -lc 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

db_identity="$(docker exec "$DB_CONTAINER" sh -lc 'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select current_database() || chr(124) || current_user"')"
[ "$db_identity" = "desifaces_v3|desifaces_v3_admin" ] || die "refusing non-V3 database identity: $db_identity"
echo "PASS: V3 database identity=$db_identity"
wait_http "svc-pricing" "http://127.0.0.1:18009/api/health"

banner "2. CAPTURE CURRENT IN-FLIGHT AUDIO JOBS BEFORE RESTART"
# Capture only recent queued/running Audio jobs. These are existing jobs; the
# script never manufactures a replacement job or a second Director stage attempt.
mapfile -t INFLIGHT_AUDIO_JOBS < <(
  docker exec "$DB_CONTAINER" sh -lc \
    'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
      select id::text
      from public.studio_jobs
      where studio_type = '\''audio'\''
        and status in ('\''queued'\'', '\''running'\'')
        and updated_at >= now() - interval '\''2 hours'\''
      order by updated_at desc
      limit 32
    "' | sed '/^[[:space:]]*$/d'
)

if ((${#INFLIGHT_AUDIO_JOBS[@]})); then
  printf 'existing_inflight_audio_job=%s\n' "${INFLIGHT_AUDIO_JOBS[@]}"
else
  echo "INFO: no recent queued/running Audio job found before restart"
fi

banner "3. APPLY IDEMPOTENT MULTI-PERSON PREMIUM CATALOG"
docker exec -i "$DB_CONTAINER" sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$MIGRATION"
echo "PASS: multi-person pricing migration applied"

banner "4. CERTIFY FACE PREMIUM CATALOG RATE"
docker exec -i "$DB_CONTAINER" sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
DO $$
DECLARE
  source_credits bigint;
  premium_credits bigint;
  variant_lines integer;
BEGIN
  SELECT default_unit_credits INTO source_credits
  FROM pricing_skus WHERE code='FACE_EDIT_PREMIUM_RUN';

  SELECT default_unit_credits INTO premium_credits
  FROM pricing_skus WHERE code='FACE_MULTI_PERSON' AND status='active';

  SELECT count(*) INTO variant_lines
  FROM pricing_variant_lines
  WHERE variant_code='FACE_MULTI_PERSON'
    AND sku_code='FACE_MULTI_PERSON'
    AND qty_mode='param'
    AND qty_param='num_edits';

  IF source_credits IS NULL OR premium_credits IS NULL THEN
    RAISE EXCEPTION 'Face premium source/target SKU missing';
  END IF;
  IF premium_credits <= source_credits THEN
    RAISE EXCEPTION 'FACE_MULTI_PERSON must be premium: source=%, premium=%', source_credits, premium_credits;
  END IF;
  IF variant_lines <> 1 THEN
    RAISE EXCEPTION 'FACE_MULTI_PERSON must have exactly one num_edits variant line, got %', variant_lines;
  END IF;

  RAISE NOTICE 'FACE_RATE source_default_credits=% premium_default_credits=%', source_credits, premium_credits;
END $$;

SELECT code, default_unit_credits
FROM pricing_skus
WHERE code IN ('FACE_EDIT_PREMIUM_RUN','FACE_MULTI_PERSON')
ORDER BY code;
SQL

echo "PASS: FACE_MULTI_PERSON is catalog-premium over its source Face SKU"

banner "5. BUILD ONLY THE AFFECTED RUNTIMES"
"$COMPOSE" --profile v3-execution --profile v3-orchestration build \
  svc-face \
  svc-audio \
  svc-audio-worker \
  svc-director \
  svc-director-worker

echo "PASS: affected images built"

banner "6. RECREATE ONLY THE AFFECTED RUNTIMES"
"$COMPOSE" --profile v3-execution --profile v3-orchestration up -d --no-deps --force-recreate \
  svc-face \
  svc-audio \
  svc-audio-worker \
  svc-director \
  svc-director-worker

wait_http "svc-face" "http://127.0.0.1:18003/api/health"
wait_http "svc-audio" "http://127.0.0.1:18004/api/health"
wait_http "svc-director" "http://127.0.0.1:18011/api/health"
wait_http "svc-pricing" "http://127.0.0.1:18009/api/health"
wait_container_running "$AUDIO_WORKER_CONTAINER"
wait_container_running "$DIRECTOR_WORKER_CONTAINER"

banner "7. VERIFY DEPLOYED FACE PREMIUM ROUTING"
docker exec -i "$FACE_CONTAINER" python - <<'PY'
from app.domain.models import CreatorPlatformRequest
from desifaces_shared.pricing.multi_person import FACE_MULTI_PERSON, participant_count, select_multi_person_pricing

req = CreatorPlatformRequest.model_validate({
    "mode": "text-to-image",
    "subject_composition_code": "single_person",
    "num_variants": 1,
    "pricing_context": {
        "multi_person": True,
        "pricing_scope": "director_participant_identity",
    },
})
raw = req.model_dump(mode="json")
assert raw["subject_composition_code"] == "single_person"
assert raw["pricing_context"]["multi_person"] is True
assert participant_count(raw) >= 2

for count in (2, 5):
    selection = select_multi_person_pricing(
        studio="face",
        participant_count_value=count,
        natural_units=1,
    )
    assert selection is not None
    assert selection.sku_code == FACE_MULTI_PERSON
    assert selection.billable_units == 1
    assert selection.variant_params == {"num_edits": "1"}
    assert selection.metadata["participant_scaling"] == "per_character_natural_usage"

print("face_policy=PASS sku=FACE_MULTI_PERSON billable_units_per_character=1")
PY

docker exec -i "$DIRECTOR_CONTAINER" python - <<'PY'
from app.face_pricing_context_runtime import _director_face_pricing_context

raw = {
    "subject_composition_code": "single_person",
    "num_variants": 1,
    "user_prompt": "identity portrait",
}
out = _director_face_pricing_context(raw)
assert out["subject_composition_code"] == "single_person"
assert out["pricing_context"]["multi_person"] is True
assert out["pricing_context"]["pricing_scope"] == "director_participant_identity"
assert "pricing_context" not in raw
print("director_face_context=PASS")
PY

echo "PASS: deployed Director -> Face premium pricing context is active"

banner "8. VERIFY LIVE FACE QUOTE IS PREMIUM"
docker exec -i "$PRICING_CONTAINER" python - <<'PY'
import asyncio
import os
from uuid import UUID

import asyncpg
from app.services.engine.pricing_engine import quote_variant

TEST_USER = UUID("88888888-8888-4888-8888-888888888888")

async def quote(conn, variant, params):
    return await quote_variant(
        conn,
        user_id=TEST_USER,
        variant_code=variant,
        params=params,
        channel="web",
        country_code="",
        currency="USD",
        billing_mode="bill",
    )

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(
            """
            insert into pricing_user_entitlements(user_id, tier_code, effective_from, metadata_json)
            values($1, 'free', now(), '{}'::jsonb)
            on conflict (user_id) do update
              set tier_code=excluded.tier_code,
                  effective_from=excluded.effective_from
            """,
            TEST_USER,
        )

        premium = await quote(
            conn,
            "FACE_MULTI_PERSON",
            {
                "num_edits": 1,
                "multi_person": True,
                "participant_count": 2,
                "pricing_scope": "director_participant_identity",
            },
        )
        baseline = await quote(conn, "FACE_EDIT_PREMIUM_BATCH", {"num_edits": 1})

        assert premium.total_credits > baseline.total_credits > 0
        assert premium.lines and premium.lines[0].qty == 1
        print(
            "face_quote=PASS",
            {
                "FACE_MULTI_PERSON": premium.total_credits,
                "baseline": baseline.total_credits,
                "qty": premium.lines[0].qty,
            },
        )
    finally:
        await tx.rollback()
        await conn.close()

asyncio.run(main())
PY

echo "PASS: live pricing engine returns a premium Face quote for one Director character"

banner "9. FORCE ONE SAFE STALE-AUDIO RECOVERY PASS"
# Worker startup already performs this recovery. Run it once explicitly as well
# so certification is deterministic even when the worker starts before an old row
# crosses the lease threshold. The same job ID and reservation are retained.
docker exec -i "$AUDIO_WORKER_CONTAINER" python - <<'PY'
import asyncio

from app.db import get_pool
from app.repos.tts_jobs_repo import TTSJobsRepo

async def main():
    pool = await get_pool()
    repo = TTSJobsRepo(pool, studio_type="audio")
    recovered = await repo.requeue_stale_running_jobs(
        stale_after_seconds=60,
        max_attempts=3,
        limit=64,
    )
    print("explicit_recovered_audio_jobs=", recovered)

asyncio.run(main())
PY

echo "PASS: stale Audio recovery routine executed"

banner "10. WAIT FOR PRE-EXISTING IN-FIGHT AUDIO TO LEAVE STUCK STATE"
if ((${#INFLIGHT_AUDIO_JOBS[@]})); then
  for job_id in "${INFLIGHT_AUDIO_JOBS[@]}"; do
    terminal=""
    for _ in $(seq 1 120); do
      status_value="$(docker exec "$DB_CONTAINER" sh -lc \
        "psql -At -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -c \"select status from public.studio_jobs where id='${job_id}'::uuid and studio_type='audio'\"" \
        | tr -d '[:space:]')"

      case "$status_value" in
        succeeded)
          echo "PASS: existing Audio job $job_id -> succeeded"
          terminal="succeeded"
          break
          ;;
        failed|blocked|cancelled|canceled)
          echo "FAIL: existing Audio job $job_id reached terminal state=$status_value" >&2
          docker exec "$DB_CONTAINER" sh -lc \
            "psql -x -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -c \"select id,status,error_code,error_message,attempt_count,updated_at from public.studio_jobs where id='${job_id}'::uuid\"" || true
          exit 1
          ;;
        queued|running|pricing_pending|reserved|"")
          sleep 1
          ;;
        *)
          echo "INFO: Audio job $job_id current status=$status_value"
          sleep 1
          ;;
      esac
    done

    [ "$terminal" = "succeeded" ] || {
      docker exec "$DB_CONTAINER" sh -lc \
        "psql -x -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -c \"select id,status,error_code,error_message,attempt_count,updated_at from public.studio_jobs where id='${job_id}'::uuid\"" || true
      die "existing Audio job $job_id did not complete within 120 seconds after recovery"
    }
  done
else
  echo "INFO: no pre-existing recent Audio job required recovery wait"
fi

banner "11. FINAL STATUS"
echo "PASS: V3 Face premium routing + Audio stale-job recovery certified"
echo "HEAD=$(git rev-parse HEAD)"
echo "Affected runtime only: svc-face, svc-audio, svc-audio-worker, svc-director, svc-director-worker"
echo "No new Face generation was invoked by this script."
echo "Existing queued/stale Audio jobs may execute using their original job IDs and existing pricing reservations."
