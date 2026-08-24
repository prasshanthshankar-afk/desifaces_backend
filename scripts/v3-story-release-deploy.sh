#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

EXPECTED_BRANCH="feature/v3-multiperson-core-20260818"
MIGRATION="migrations/2026_08_24_v3_multiperson_pricing_packages.sql"
COMPOSE=(bash "$ROOT/scripts/v3-compose.sh")

fail() {
  echo "V3 STORY RELEASE: FAIL: $*" >&2
  exit 1
}

info() {
  echo
  echo "===== $* ====="
}

branch="$(git branch --show-current)"
[[ "$branch" == "$EXPECTED_BRANCH" ]] || fail "expected branch $EXPECTED_BRANCH, found $branch"
git diff --quiet || fail "working tree has unstaged changes"
git diff --cached --quiet || fail "working tree has staged changes"
[[ -f "$ROOT/infra/.env" ]] || fail "missing infra/.env"
[[ -f "$MIGRATION" ]] || fail "missing $MIGRATION"

POSTGRES_DB="$(awk -F= '$1=="POSTGRES_DB"{sub(/^[^=]*=/,""); print; exit}' "$ROOT/infra/.env")"
POSTGRES_USER="$(awk -F= '$1=="POSTGRES_USER"{sub(/^[^=]*=/,""); print; exit}' "$ROOT/infra/.env")"
[[ "$POSTGRES_DB" == "desifaces_v3" ]] || fail "refusing non-V3 database: $POSTGRES_DB"
[[ "$POSTGRES_USER" == "desifaces_v3_admin" ]] || fail "refusing non-V3 database user: $POSTGRES_USER"

info "1. STATIC BACKEND GATE"
python3 -m py_compile \
  services/svc-director/app/app/experience_compiler.py \
  services/svc-director/app/app/studio_preflight_routes.py \
  services/svc-director/app/app/audio_autoconfigure_routes.py \
  services/svc-director/app/app/audio_voice_routes.py \
  services/svc-director/app/app/audio_execution_runtime.py \
  services/svc-director/app/app/studio_e2e_routes.py \
  services/svc-director/app/app/fusion_execution.py \
  services/svc-director/app/app/fusion_execution_resilient.py \
  services/svc-director/app/app/fusion_execution_performance.py \
  services/svc-director/app/app/fusion_execution_runtime.py \
  services/svc-director/app/app/fusion_resilience_routes.py \
  services/svc-director/app/app/studio_routes_runtime.py

echo "PASS: Python production boundaries compile"

info "2. V3 RUNTIME IDENTITY"
"${COMPOSE[@]}" ps desifaces-db >/dev/null
"${COMPOSE[@]}" exec -T desifaces-db pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null
echo "PASS: isolated V3 PostgreSQL is ready"

info "3. APPLY DORMANT MULTI-PERSON PRICING CONFIG"
"${COMPOSE[@]}" exec -T desifaces-db \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$MIGRATION"

STANDARD_STRATEGY="$("${COMPOSE[@]}" exec -T desifaces-db psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select pricing_strategy from pricing_experience_packages where package_code='V3_MULTIPERSON_STANDARD' and is_active=true and is_default=true")"
PREMIUM_ACTIVE="$("${COMPOSE[@]}" exec -T desifaces-db psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select is_active::text from pricing_experience_packages where package_code='V3_MULTIPERSON_PREMIUM'")"
[[ "$STANDARD_STRATEGY" == "component_passthrough" ]] || fail "standard multi-person pricing is not component_passthrough"
[[ "$PREMIUM_ACTIVE" == "false" ]] || fail "premium package must remain dormant for this release"
echo "PASS: existing Face/Audio/Fusion pricing remains authoritative; premium package dormant"

info "4. VERIFY OWNER-SERVICE CONTRACTS BEFORE DIRECTOR CUTOVER"
need_fusion_rebuild=0
need_extension_rebuild=0
if ! curl -fsS http://127.0.0.1:18002/openapi.json | jq -e '.paths["/jobs/{job_id}/status-light"]' >/dev/null; then
  need_fusion_rebuild=1
fi
if ! curl -fsS http://127.0.0.1:18006/openapi.json | jq -e '.paths["/api/longform/v3/scene-stitch"]' >/dev/null; then
  need_extension_rebuild=1
fi
curl -fsS http://127.0.0.1:18009/openapi.json >/dev/null || fail "svc-pricing is not reachable on V3 port 18009"

if [[ "$need_fusion_rebuild" == "1" ]]; then
  echo "svc-fusion runtime is missing status-light; rebuilding only svc-fusion + worker"
  "${COMPOSE[@]}" build svc-fusion svc-fusion-worker
  "${COMPOSE[@]}" up -d --no-deps svc-fusion svc-fusion-worker
fi
if [[ "$need_extension_rebuild" == "1" ]]; then
  echo "svc-fusion-extension runtime is missing scene-stitch; rebuilding only svc-fusion-extension"
  "${COMPOSE[@]}" build svc-fusion-extension
  "${COMPOSE[@]}" up -d --no-deps svc-fusion-extension
fi

info "5. BUILD AND CUT OVER DIRECTOR ONLY"
"${COMPOSE[@]}" build svc-director svc-director-worker
"${COMPOSE[@]}" up -d --no-deps svc-director svc-director-worker

wait_http() {
  local url="$1"
  local label="$2"
  for _ in $(seq 1 40); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "PASS: $label"
      return 0
    fi
    sleep 2
  done
  fail "$label did not become ready: $url"
}

info "6. SERVICE READINESS"
wait_http http://127.0.0.1:18011/openapi.json "svc-director"
wait_http http://127.0.0.1:18002/openapi.json "svc-fusion"
wait_http http://127.0.0.1:18006/openapi.json "svc-fusion-extension"
wait_http http://127.0.0.1:18009/openapi.json "svc-pricing"

DIRECTOR_OPENAPI="$(mktemp)"
FUSION_OPENAPI="$(mktemp)"
EXT_OPENAPI="$(mktemp)"
trap 'rm -f "$DIRECTOR_OPENAPI" "$FUSION_OPENAPI" "$EXT_OPENAPI"' EXIT
curl -fsS http://127.0.0.1:18011/openapi.json > "$DIRECTOR_OPENAPI"
curl -fsS http://127.0.0.1:18002/openapi.json > "$FUSION_OPENAPI"
curl -fsS http://127.0.0.1:18006/openapi.json > "$EXT_OPENAPI"

jq -e '.paths["/api/director/studio-workflows/{workflow_id}/preflight"]' "$DIRECTOR_OPENAPI" >/dev/null || fail "Director preflight route missing"
jq -e '.paths["/api/director/studio-workflows/{workflow_id}/audio-autoconfigure"]' "$DIRECTOR_OPENAPI" >/dev/null || fail "Audio autoconfigure route missing"
jq -e '.paths["/api/director/studio-workflows/{workflow_id}/fusion-stages/{stage_run_id}/pricing-preview"]' "$DIRECTOR_OPENAPI" >/dev/null || fail "Fusion pricing route missing"
jq -e '.paths["/api/director/studio-workflows/{workflow_id}/fusion-stages/{stage_run_id}/retry-stitch"]' "$DIRECTOR_OPENAPI" >/dev/null || fail "Fusion stitch-only recovery route missing"
jq -e '.paths["/jobs/{job_id}/status-light"]' "$FUSION_OPENAPI" >/dev/null || fail "Fusion light-status route missing"
jq -e '.paths["/api/longform/v3/scene-stitch"]' "$EXT_OPENAPI" >/dev/null || fail "Fusion Extension scene-stitch route missing"

echo "PASS: V3 Story owner-service contracts are present"

info "7. RELEASE RESULT"
git log -1 --oneline
echo "PASS: V3 Story backend package deployed"
echo "No V2 service was rebuilt or restarted."
echo "Current pricing remains existing component pricing."
echo "Next gate: authenticated Fusion certification with scripts/v3-story-fusion-certify.sh"
