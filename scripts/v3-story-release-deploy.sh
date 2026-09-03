#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

EXPECTED_BRANCH="feature/v3-multiperson-core-20260818"
PACKAGE_MIGRATION="migrations/2026_08_24_v3_multiperson_pricing_packages.sql"
PARENT_PRICING_MIGRATION="migrations/2026_08_24_v3_fusion_parent_pricing_integrity.sql"
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
[[ -f "$PACKAGE_MIGRATION" ]] || fail "missing $PACKAGE_MIGRATION"
[[ -f "$PARENT_PRICING_MIGRATION" ]] || fail "missing $PARENT_PRICING_MIGRATION"

POSTGRES_DB="$(awk -F= '$1=="POSTGRES_DB"{sub(/^[^=]*=/,""); print; exit}' "$ROOT/infra/.env")"
POSTGRES_USER="$(awk -F= '$1=="POSTGRES_USER"{sub(/^[^=]*=/,""); print; exit}' "$ROOT/infra/.env")"
[[ "$POSTGRES_DB" == "desifaces_v3" ]] || fail "refusing non-V3 database: $POSTGRES_DB"
[[ "$POSTGRES_USER" == "desifaces_v3_admin" ]] || fail "refusing non-V3 database user: $POSTGRES_USER"

info "1. STATIC BACKEND + PRICING GATE"
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
  services/svc-director/app/app/fusion_execution_parent_pricing.py \
  services/svc-director/app/app/fusion_execution_runtime.py \
  services/svc-director/app/app/fusion_resilience_routes.py \
  services/svc-director/app/app/studio_routes_runtime.py \
  services/svc-fusion-extension/app/app/api/routes/v3_scene_pricing.py \
  services/svc-fusion-extension/app/app/api/routes/v3_scene_stitch.py \
  services/svc-fusion-extension/app/app/main.py

git diff --check
echo "PASS: Python production boundaries compile and Git diff is clean"

info "2. V3 RUNTIME IDENTITY"
"${COMPOSE[@]}" ps desifaces-db >/dev/null
"${COMPOSE[@]}" exec -T desifaces-db pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null
echo "PASS: isolated V3 PostgreSQL is ready"

info "3. SNAPSHOT FUSION ECONOMICS BEFORE MIGRATION"
FUSION_BEFORE="$("${COMPOSE[@]}" exec -T desifaces-db psql -At -F '|' -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select unit,default_unit_credits,coalesce(provider_hint,'') from public.pricing_skus where code='FUSION_TALK_MIN'")"
[[ -n "$FUSION_BEFORE" ]] || fail "FUSION_TALK_MIN is missing"
IFS='|' read -r UNIT_BEFORE CREDITS_BEFORE PROVIDER_BEFORE <<< "$FUSION_BEFORE"
[[ "$UNIT_BEFORE" == "minute" ]] || fail "existing FUSION_TALK_MIN unit is not minute: $UNIT_BEFORE"
[[ "$CREDITS_BEFORE" =~ ^[0-9]+$ && "$CREDITS_BEFORE" -gt 0 ]] || fail "existing Fusion credit rate is invalid: $CREDITS_BEFORE"
echo "Fusion economics before: unit=$UNIT_BEFORE credits_per_unit=$CREDITS_BEFORE provider_hint=${PROVIDER_BEFORE:-<blank>}"

info "4. APPLY V3 PRICING CONFIG + INTEGRITY MIGRATION"
"${COMPOSE[@]}" exec -T desifaces-db \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$PACKAGE_MIGRATION"
"${COMPOSE[@]}" exec -T desifaces-db \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$PARENT_PRICING_MIGRATION"

STANDARD_STRATEGY="$("${COMPOSE[@]}" exec -T desifaces-db psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select pricing_strategy from pricing_experience_packages where package_code='V3_MULTIPERSON_STANDARD' and is_active=true and is_default=true")"
PREMIUM_ACTIVE="$("${COMPOSE[@]}" exec -T desifaces-db psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select is_active::text from pricing_experience_packages where package_code='V3_MULTIPERSON_PREMIUM'")"
[[ "$STANDARD_STRATEGY" == "component_passthrough" ]] || fail "standard multi-person pricing is not component_passthrough"
[[ "$PREMIUM_ACTIVE" == "false" ]] || fail "premium package must remain dormant for this release"

FUSION_AFTER="$("${COMPOSE[@]}" exec -T desifaces-db psql -At -F '|' -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select unit,default_unit_credits,coalesce(provider_hint,'') from public.pricing_skus where code='FUSION_TALK_MIN'")"
IFS='|' read -r UNIT_AFTER CREDITS_AFTER PROVIDER_AFTER <<< "$FUSION_AFTER"
[[ "$UNIT_AFTER" == "$UNIT_BEFORE" ]] || fail "Fusion unit changed during migration: $UNIT_BEFORE -> $UNIT_AFTER"
[[ "$CREDITS_AFTER" == "$CREDITS_BEFORE" ]] || fail "Fusion credit rate changed during migration: $CREDITS_BEFORE -> $CREDITS_AFTER"
[[ -z "$PROVIDER_AFTER" ]] || fail "Fusion pricing SKU is not provider-neutral: $PROVIDER_AFTER"

TRIGGER_COUNT="$("${COMPOSE[@]}" exec -T desifaces-db psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select count(*) from pg_trigger where tgname='trg_v3_guard_fusion_review_pricing_commit' and not tgisinternal")"
[[ "$TRIGGER_COUNT" == "1" ]] || fail "Fusion committed-pricing review guard trigger is missing"

echo "PASS: launch pricing economics unchanged; provider metadata neutral; premium dormant; HITL DB guard installed"

info "5. BUILD ONLY CHANGED V3 OWNER SERVICES"
"${COMPOSE[@]}" build svc-fusion-extension svc-director svc-director-worker
"${COMPOSE[@]}" up -d --no-deps svc-fusion-extension svc-director svc-director-worker

# svc-fusion itself was not modified in this cut; its already-deployed internal-child
# suppression contract is verified below through OpenAPI + authenticated certification.

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
jq -e '.paths["/api/director/studio-workflows/{workflow_id}/fusion-stages/{stage_run_id}/pricing-preview"]' "$DIRECTOR_OPENAPI" >/dev/null || fail "Director Fusion parent pricing route missing"
jq -e '.paths["/api/director/studio-workflows/{workflow_id}/fusion-stages/{stage_run_id}/dispatch"]' "$DIRECTOR_OPENAPI" >/dev/null || fail "Director Fusion dispatch route missing"
jq -e '.paths["/api/director/studio-workflows/{workflow_id}/fusion-stages/{stage_run_id}/retry-stitch"]' "$DIRECTOR_OPENAPI" >/dev/null || fail "Fusion stitch-only recovery route missing"
jq -e '.paths["/jobs/pricing/preview"]' "$FUSION_OPENAPI" >/dev/null || fail "svc-fusion pricing preview route missing"
jq -e '.paths["/jobs/{job_id}/status-light"]' "$FUSION_OPENAPI" >/dev/null || fail "Fusion light-status route missing"

for path in \
  '/api/longform/v3/scene-pricing/preview' \
  '/api/longform/v3/scene-pricing/reserve' \
  '/api/longform/v3/scene-pricing/commit' \
  '/api/longform/v3/scene-pricing/release' \
  '/api/longform/v3/scene-stitch'; do
  jq -e --arg path "$path" '.paths[$path]' "$EXT_OPENAPI" >/dev/null \
    || fail "Fusion Extension contract missing: $path"
done

echo "PASS: one-parent pricing + zero-priced-child runtime contracts are present"

info "7. RELEASE RESULT"
git log -1 --oneline
echo "PASS: V3 Fusion parent-pricing package deployed"
echo "No V2 service was rebuilt or restarted."
echo "FUSION_TALK_MIN economics remained exactly $CREDITS_AFTER credits per $UNIT_AFTER."
echo "Multi-person premium package remains dormant."
echo "Billable Fusion remains CLOSED until scripts/v3-fusion-pricing-certify.sh passes."
