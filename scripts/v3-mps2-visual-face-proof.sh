#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

EXPECTED_BRANCH="feature/v3-multiperson-core-20260818"
CURRENT_BRANCH="$(git branch --show-current)"
if [ "$CURRENT_BRANCH" != "$EXPECTED_BRANCH" ]; then
  echo "MPS2_VISUAL_FAIL=wrong_branch:$CURRENT_BRANCH"
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "MPS2_VISUAL_FAIL=working_tree_not_clean"
  git status --short
  exit 1
fi

if [ -z "${DF_DIRECTOR_LLM_MODEL:-}" ] && [ -f infra/.env ]; then
  FILE_MODEL="$(awk -F= '$1 == "DF_DIRECTOR_LLM_MODEL" {sub(/^[^=]*=/, ""); print}' infra/.env | tail -n 1 | tr -d '\r' | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
  if [ -n "$FILE_MODEL" ]; then export DF_DIRECTOR_LLM_MODEL="$FILE_MODEL"; fi
fi
export DF_DIRECTOR_LLM_MODEL="${DF_DIRECTOR_LLM_MODEL:-gpt-5.6}"

# Canonical V3 E2E media actor. Passwords/credentials are deliberately never
# stored in this repository; the proof resolves the user from V3 DB and uses a
# short-lived locally signed test JWT.
DF_V3_E2E_TEST_USER_EMAIL="user_apple_iap_test1@desifaces.ai"
export DF_V3_E2E_TEST_USER_EMAIL

# Fast source regression gate. svc-director and svc-face both expose a top-level
# Python package named `app`; test them in isolated interpreter processes so one
# service package cannot shadow the other.
docker run --rm -v "$PWD:/repo" -w /repo desifaces-v3-svc-fusion \
  sh -lc '
    pip install -q pytest langgraph==1.2.9 httpx &&
    PYTHONPATH=/repo:/repo/services/shared:/repo/services/shared/python:/repo/services/svc-director/app \
      python -m pytest -q \
        test/test_v3_participant_face_bridge.py \
        test/test_v3_participant_ref_normalization.py \
        test/test_v3_studio_hitl_workflow.py \
        test/test_v3_face_execution_policy.py &&
    PYTHONPATH=/repo:/repo/services/shared:/repo/services/shared/python:/repo/services/svc-face/app \
      python -m pytest -q test/test_v3_face_creator_config_hydration.py
  '
echo "MPS2_VISUAL_TARGETED_UNIT=PASS"

DB_NAME="$(docker exec desifaces-v3-db sh -lc 'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select current_database()"')"
if [ "$DB_NAME" != "desifaces_v3" ]; then
  echo "MPS2_VISUAL_FAIL=wrong_database:$DB_NAME"
  exit 1
fi
echo "MPS2_VISUAL_DB_TARGET=PASS"

# The per-output attempt ledger is idempotent schema. Apply it explicitly so a
# Face slot can be retried/regenerated without depending on manual migration state.
docker exec -i desifaces-v3-db sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < migrations/2026_08_19_v3_studio_stage_attempts.sql >/dev/null
echo "MPS2_STAGE_ATTEMPT_MIGRATION=PASS"

FACE_WORKER_WAS_RUNNING=0
if docker ps --format '{{.Names}}' | grep -Fxq 'df-v3-svc-face-worker'; then
  FACE_WORKER_WAS_RUNNING=1
fi

cleanup_worker() {
  if [ "$FACE_WORKER_WAS_RUNNING" = "0" ]; then
    ./scripts/v3-compose.sh --profile v3-execution stop svc-face-worker >/dev/null 2>&1 || true
  fi
}
trap cleanup_worker EXIT

# Start only the services/runners required by this proof. Other execution workers
# remain off even though the profile is enabled because services are named.
COMPOSE_PARALLEL_LIMIT=1 ./scripts/v3-compose.sh \
  --profile v3-orchestration \
  --profile v3-execution \
  up -d --build \
  svc-core svc-pricing svc-face svc-face-worker svc-director svc-director-worker

for item in \
  "director:127.0.0.1:18011/api/health" \
  "face:127.0.0.1:18003/api/health" \
  "pricing:127.0.0.1:18009/api/health" \
  "core:127.0.0.1:18000/api/health"
do
  name="${item%%:*}"
  url="${item#*:}"
  ok=""
  for _ in $(seq 1 60); do
    if curl -fsS "$url" >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  if [ -z "$ok" ]; then
    echo "MPS2_VISUAL_FAIL=${name}_health_timeout"
    exit 1
  fi
done

echo "MPS2_VISUAL_SERVICE_HEALTH=PASS"

if ! docker ps --format '{{.Names}}' | grep -Fxq 'df-v3-svc-director-worker'; then
  echo "MPS2_VISUAL_FAIL=director_worker_not_running"
  exit 1
fi
if ! docker ps --format '{{.Names}}' | grep -Fxq 'df-v3-svc-face-worker'; then
  echo "MPS2_VISUAL_FAIL=face_worker_not_running"
  exit 1
fi

UNEXPECTED_WORKERS="$(docker ps --format '{{.Names}}' | grep -E '^df-v3-.*(worker|scheduler)$' | grep -v -E '^df-v3-svc-(director|face)-worker$' || true)"
if [ -n "$UNEXPECTED_WORKERS" ]; then
  echo "MPS2_VISUAL_FAIL=unexpected_execution_workers:$UNEXPECTED_WORKERS"
  exit 1
fi
echo "MPS2_VISUAL_EXECUTION_SCOPE=PASS:director_worker+face_worker_only"

FACE_MODEL="$(docker exec df-v3-svc-face python -c 'import os; print((os.getenv("OPENAI_IMAGE_MODEL_T2I") or "gpt-image-2").strip())')"
if [ "$FACE_MODEL" != "gpt-image-2" ]; then
  echo "MPS2_VISUAL_FAIL=face_model_not_gpt-image-2:$FACE_MODEL"
  exit 1
fi
echo "MPS2_VISUAL_FACE_MODEL=PASS:$FACE_MODEL"

# V3 storage namespaces are infrastructure, not per-job state. Create the
# configured private Face input/output containers idempotently, fail closed
# unless both are explicitly V3-isolated names, then prove write/read/delete.
docker exec df-v3-svc-face python -m app.tools.v3_mps2_prepare_visual_storage

# Fail before pricing/provider execution if any creator masterdata row used by
# Face falls back from its declared runtime model to an untyped dict.
docker exec df-v3-svc-face python -m app.tools.v3_mps2_audit_creator_config_hydration

# Pricing must remain real. Do not inject fake balance or bypass reservations.
# Reuse the certified C6 period-aware integrity repair, which can only reconcile
# already-persisted active subscription periods, then require >=10 spendable
# credits on the canonical E2E actor before two 5-credit Face jobs are allowed.
docker exec \
  -e DF_V3_E2E_TEST_USER_EMAIL="$DF_V3_E2E_TEST_USER_EMAIL" \
  df-v3-svc-pricing \
  python -m app.tools.v3_mps2_prepare_visual_pricing

# Interactive unless explicit test-only approval flags are supplied.
set +e
docker exec -i \
  -e DF_V3_E2E_TEST_USER_EMAIL="$DF_V3_E2E_TEST_USER_EMAIL" \
  -e MPS2_FACE_MODEL_RESOLVED="$FACE_MODEL" \
  -e MPS2_DIRECTOR_PLAN_APPROVED="${MPS2_DIRECTOR_PLAN_APPROVED:-}" \
  -e MPS2_FACE_GENERATION_APPROVED="${MPS2_FACE_GENERATION_APPROVED:-}" \
  df-v3-svc-director \
  python -m app.tools.v3_mps2_visual_face_proof_v3
RC=$?
set -e

if [ "$RC" -ne 0 ]; then
  echo "MPS2_VISUAL_DIRECTOR_LOGS_BEGIN"
  docker logs --tail 160 df-v3-svc-director 2>&1 | sed -E 's/(sk-[A-Za-z0-9_-]{12})[A-Za-z0-9_-]*/\1...REDACTED/g' || true
  echo "MPS2_VISUAL_DIRECTOR_LOGS_END"
  echo "MPS2_VISUAL_FACE_LOGS_BEGIN"
  docker logs --tail 220 df-v3-svc-face-worker 2>&1 | sed -E 's/(sk-[A-Za-z0-9_-]{12})[A-Za-z0-9_-]*/\1...REDACTED/g' || true
  echo "MPS2_VISUAL_FACE_LOGS_END"
  exit "$RC"
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="$PWD/artifacts/v3-mps2-visual-proof/$STAMP"
mkdir -p "$OUT_DIR"
docker cp df-v3-svc-director:/tmp/v3_mps2_visual_proof/. "$OUT_DIR/"
ln -sfn "$OUT_DIR" "$PWD/artifacts/v3-mps2-visual-proof/latest"

echo "MPS2_VISUAL_ARTIFACT_COPY=PASS"
echo "MPS2_VISUAL_OUTPUT_DIR=$OUT_DIR"
echo "MPS2_VISUAL_INTENT=$OUT_DIR/01_director_intent.json"
echo "MPS2_VISUAL_GENERATIVE_PLAN=$OUT_DIR/02_director_generative_plan.json"
echo "MPS2_VISUAL_FACE_ANANYA=$OUT_DIR/07_ananya_face.png"
echo "MPS2_VISUAL_FACE_RAVI=$OUT_DIR/07_ravi_face.png"
echo "MPS2_VISUAL_MANIFEST=$OUT_DIR/manifest.json"
echo "V3_MPS2_VISUAL_PROOF_RUNNER=PASS"
