#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TARGET_BRANCH="feature/v3-multiperson-core-20260818"
STORY_WORKFLOW_ID="${STORY_WORKFLOW_ID:-a58bd7bf-b958-4bfe-9855-0d964d500b04}"
EXPECTED_FINAL_MEDIA_ID="${EXPECTED_FINAL_MEDIA_ID:-632685ec-c33b-43c8-94c4-be7bf09d43ac}"

fail() {
  echo "DEPLOY_FINAL_MEDIA=FAIL: $*" >&2
  exit 1
}

current_branch="$(git branch --show-current)"
[[ "$current_branch" == "$TARGET_BRANCH" ]] || fail "expected branch $TARGET_BRANCH, found $current_branch"

[[ -z "$(git status --porcelain --untracked-files=no)" ]] || fail "tracked worktree has local modifications; refusing deployment"

echo "=== 1. UPDATE EXACT FEATURE BRANCH ==="
git fetch origin "$TARGET_BRANCH"
git merge --ff-only "origin/$TARGET_BRANCH"
DEPLOY_SHA="$(git rev-parse HEAD)"
echo "DEPLOY_SHA=$DEPLOY_SHA"

[[ -f services/svc-dashboard/app/app/services/final_video_visibility.py ]] \
  || fail "canonical final-video helper missing"
grep -q 'm.id = w.final_media_id' services/svc-dashboard/app/app/services/final_video_visibility.py \
  || fail "final_media_id canonical relationship missing"
grep -q 'enrich_dashboard_home_with_v3_finals' services/svc-dashboard/app/app/api/routes/dashboard.py \
  || fail "dashboard home integration missing"
grep -q 'enrich_dashboard_library_with_v3_finals' services/svc-dashboard/app/app/api/routes/dashboard.py \
  || fail "dashboard library integration missing"

# The fix is service-scoped. It deliberately does not apply a DB migration,
# restart Fusion/Director workers, or mutate pricing/media rows.
echo "=== 2. BUILD SVC-DASHBOARD ONLY ==="
./scripts/v3-compose.sh build svc-dashboard

echo "=== 3. RECREATE SVC-DASHBOARD ONLY ==="
./scripts/v3-compose.sh up -d --no-deps --force-recreate svc-dashboard


echo "=== 4. HEALTH CHECK ==="
healthy=0
for _ in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:18005/api/health >/tmp/df-v3-dashboard-health.json 2>/dev/null; then
    healthy=1
    break
  fi
  sleep 2
done
[[ "$healthy" == "1" ]] || {
  ./scripts/v3-compose.sh ps svc-dashboard || true
  ./scripts/v3-compose.sh logs --tail=120 svc-dashboard || true
  fail "svc-dashboard did not become healthy"
}
cat /tmp/df-v3-dashboard-health.json
echo


echo "=== 5. RUNTIME CODE CERTIFICATION ==="
docker exec -i df-v3-svc-dashboard python - <<'PY'
from pathlib import Path

helper = Path('/app/app/services/final_video_visibility.py').read_text()
route = Path('/app/app/api/routes/dashboard.py').read_text()
required = [
    'm.id = w.final_media_id',
    "lower(coalesce(w.state, '')) = 'completed'",
    "lower(coalesce(w.current_stage, '')) = 'fusion'",
    "'render_kind', 'final'",
    "'output_role', 'final'",
    "'canonical_final', true",
    "'display_scope', 'final_outputs'",
]
for marker in required:
    assert marker in helper, marker
assert 'm.role =' not in helper
assert 'enrich_dashboard_home_with_v3_finals' in route
assert 'enrich_dashboard_library_with_v3_finals' in route
print('RUNTIME_CANONICAL_FINAL_CODE=PASS')
PY


echo "=== 6. EXACT STORY / FINAL-MEDIA DB PROOF ==="
DB_PROOF="$(docker exec \
  -e STORY_WORKFLOW_ID="$STORY_WORKFLOW_ID" \
  -e EXPECTED_FINAL_MEDIA_ID="$EXPECTED_FINAL_MEDIA_ID" \
  desifaces-v3-db sh -lc '
    set -eu
    psql -v ON_ERROR_STOP=1 -At \
      -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
      -v workflow_id="$STORY_WORKFLOW_ID" \
      -v final_media_id="$EXPECTED_FINAL_MEDIA_ID" <<'"'"'SQL'"'"'
select concat_ws('"'"'|'"'"',
  '"'"'workflow'"'"',
  w.workflow_id::text,
  lower(coalesce(w.state,'"'"''"'"')),
  lower(coalesce(w.current_stage,'"'"''"'"')),
  w.final_media_id::text,
  case when w.final_media_id::text = :'"'"'final_media_id'"'"' then '"'"'MATCH'"'"' else '"'"'MISMATCH'"'"' end
)
from public.v3_studio_workflows w
where w.workflow_id::text = :'"'"'workflow_id'"'"';

select concat_ws('"'"'|'"'"',
  '"'"'media'"'"',
  m.id::text,
  lower(coalesce(m.kind,'"'"''"'"')),
  lower(coalesce(m.content_type,'"'"''"'"')),
  lower(coalesce(m.lifecycle_state,'"'"'active'"'"')),
  case when m.deleted_at is null then '"'"'NOT_DELETED'"'"' else '"'"'DELETED'"'"' end,
  case when nullif(m.storage_ref,'"'"''"'"') is not null then '"'"'HAS_STORAGE_REF'"'"' else '"'"'NO_STORAGE_REF'"'"' end,
  coalesce(m.role,'"'"''"'"')
)
from public.media_assets m
where m.id::text = :'"'"'final_media_id'"'"';
SQL
  ' )"
printf '%s\n' "$DB_PROOF"

grep -q "workflow|$STORY_WORKFLOW_ID|completed|fusion|$EXPECTED_FINAL_MEDIA_ID|MATCH" <<<"$DB_PROOF" \
  || fail "workflow does not certify the expected canonical final_media_id"
grep -q "media|$EXPECTED_FINAL_MEDIA_ID|" <<<"$DB_PROOF" \
  || fail "canonical media row missing"
grep -q 'NOT_DELETED' <<<"$DB_PROOF" \
  || fail "canonical media is deleted"
grep -q 'HAS_STORAGE_REF' <<<"$DB_PROOF" \
  || fail "canonical media has no storage_ref"


echo "=== 7. DEPLOYMENT RESULT ==="
echo "DEPLOY_FINAL_MEDIA=PASS"
echo "DEPLOY_SHA=$DEPLOY_SHA"
echo "STORY_WORKFLOW_ID=$STORY_WORKFLOW_ID"
echo "FINAL_MEDIA_ID=$EXPECTED_FINAL_MEDIA_ID"
echo "NEXT_UI_CHECK=Dashboard Recent Videos and Saved Work Videos should each show the canonical final only; child scene/dialogue-turn clips must remain absent."
