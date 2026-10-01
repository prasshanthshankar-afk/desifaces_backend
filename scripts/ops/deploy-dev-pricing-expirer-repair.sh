#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

TARGET_SHA="${TARGET_SHA:?TARGET_SHA required}"
SHORT="${TARGET_SHA:0:12}"
ROOT=""
for p in "$HOME/workspace/desifaces-v3" "$HOME/workspace/desifaces_backend" "$HOME/workspace/desifaces-runtime"; do
  if [[ -d "$p/.git" ]]; then ROOT="$p"; break; fi
done
[[ -n "$ROOT" ]] || { echo "FAIL: backend git repo not found"; exit 2; }

ENV_FILE=""
for p in "$HOME/workspace/desifaces-runtime/infra/.env" "$HOME/workspace/desifaces-v3/infra/.env" "$ROOT/infra/.env"; do
  if [[ -f "$p" && -s "$p" ]]; then ENV_FILE="$p"; break; fi
done
[[ -n "$ENV_FILE" ]] || { echo "FAIL: live DEV env file not found"; exit 2; }

PROJECT="$(docker inspect df-svc-pricing --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true)"
[[ -n "$PROJECT" ]] || PROJECT="desifaces-v3"

WT="/tmp/df-pricing-expirer-$SHORT"
rm -rf "$WT"
git -C "$ROOT" fetch origin "$TARGET_SHA" >/dev/null 2>&1 || true
git -C "$ROOT" worktree add --detach "$WT" "$TARGET_SHA" >/dev/null
cleanup(){ git -C "$ROOT" worktree remove --force "$WT" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# docker-compose.yml also references ./infra/.env relative to the isolated
# source tree. Link that path to the already-existing live DEV env instead of
# copying secrets into the worktree.
mkdir -p "$WT/infra"
ln -sfn "$ENV_FILE" "$WT/infra/.env"
[[ -f "$WT/infra/.env" ]] || { echo "FAIL: isolated live-env link unavailable"; exit 2; }
echo "ISOLATED_LIVE_ENV_LINK=PASS source=$ENV_FILE"

COMPOSE=(docker compose --project-directory "$WT" -p "$PROJECT" --env-file "$ENV_FILE" -f "$WT/docker-compose.yml")

echo "============================================================"
echo " desifaces DEV — PRICING EXPIRY REPAIR"
echo "============================================================"
echo "target_sha=$TARGET_SHA"
echo "pricing_api_restart=NONE"
echo "generation_services=UNTOUCHED"
echo "production=UNTOUCHED"

echo
echo "===== 1. SOURCE / COMPOSE GATE ====="
python3 - "$WT/services/svc-pricing/app/app/workers/reservation_expirer.py" <<'PY'
from pathlib import Path
import sys
src=Path(sys.argv[1]).read_text()
assert "from app.services.reservations.reservation_service import release" in src
assert 'reason="expired"' in src
assert "Process once immediately on startup" in src
print("CANONICAL_EXPIRY_SOURCE=PASS")
PY
"${COMPOSE[@]}" config -q
echo "COMPOSE_INTERPOLATION=PASS"

echo
echo "===== 2. PRE-REPAIR EXPIRED HOLDS ====="
DB_URL="$(docker exec df-svc-pricing sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DB_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DB_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i desifaces-db psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

"${PSQL[@]}" -P pager=off -c "
select id,user_id,reserved_credits,expires_at,now()-expires_at overdue
from public.pricing_credit_reservations
where status='reserved' and expires_at<now()
order by expires_at;
"
BEFORE="$("${PSQL[@]}" -Atq -c "select count(*) from public.pricing_credit_reservations where status='reserved' and expires_at<now();")"
echo "EXPIRED_RESERVED_BEFORE=$BEFORE"

echo
echo "===== 3. BUILD + START ONLY EXPIRY WORKER ====="
"${COMPOSE[@]}" build svc-pricing-reservation-expirer
"${COMPOSE[@]}" up -d --no-deps --force-recreate svc-pricing-reservation-expirer

for _ in $(seq 1 30); do
  state="$(docker inspect df-svc-pricing-reservation-expirer --format '{{.State.Running}}' 2>/dev/null || true)"
  [[ "$state" == "true" ]] && break
  sleep 1
done
[[ "$(docker inspect df-svc-pricing-reservation-expirer --format '{{.State.Running}}' 2>/dev/null || true)" == "true" ]] || {
  docker logs --tail 120 df-svc-pricing-reservation-expirer 2>&1 || true
  echo "FAIL: pricing reservation expirer did not start"
  exit 3
}
echo "PRICING_EXPIRER_RUNTIME=PASS"

echo
echo "===== 4. WAIT FOR CANONICAL RELEASE ====="
AFTER="$BEFORE"
for _ in $(seq 1 30); do
  AFTER="$("${PSQL[@]}" -Atq -c "select count(*) from public.pricing_credit_reservations where status='reserved' and expires_at<now();")"
  [[ "$AFTER" == "0" ]] && break
  sleep 1
done
echo "EXPIRED_RESERVED_AFTER=$AFTER"
[[ "$AFTER" == "0" ]] || {
  docker logs --tail 160 df-svc-pricing-reservation-expirer 2>&1 || true
  echo "FAIL: expired holds remain"
  exit 4
}

echo
echo "===== 5. ACCOUNT / LOT CONSISTENCY ====="
"${PSQL[@]}" -P pager=off -c "
select user_id,balance_credits,reserved_credits,(balance_credits-reserved_credits) available,updated_at
from public.pricing_credit_accounts
order by updated_at desc
limit 10;
"

MISMATCH="$("${PSQL[@]}" -Atq -c "
with lots as (
  select user_id,coalesce(sum(reserved_amount),0)::numeric reserved
  from public.pricing_credit_lots
  where status='active' and (expires_at is null or expires_at>now())
  group by user_id
)
select count(*)
from public.pricing_credit_accounts a
left join lots l on l.user_id=a.user_id
where a.reserved_credits::numeric <> coalesce(l.reserved,0);
")"
echo "ACCOUNT_LOT_RESERVED_MISMATCHES=$MISMATCH"
[[ "$MISMATCH" == "0" ]] || { echo "FAIL: account/lot reserved mismatch remains"; exit 5; }

echo
echo "===== 6. WORKER LOG ====="
docker logs --tail 80 df-svc-pricing-reservation-expirer 2>&1 || true

echo
echo "============================================================"
echo "PRICING_EXPIRED_HOLDS_REPAIRED=PASS"
echo "PRICING_EXPIRER_RUNNING=PASS"
echo "pricing_api_restart=NONE"
echo "generation_services=UNTOUCHED"
echo "production=UNTOUCHED"
echo "============================================================"
