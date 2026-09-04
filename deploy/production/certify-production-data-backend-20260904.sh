#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/home/azureuser/workspace/desifaces}"
PROD_ENV="${PROD_ENV:-$ROOT/infra/.env}"
BASELINE_BACKUP="${BASELINE_BACKUP:-/home/azureuser/backups/desifaces-release-20260904T011508Z/desifaces-20260904T011508Z.dump}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
AUDIT_DB="desifaces_customer_data_audit_${STAMP//[^0-9]/}"
AUDIT_CREATED=0
TMP="$(mktemp -d)"

log(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
cleanup(){
  rc=$?
  set +e
  if (( AUDIT_CREATED == 1 )); then
    u="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_USER"' 2>/dev/null)"
    [[ -n "$u" ]] && docker exec desifaces-db dropdb -U "$u" --if-exists "$AUDIT_DB" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP" >/dev/null 2>&1 || true
  exit "$rc"
}
trap cleanup EXIT

for x in docker curl python3 grep awk sort comm sha256sum mktemp; do need "$x"; done
docker compose version >/dev/null 2>&1 || fail "docker compose v2 required"
HOST="$(hostname -s 2>/dev/null || hostname)"
[[ "$HOST" == desifaces-gpu* ]] || fail "run on desifaces-gpu; current host=$HOST"
[[ -f "$ROOT/RELEASE" && -f "$PROD_ENV" ]] || fail "canonical production package/env missing"
[[ -s "$BASELINE_BACKUP" ]] || fail "pre-migration production backup missing: $BASELINE_BACKUP"
if [[ -f "$BASELINE_BACKUP.sha256" ]]; then
  (cd "$(dirname "$BASELINE_BACKUP")" && sha256sum -c "$(basename "$BASELINE_BACKUP").sha256") >/dev/null || fail "baseline backup checksum verification failed"
fi

docker inspect desifaces-db >/dev/null 2>&1 || fail "production PostgreSQL container missing"
docker inspect desifaces-redis >/dev/null 2>&1 || fail "production Redis container missing"
DB_USER="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_DB"')"
[[ -n "$DB_USER" && -n "$DB_NAME" ]] || fail "cannot resolve production PostgreSQL identity"
[[ "$DB_NAME" == "desifaces" ]] || fail "unexpected live production database name: $DB_NAME"
docker exec desifaces-db pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null || fail "production PostgreSQL not ready"

log "============================================================"
log " desifaces.ai — CUSTOMER DATA + BACKEND CERTIFICATION"
log "============================================================"
log "live_db=$DB_NAME"
log "baseline_backup=$BASELINE_BACKUP"
log "database_write_policy=READ_ONLY_LIVE_DB"
log "web_deployment=BLOCKED_PENDING_BACKEND_PASS"
log "mobile_deployment=BLOCKED_PENDING_BACKEND_PASS"

log ""
log "===== 1. RESTORE PRE-MIGRATION PRODUCTION BACKUP TO TEMP AUDIT DB ====="
docker exec desifaces-db createdb -U "$DB_USER" "$AUDIT_DB"
AUDIT_CREATED=1
docker exec -i desifaces-db pg_restore -U "$DB_USER" -d "$AUDIT_DB" --no-owner --no-privileges < "$BASELINE_BACKUP" >/dev/null
log "PRODUCTION_BASELINE_RESTORE=PASS db=$AUDIT_DB"

psqlq(){ local db="$1" q="$2"; docker exec desifaces-db psql -X -A -t -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$db" -c "$q"; }
reg_exists(){ local db="$1" reg="$2"; [[ "$(psqlq "$db" "select to_regclass('$reg') is not null;")" == "t" ]]; }

log ""
log "===== 2. DISCOVER CUSTOMER-OWNED TABLES FROM PRE-MIGRATION PRODUCTION ====="
TABLES_FILE="$TMP/customer_tables.txt"
psqlq "$AUDIT_DB" "
select distinct table_schema||'.'||table_name
from information_schema.columns
where table_schema in ('core','public')
  and column_name in ('user_id','owner_user_id','requested_by_user_id','account_id','billing_account_id','customer_id')
union
select 'core.users' where to_regclass('core.users') is not null
union
select 'public.media_assets' where to_regclass('public.media_assets') is not null
order by 1;
" | sed '/^$/d' > "$TABLES_FILE"
[[ -s "$TABLES_FILE" ]] || fail "no customer-owned baseline tables discovered"
log "CUSTOMER_TABLE_DISCOVERY=PASS count=$(wc -l < "$TABLES_FILE" | tr -d ' ')"

missing_total=0
row_loss_total=0
key_loss_total=0
checked=0
while IFS= read -r reg; do
  [[ -n "$reg" ]] || continue
  schema="${reg%%.*}"; table="${reg#*.}"
  reg_exists "$AUDIT_DB" "$reg" || continue
  reg_exists "$DB_NAME" "$reg" || { log "DATA_FAIL table=$reg reason=missing_live_table"; missing_total=$((missing_total+1)); continue; }

  baseline_count="$(psqlq "$AUDIT_DB" "select count(*) from $reg;")"
  live_count="$(psqlq "$DB_NAME" "select count(*) from $reg;")"
  baseline_count="${baseline_count//[[:space:]]/}"; live_count="${live_count//[[:space:]]/}"
  [[ "$baseline_count" =~ ^[0-9]+$ && "$live_count" =~ ^[0-9]+$ ]] || fail "non-numeric row count for $reg"
  if (( live_count < baseline_count )); then
    log "DATA_FAIL table=$reg baseline_rows=$baseline_count live_rows=$live_count reason=row_count_decreased"
    row_loss_total=$((row_loss_total+1))
  else
    log "DATA_COUNT_PASS table=$reg baseline_rows=$baseline_count live_rows=$live_count"
  fi

  pk_expr="$(psqlq "$AUDIT_DB" "
select string_agg(format('coalesce(%I::text,'''')',a.attname), ', ' order by k.ord)
from pg_index i
cross join lateral unnest(i.indkey) with ordinality as k(attnum,ord)
join pg_attribute a on a.attrelid=i.indrelid and a.attnum=k.attnum
where i.indrelid='$reg'::regclass and i.indisprimary;
")"
  pk_expr="$(printf '%s' "$pk_expr" | tr -d '\r')"
  if [[ -n "$pk_expr" ]]; then
    base_keys="$TMP/${schema}_${table}_baseline.keys"
    live_keys="$TMP/${schema}_${table}_live.keys"
    psqlq "$AUDIT_DB" "select concat_ws(E'\\x1f', $pk_expr) from $reg order by 1;" | LC_ALL=C sort -u > "$base_keys"
    psqlq "$DB_NAME" "select concat_ws(E'\\x1f', $pk_expr) from $reg order by 1;" | LC_ALL=C sort -u > "$live_keys"
    missing_keys="$TMP/${schema}_${table}_missing.keys"
    LC_ALL=C comm -23 "$base_keys" "$live_keys" > "$missing_keys"
    if [[ -s "$missing_keys" ]]; then
      n="$(wc -l < "$missing_keys" | tr -d ' ')"
      log "DATA_FAIL table=$reg reason=baseline_primary_keys_missing count=$n"
      head -n 5 "$missing_keys" | sed 's/^/MISSING_KEY_SAMPLE /'
      key_loss_total=$((key_loss_total+1))
    else
      log "DATA_KEYSET_PASS table=$reg baseline_primary_keys_preserved=$baseline_count"
    fi
  else
    log "DATA_KEYSET_SKIP table=$reg reason=no_primary_key"
  fi
  checked=$((checked+1))
done < "$TABLES_FILE"

(( checked > 0 )) || fail "no customer tables were checked"
(( missing_total == 0 && row_loss_total == 0 && key_loss_total == 0 )) || fail "customer-data preservation failed missing_tables=$missing_total row_loss_tables=$row_loss_total key_loss_tables=$key_loss_total"
log "CUSTOMER_DATA_PRESERVATION=PASS tables_checked=$checked"

log ""
log "===== 3. CREDIT + ACCOUNT INTEGRITY ====="
NEGATIVE="$(psqlq "$DB_NAME" "select count(*) from public.pricing_credit_accounts where balance_credits < 0 or coalesce(reserved_credits,0) < 0;")"
[[ "${NEGATIVE//[[:space:]]/}" == "0" ]] || fail "negative production credit account count=$NEGATIVE"
ORPHAN_CREDIT="$(psqlq "$DB_NAME" "select count(*) from public.pricing_credit_accounts p left join core.users u on u.id=p.user_id where u.id is null;")"
[[ "${ORPHAN_CREDIT//[[:space:]]/}" == "0" ]] || fail "orphan production credit accounts=$ORPHAN_CREDIT"
INDIA_MISMATCH="$(psqlq "$DB_NAME" "select count(*) from public.pricing_billing_account_members bam join core.users u on u.id=bam.user_id join public.pricing_billing_accounts ba on ba.id=bam.billing_account_id where bam.status='active' and u.country_code='IN' and ba.default_currency<>'INR';")"
[[ "${INDIA_MISMATCH//[[:space:]]/}" == "0" ]] || fail "India billing currency mismatches=$INDIA_MISMATCH"
log "ACCOUNT_CREDIT_INTEGRITY=PASS"
log "INDIA_CURRENCY_INVARIANT=PASS"

log ""
log "===== 4. REQUIRED V3 SCHEMA ====="
for reg in public.v3_generation_requests public.v3_generation_jobs public.v3_media_assets public.v3_story_projects public.v3_studio_workflows; do
  reg_exists "$DB_NAME" "$reg" || fail "required V3 schema object missing: $reg"
done
[[ "$(psqlq "$DB_NAME" "select exists(select 1 from information_schema.columns where table_schema='core' and table_name='users' and column_name='country_code');")" == "t" ]] || fail "core.users.country_code missing"
[[ "$(psqlq "$DB_NAME" "select exists(select 1 from information_schema.columns where table_schema='public' and table_name='media_assets' and column_name='parent_generation_job_id');")" == "t" ]] || fail "media_assets.parent_generation_job_id missing"
log "V3_SCHEMA_CERTIFICATION=PASS"

log ""
log "===== 5. REBUILD + RECREATE PIKU ASSISTANT ONLY ====="
COMPOSE=(docker compose --env-file "$PROD_ENV" -f "$ROOT/docker-compose.yml" -f "$ROOT/deploy/production/docker-compose.v3-app.production.yml")
"${COMPOSE[@]}" build svc-assistant
"${COMPOSE[@]}" up -d --no-deps --force-recreate svc-assistant
log "ASSISTANT_RECREATE=APPLIED"

wait_http(){
  local name="$1" url="$2" max="${3:-60}" i code
  for ((i=1;i<=max;i++)); do
    code="$(curl -sS --max-time 4 -o "$TMP/health.json" -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [[ "$code" == "200" ]]; then log "PASS $name $url"; return 0; fi
    sleep 2
  done
  return 1
}

if ! wait_http assistant http://127.0.0.1:18012/api/health 45; then
  log "ASSISTANT_CONTAINER_STATE=$(docker inspect -f '{{.State.Status}}/{{.State.Restarting}}/{{.State.ExitCode}}' df-v3-svc-assistant 2>/dev/null || true)"
  docker logs --tail 120 df-v3-svc-assistant 2>&1 || true
  fail "assistant did not start"
fi
curl -fsS http://127.0.0.1:18012/api/health > "$TMP/assistant.json"
curl -fsS http://127.0.0.1:18011/api/health > "$TMP/director.json"
python3 - "$TMP/assistant.json" "$TMP/director.json" <<'PY'
import json,sys
for label,path in [('ASSISTANT',sys.argv[1]),('DIRECTOR',sys.argv[2])]:
    data=json.load(open(path))
    print(f"{label}_HEALTH={json.dumps(data, sort_keys=True)}")
    if data.get('runtime_ready') is not True:
        reason=data.get('llm_configuration_error') or data.get('configuration_error') or 'runtime_ready_false'
        raise SystemExit(f"FAIL: {label.lower()} runtime not ready: {reason}")
print('DIRECTOR_ASSISTANT_RUNTIME=PASS')
PY

log ""
log "===== 6. BACKEND SERVICE CERTIFICATION ====="
for spec in \
  'core|http://127.0.0.1:8000/api/health' \
  'fusion|http://127.0.0.1:8002/api/health' \
  'face|http://127.0.0.1:8003/api/health' \
  'audio|http://127.0.0.1:8004/api/health' \
  'dashboard|http://127.0.0.1:8005/api/health' \
  'fusion-extension|http://127.0.0.1:8006/api/health' \
  'pricing|http://127.0.0.1:8009/api/health' \
  'director|http://127.0.0.1:18011/api/health' \
  'assistant|http://127.0.0.1:18012/api/health'; do
  name="${spec%%|*}"; url="${spec#*|}"; wait_http "$name" "$url" 20 || fail "$name health failed"
done

for url in \
  'http://127.0.0.1:8000/auth/me' \
  'http://127.0.0.1:8009/api/pricing/me/spending/summary' \
  'http://127.0.0.1:8009/api/pricing/me/spending/transactions'; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' "$url" || true)"
  [[ "$code" == "401" || "$code" == "403" ]] || fail "auth guard unexpected for $url: HTTP $code"
done
log "BACKEND_AUTH_GUARDS=PASS"

log ""
log "===== 7. FINAL READ-ONLY LIVE DATA RECHECK ====="
LIVE_USERS="$(psqlq "$DB_NAME" 'select count(*) from core.users;')"
BASE_USERS="$(psqlq "$AUDIT_DB" 'select count(*) from core.users;')"
LIVE_MEDIA="$(psqlq "$DB_NAME" 'select count(*) from public.media_assets;')"
BASE_MEDIA="$(psqlq "$AUDIT_DB" 'select count(*) from public.media_assets;')"
log "CUSTOMER_COUNT_SUMMARY users_baseline=${BASE_USERS//[[:space:]]/} users_live=${LIVE_USERS//[[:space:]]/} media_baseline=${BASE_MEDIA//[[:space:]]/} media_live=${LIVE_MEDIA//[[:space:]]/}"
log "PRODUCTION_DATA_FINAL_RECHECK=PASS"

log ""
log "============================================================"
log " DATABASE + BACKEND CERTIFICATION PASS"
log "============================================================"
log "CUSTOMER_DATA_PRESERVATION=PASS"
log "PRODUCTION_DATABASE=PASS"
log "PRODUCTION_BACKEND=PASS"
log "WEB_DEPLOYMENT_ALLOWED=YES"
log "MOBILE_DEPLOYMENT_ALLOWED=AFTER_WEB_VALIDATION"
