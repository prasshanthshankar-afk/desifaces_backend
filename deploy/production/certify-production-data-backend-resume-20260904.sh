#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/home/azureuser/workspace/desifaces}"
PROD_ENV="${PROD_ENV:-$ROOT/infra/.env}"
BASELINE_BACKUP="${BASELINE_BACKUP:-/home/azureuser/backups/desifaces-release-20260904T011508Z/desifaces-20260904T011508Z.dump}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
AUDIT_DB="desifaces_customer_data_resume_${STAMP//[^0-9]/}"
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

for x in docker curl python3 sort comm sha256sum mktemp; do need "$x"; done
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
[[ "$DB_NAME" == "desifaces" && -n "$DB_USER" ]] || fail "unexpected production PostgreSQL identity"
docker exec desifaces-db pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null || fail "production PostgreSQL not ready"

log "============================================================"
log " desifaces.ai — DB PRESERVATION RESUME + BACKEND CERTIFICATION"
log "============================================================"
log "live_db=$DB_NAME"
log "database_write_policy=READ_ONLY_LIVE_DB"
log "live_db_restore=FORBIDDEN"
log "web_action=NONE"
log "mobile_action=NONE"

log ""
log "===== 1. RESTORE PRE-MIGRATION PRODUCTION BACKUP TO TEMP AUDIT DB ====="
docker exec desifaces-db createdb -U "$DB_USER" "$AUDIT_DB"
AUDIT_CREATED=1
docker exec -i desifaces-db pg_restore -U "$DB_USER" -d "$AUDIT_DB" --no-owner --no-privileges < "$BASELINE_BACKUP" >/dev/null
log "PRODUCTION_BASELINE_RESTORE=PASS db=$AUDIT_DB"

psqlq(){ local db="$1" q="$2"; docker exec desifaces-db psql -X -A -t -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$db" -c "$q"; }
reg_exists(){ local db="$1" reg="$2"; [[ "$(psqlq "$db" "select to_regclass('$reg') is not null;")" == "t" ]]; }

log ""
log "===== 2. RECONFIRM CUSTOMER DATA PRESERVATION ====="
TABLES="$TMP/customer_tables.txt"
psqlq "$AUDIT_DB" "
select distinct table_schema||'.'||table_name
from information_schema.columns
where table_schema in ('core','public')
  and column_name in ('user_id','owner_user_id','requested_by_user_id','account_id','billing_account_id','customer_id')
union select 'core.users' where to_regclass('core.users') is not null
union select 'public.media_assets' where to_regclass('public.media_assets') is not null
order by 1;
" | sed '/^$/d' > "$TABLES"
[[ -s "$TABLES" ]] || fail "no customer-owned tables discovered"
checked=0
while IFS= read -r reg; do
  [[ -n "$reg" ]] || continue
  reg_exists "$DB_NAME" "$reg" || fail "customer table missing in live DB: $reg"
  base="$(psqlq "$AUDIT_DB" "select count(*) from $reg;")"; live="$(psqlq "$DB_NAME" "select count(*) from $reg;")"
  base="${base//[[:space:]]/}"; live="${live//[[:space:]]/}"
  [[ "$base" =~ ^[0-9]+$ && "$live" =~ ^[0-9]+$ ]] || fail "invalid count for $reg"
  (( live >= base )) || fail "row_count_decreased table=$reg baseline=$base live=$live"

  pk="$(psqlq "$AUDIT_DB" "select string_agg(format('coalesce(%I::text,'''')',a.attname), ', ' order by k.ord) from pg_index i cross join lateral unnest(i.indkey) with ordinality as k(attnum,ord) join pg_attribute a on a.attrelid=i.indrelid and a.attnum=k.attnum where i.indrelid='$reg'::regclass and i.indisprimary;")"
  pk="$(printf '%s' "$pk" | tr -d '\r')"
  if [[ -n "$pk" ]]; then
    tag="${reg//./_}"
    psqlq "$AUDIT_DB" "select concat_ws(E'\\x1f', $pk) from $reg order by 1;" | LC_ALL=C sort -u > "$TMP/$tag.base"
    psqlq "$DB_NAME" "select concat_ws(E'\\x1f', $pk) from $reg order by 1;" | LC_ALL=C sort -u > "$TMP/$tag.live"
    LC_ALL=C comm -23 "$TMP/$tag.base" "$TMP/$tag.live" > "$TMP/$tag.missing"
    [[ ! -s "$TMP/$tag.missing" ]] || fail "baseline_primary_keys_missing table=$reg count=$(wc -l < "$TMP/$tag.missing" | tr -d ' ')"
  fi
  checked=$((checked+1))
done < "$TABLES"
(( checked >= 50 )) || fail "unexpectedly small customer table audit: $checked"
log "CUSTOMER_DATA_PRESERVATION=PASS tables_checked=$checked"

log ""
log "===== 3. CREDIT / ACCOUNT REGRESSION CHECK ====="
BASE_NEG="$(psqlq "$AUDIT_DB" "select count(*) from public.pricing_credit_accounts where balance_credits < 0 or coalesce(reserved_credits,0) < 0;")"
LIVE_NEG="$(psqlq "$DB_NAME" "select count(*) from public.pricing_credit_accounts where balance_credits < 0 or coalesce(reserved_credits,0) < 0;")"
BASE_ORPHAN="$(psqlq "$AUDIT_DB" "select count(*) from public.pricing_credit_accounts p left join core.users u on u.id=p.user_id where u.id is null;")"
LIVE_ORPHAN="$(psqlq "$DB_NAME" "select count(*) from public.pricing_credit_accounts p left join core.users u on u.id=p.user_id where u.id is null;")"
for v in BASE_NEG LIVE_NEG BASE_ORPHAN LIVE_ORPHAN; do eval "n=\${$v}"; n="${n//[[:space:]]/}"; [[ "$n" =~ ^[0-9]+$ ]] || fail "invalid integrity count $v"; eval "$v=$n"; done
(( LIVE_NEG <= BASE_NEG )) || fail "negative_credit_account_regression baseline=$BASE_NEG live=$LIVE_NEG"
(( LIVE_ORPHAN <= BASE_ORPHAN )) || fail "orphan_credit_account_regression baseline=$BASE_ORPHAN live=$LIVE_ORPHAN"
if (( LIVE_ORPHAN > 0 )); then log "LEGACY_INTEGRITY_PRESERVED type=orphan_credit_account baseline=$BASE_ORPHAN live=$LIVE_ORPHAN action=NO_MUTATION"; fi
if (( LIVE_NEG > 0 )); then log "LEGACY_INTEGRITY_PRESERVED type=negative_credit_account baseline=$BASE_NEG live=$LIVE_NEG action=NO_MUTATION"; fi
INDIA="$(psqlq "$DB_NAME" "select count(*) from public.pricing_billing_account_members bam join core.users u on u.id=bam.user_id join public.pricing_billing_accounts ba on ba.id=bam.billing_account_id where bam.status='active' and u.country_code='IN' and ba.default_currency<>'INR';")"
[[ "${INDIA//[[:space:]]/}" == "0" ]] || fail "India billing currency mismatch count=$INDIA"
log "ACCOUNT_CREDIT_REGRESSION=PASS"
log "INDIA_CURRENCY_INVARIANT=PASS"

log ""
log "===== 4. REQUIRED V3 SCHEMA ====="
for reg in public.v3_generation_requests public.v3_generation_jobs public.v3_media_assets public.v3_projects public.v3_studio_workflows; do
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

wait_http(){ local name="$1" url="$2" max="${3:-60}" i code; for ((i=1;i<=max;i++)); do code="$(curl -sS --max-time 4 -o "$TMP/health.json" -w '%{http_code}' "$url" 2>/dev/null || true)"; if [[ "$code" == 200 ]]; then log "PASS $name $url"; return 0; fi; sleep 2; done; return 1; }
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
for url in 'http://127.0.0.1:8000/auth/me' 'http://127.0.0.1:8009/api/pricing/me/spending/summary' 'http://127.0.0.1:8009/api/pricing/me/spending/transactions'; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' "$url" || true)"; [[ "$code" == 401 || "$code" == 403 ]] || fail "auth guard unexpected for $url: HTTP $code"
done
log "BACKEND_AUTH_GUARDS=PASS"

log ""
log "============================================================"
log " DATABASE + BACKEND RESUME CERTIFICATION PASS"
log "============================================================"
log "CUSTOMER_DATA_PRESERVATION=PASS"
log "PRODUCTION_DATABASE=PASS"
log "PRODUCTION_BACKEND=PASS"
log "NEXT_PHASE=WEB"
