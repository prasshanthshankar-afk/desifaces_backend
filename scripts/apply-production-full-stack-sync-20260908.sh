#!/usr/bin/env bash
set -Eeuo pipefail

DEV_HOST="${DEV_HOST:-desifaces-dev}"
PROD_HOST="${PROD_HOST:-desifaces-gpu}"
DEV_ROOT="/home/azureuser/workspace/desifaces-v3"
PROD_ROOT="/home/azureuser/workspace/desifaces"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN="$(mktemp -d /tmp/desifaces-full-sync-${STAMP}.XXXXXX)"
trap 'rm -rf "$RUN" >/dev/null 2>&1 || true' EXIT

fail(){ echo "FAIL: $*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
for x in ssh scp awk grep sort python3 gh diff curl; do need "$x"; done
[[ "$(uname -s)" == "Darwin" ]] || fail "run from the Mac release environment"

echo "============================================================"
echo " desifaces.ai — PRODUCTION FULL STACK SYNC"
echo "============================================================"
echo "CUSTOMER_DATA_ACTION=NONE"
echo "PRICING_DATA_ACTION=NONE"
echo "DB_RESTORE_ACTION=NONE"
echo "DB_MASTERDATA_MODE=ADDITIVE_UPSERT_ONLY"
echo "REDIS_ACTION=NONE"
echo "WEB_SOURCE_ACTION=NONE"
echo "MOBILE_BUILD_ACTION=NONE"
echo "run_dir=$RUN"

for host in "$DEV_HOST" "$PROD_HOST"; do
  ssh -o BatchMode=yes -o ConnectTimeout=12 "$host" 'hostname -s' >/dev/null || fail "cannot SSH to $host"
done

# -----------------------------------------------------------------------------
# 1. Export V3 masterdata from dev. Only Azure-backed provider capability rows
#    are promoted; no dev provider secrets are copied to production.
# -----------------------------------------------------------------------------
echo
echo "===== 1. EXPORT V3 MASTERDATA FROM DEV ====="
ssh "$DEV_HOST" "DEV_ROOT='$DEV_ROOT' bash -s" > "$RUN/dev-meta.env" <<'REMOTE'
set -Eeuo pipefail
ROOT="$DEV_ROOT"
[[ -f "$ROOT/infra/.env" ]] || exit 2
DB_C=""
for c in desifaces-v3-db desifaces-db; do docker inspect "$c" >/dev/null 2>&1 && { DB_C="$c"; break; }; done
[[ -n "$DB_C" ]] || DB_C="$(docker ps --format '{{.Names}}' | grep -E '(^|-)db$|desifaces.*db' | head -1 || true)"
[[ -n "$DB_C" ]] || exit 2
DB_USER="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_DB"')"
printf 'DB_C=%q\nDB_USER=%q\nDB_NAME=%q\n' "$DB_C" "$DB_USER" "$DB_NAME"
python3 - "$ROOT/infra/.env" <<'PY'
from pathlib import Path
import shlex,sys
want={'AUDIO_OUTPUT_CONTAINER','AZURE_AUDIO_CONTAINER','FACE_INPUT_CONTAINER','FACE_OUTPUT_CONTAINER','OPENAI_IMAGE_MODERATION'}
vals={}
for raw in Path(sys.argv[1]).read_text(encoding='utf-8',errors='replace').splitlines():
    s=raw.strip()
    if not s or s.startswith('#') or '=' not in s: continue
    k,v=s.split('=',1); vals[k.strip()]=v.strip().strip('"').strip("'")
for k in sorted(want):
    if vals.get(k,''):
        print(f'SAFE_{k}={shlex.quote(vals[k])}')
PY
REMOTE
# shellcheck disable=SC1090
source "$RUN/dev-meta.env"
[[ -n "${DB_C:-}" && -n "${DB_USER:-}" && -n "${DB_NAME:-}" ]] || fail "dev DB metadata unresolved"
mkdir -p "$RUN/md"

export_table(){
  local table="$1" where="$2" file="$RUN/md/${table}.csv"
  ssh "$DEV_HOST" "DBC='$DB_C' DBU='$DB_USER' DBN='$DB_NAME' T='$table' W='$where' bash -s" > "$file" <<'REMOTE'
set -Eeuo pipefail
SQL="COPY (SELECT * FROM public.\"$T\""
[[ -n "$W" ]] && SQL+=" WHERE $W"
SQL+=") TO STDOUT WITH CSV HEADER"
docker exec "$DBC" psql -X -q -v ON_ERROR_STOP=1 -U "$DBU" -d "$DBN" -c "$SQL"
REMOTE
  [[ -s "$file" ]] || fail "empty export for $table"
  echo "EXPORT|$table|$(wc -l < "$file" | tr -d ' ') lines"
}

export_table face_generation_regions ""
export_table tts_languages ""
export_table tts_locales ""
export_table tts_locale_aliases ""
export_table tts_providers "provider_code = 'azure'"
export_table tts_provider_models "provider_code = 'azure'"
export_table tts_model_language_capabilities "provider_code = 'azure'"
export_table tts_model_locale_capabilities "provider_code = 'azure'"
export_table tts_voices "provider = 'azure'"
export_table tts_voice_model_capabilities "provider_code = 'azure'"
export_table tts_voice_locale_capabilities "voice_id IN (SELECT id FROM public.tts_voices WHERE provider='azure')"
echo "DEV_MASTERDATA_EXPORT=PASS"

# -----------------------------------------------------------------------------
# 2. Production backup + customer-count checkpoint before any live mutation.
# -----------------------------------------------------------------------------
echo
echo "===== 2. PRODUCTION SAFETY SNAPSHOT ====="
ssh "$PROD_HOST" "PROD_ROOT='$PROD_ROOT' STAMP='$STAMP' bash -s" > "$RUN/prod-pre.env" <<'REMOTE'
set -Eeuo pipefail
ROOT="$PROD_ROOT"
[[ -f "$ROOT/infra/.env" && -f "$ROOT/docker-compose.yml" ]] || exit 2
DB_C=desifaces-db; docker inspect "$DB_C" >/dev/null 2>&1 || exit 2
docker inspect desifaces-redis >/dev/null 2>&1 || exit 2
DB_USER="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_DB"')"
[[ "$DB_NAME" == desifaces ]] || exit 2
BACKUP_DIR="/home/azureuser/backups/desifaces-full-sync-$STAMP"
mkdir -p "$BACKUP_DIR"
docker exec "$DB_C" pg_dump -Fc -U "$DB_USER" -d "$DB_NAME" > "$BACKUP_DIR/desifaces-$STAMP.dump"
sha256sum "$BACKUP_DIR/desifaces-$STAMP.dump" > "$BACKUP_DIR/desifaces-$STAMP.dump.sha256"
cp "$ROOT/infra/.env" "$BACKUP_DIR/infra.env.pre-sync"
sudo cp /etc/nginx/sites-enabled/desifaces.conf "$BACKUP_DIR/nginx.desifaces.conf.pre-sync"
: > "$BACKUP_DIR/customer-counts.before"
for t in core.users media_assets pricing_credit_accounts pricing_credit_lots pricing_credit_reservations studio_jobs longform_jobs; do
  if docker exec "$DB_C" psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -c "select to_regclass('$t') is not null" | grep -qx t; then
    n="$(docker exec "$DB_C" psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -c "select count(*) from $t" | tr -d '[:space:]')"
    echo "$t|$n" >> "$BACKUP_DIR/customer-counts.before"
  fi
done
printf 'BACKUP_DIR=%q\n' "$BACKUP_DIR"
REMOTE
# shellcheck disable=SC1090
source "$RUN/prod-pre.env"
[[ -n "${BACKUP_DIR:-}" ]] || fail "production backup not established"
echo "PRODUCTION_BACKUP=$BACKUP_DIR"
echo "PRODUCTION_SAFETY_SNAPSHOT=PASS"
scp -q "$RUN"/md/*.csv "$PROD_HOST:/tmp/"

# Transactional merge SQL shared by clone validation and live execution.
cat > "$RUN/merge.sql" <<'SQL'
\set ON_ERROR_STOP on
BEGIN;
DO $$
DECLARE
  t text;
  cols text;
  keys text;
  keyarr text[];
  updates text;
  q text;
  tables text[] := ARRAY[
    'face_generation_regions','tts_languages','tts_locales','tts_locale_aliases',
    'tts_providers','tts_provider_models','tts_model_language_capabilities',
    'tts_model_locale_capabilities','tts_voices','tts_voice_model_capabilities',
    'tts_voice_locale_capabilities'
  ];
BEGIN
  FOREACH t IN ARRAY tables LOOP
    SELECT array_agg(a.attname ORDER BY x.ord),
           string_agg(quote_ident(a.attname), ', ' ORDER BY x.ord)
      INTO keyarr, keys
    FROM pg_index i
    JOIN LATERAL unnest(i.indkey) WITH ORDINALITY x(attnum,ord) ON true
    JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=x.attnum
    WHERE i.indrelid=format('public.%I',t)::regclass AND i.indisprimary;

    IF keyarr IS NULL THEN
      SELECT s.keyarr, s.keys INTO keyarr, keys
      FROM (
        SELECT i.indexrelid,
               array_agg(a.attname ORDER BY x.ord) AS keyarr,
               string_agg(quote_ident(a.attname), ', ' ORDER BY x.ord) AS keys
        FROM pg_index i
        JOIN LATERAL unnest(i.indkey) WITH ORDINALITY x(attnum,ord) ON true
        JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=x.attnum
        WHERE i.indrelid=format('public.%I',t)::regclass
          AND i.indisunique AND i.indisvalid
          AND i.indpred IS NULL AND i.indexprs IS NULL
        GROUP BY i.indexrelid
        ORDER BY i.indexrelid
        LIMIT 1
      ) s;
    END IF;
    IF keyarr IS NULL THEN RAISE EXCEPTION 'No conflict key for %',t; END IF;

    SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
      INTO cols
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name=t AND is_generated='NEVER';

    SELECT string_agg(format('%1$I = EXCLUDED.%1$I',c.column_name), ', ' ORDER BY c.ordinal_position)
      INTO updates
    FROM information_schema.columns c
    WHERE c.table_schema='public' AND c.table_name=t AND c.is_generated='NEVER'
      AND NOT (c.column_name = ANY(keyarr))
      AND c.column_name NOT IN ('created_at','updated_at','discovered_at','last_seen_at','last_synced_at','refreshed_at');

    IF updates IS NULL THEN updates := format('%1$I = EXCLUDED.%1$I', keyarr[1]); END IF;
    q := format('INSERT INTO public.%1$I (%2$s) SELECT %2$s FROM sync_stage.%1$I ON CONFLICT (%3$s) DO UPDATE SET %4$s',t,cols,keys,updates);
    EXECUTE q;
  END LOOP;
END $$;
COMMIT;
SQL
scp -q "$RUN/merge.sql" "$PROD_HOST:/tmp/desifaces-sync-merge-$STAMP.sql"

# -----------------------------------------------------------------------------
# 3. Validate exact change pattern against a temporary clone of production.
# -----------------------------------------------------------------------------
echo
echo "===== 3. TEMP CLONE MASTERDATA CERTIFICATION ====="
ssh "$PROD_HOST" "STAMP='$STAMP' BACKUP_DIR='$BACKUP_DIR' bash -s" <<'REMOTE'
set -Eeuo pipefail
DB_C=desifaces-db
DB_USER="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_USER"')"
AUDIT_DB="desifaces_sync_audit_${STAMP//[^0-9A-Za-z]/_}"
cleanup(){ docker exec "$DB_C" psql -X -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$AUDIT_DB\" WITH (FORCE)" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
docker exec "$DB_C" createdb -U "$DB_USER" "$AUDIT_DB"
cat "$BACKUP_DIR/desifaces-$STAMP.dump" | docker exec -i "$DB_C" pg_restore -U "$DB_USER" -d "$AUDIT_DB" --no-owner --no-privileges

docker exec "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$AUDIT_DB" -c 'CREATE SCHEMA sync_stage'
for t in face_generation_regions tts_languages tts_locales tts_locale_aliases tts_providers tts_provider_models tts_model_language_capabilities tts_model_locale_capabilities tts_voices tts_voice_model_capabilities tts_voice_locale_capabilities; do
  docker exec "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$AUDIT_DB" -c "CREATE TABLE sync_stage.\"$t\" AS SELECT * FROM public.\"$t\" WITH NO DATA"
  cat "/tmp/$t.csv" | docker exec -i "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$AUDIT_DB" -c "COPY sync_stage.\"$t\" FROM STDIN WITH CSV HEADER"
done
cat "/tmp/desifaces-sync-merge-$STAMP.sql" | docker exec -i "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$AUDIT_DB"

COUNTRIES="$(docker exec "$DB_C" psql -X -A -t -U "$DB_USER" -d "$AUDIT_DB" -c "
SELECT count(DISTINCT l.country_code)
FROM public.tts_locales l
WHERE l.is_enabled AND l.is_user_selectable AND l.tts_supported
AND EXISTS (
 SELECT 1 FROM public.tts_voice_locale_capabilities vl
 JOIN public.tts_voices v ON v.id=vl.voice_id
 JOIN public.tts_voice_model_capabilities vm ON vm.voice_id=v.id AND vm.provider_code=v.provider AND vm.is_enabled AND vm.is_approved
 JOIN public.tts_provider_models m ON m.provider_code=vm.provider_code AND m.model_code=vm.model_code AND m.is_enabled AND m.routing_enabled
 JOIN public.tts_providers p ON p.provider_code=vm.provider_code AND p.is_enabled AND p.routing_enabled
 WHERE vl.locale=l.locale AND vl.is_enabled AND vl.is_approved
);" | tr -d '[:space:]')"
echo "AUDIT_EXECUTABLE_COUNTRIES=$COUNTRIES"
[[ "$COUNTRIES" =~ ^[0-9]+$ && "$COUNTRIES" -ge 100 ]] || { echo "FAIL: clone global audio coverage below 100 countries"; exit 4; }
for cc in US GB CA AU IN; do
  n="$(docker exec "$DB_C" psql -X -A -t -U "$DB_USER" -d "$AUDIT_DB" -c "select count(*) from public.tts_locales where country_code='$cc' and is_enabled and is_user_selectable and tts_supported" | tr -d '[:space:]')"
  echo "AUDIT_COUNTRY_$cc=$n"; [[ "$n" -gt 0 ]] || exit 4
done
echo "TEMP_CLONE_MASTERDATA_CERTIFICATION=PASS"
REMOTE

# -----------------------------------------------------------------------------
# 4. Patch production .env semantically. Production-only models/features remain
#    production-specific; dev secrets are never copied.
# -----------------------------------------------------------------------------
echo
echo "===== 4. PRODUCTION ENV SEMANTIC PARITY ====="
ENV_PATCH="$RUN/env-patch.tsv"; : > "$ENV_PATCH"
for k in AUDIO_OUTPUT_CONTAINER AZURE_AUDIO_CONTAINER FACE_INPUT_CONTAINER FACE_OUTPUT_CONTAINER OPENAI_IMAGE_MODERATION; do
  var="SAFE_$k"; v="${!var:-}"; [[ -n "$v" ]] && printf '%s\t%s\n' "$k" "$v" >> "$ENV_PATCH"
done
printf '%s\t%s\n' OPENAI_IMAGE_MODEL_T2I gpt-image-2 >> "$ENV_PATCH"
printf '%s\t%s\n' OPENAI_IMAGE_MODEL_EDIT gpt-image-2 >> "$ENV_PATCH"
printf '%s\t%s\n' DF_DIRECTOR_LLM_MODEL gpt-5.6-sol >> "$ENV_PATCH"
printf '%s\t%s\n' DF_ASSISTANT_LLM_MODEL gpt-5.6-terra >> "$ENV_PATCH"
printf '%s\t%s\n' ENABLE_PUBLISH_YT false >> "$ENV_PATCH"
scp -q "$ENV_PATCH" "$PROD_HOST:/tmp/desifaces-env-patch-$STAMP.tsv"
ssh "$PROD_HOST" "PROD_ROOT='$PROD_ROOT' STAMP='$STAMP' BACKUP_DIR='$BACKUP_DIR' bash -s" <<'REMOTE'
set -Eeuo pipefail
ENV="$PROD_ROOT/infra/.env"; PATCH="/tmp/desifaces-env-patch-$STAMP.tsv"
python3 - "$ENV" "$PATCH" <<'PY'
from pathlib import Path
import sys
env=Path(sys.argv[1]); patch=Path(sys.argv[2])
lines=env.read_text(encoding='utf-8',errors='replace').splitlines(); changes={}
for line in patch.read_text().splitlines():
    if line.strip():
        k,v=line.split('\t',1); changes[k]=v
changes.setdefault('ELEVENLABS_API_KEY','')
changes.setdefault('SARVAM_API_KEY','')
seen=set(); out=[]
for raw in lines:
    s=raw.strip()
    if s and not s.startswith('#') and '=' in s:
        k=s.split('=',1)[0].strip()
        if k in changes:
            out.append(f'{k}={changes[k]}'); seen.add(k); continue
    out.append(raw)
for k,v in changes.items():
    if k not in seen: out.append(f'{k}={v}')
env.write_text('\n'.join(out)+'\n')
PY
chmod 600 "$ENV"
grep -E '^(OPENAI_IMAGE_MODEL_T2I|OPENAI_IMAGE_MODEL_EDIT|DF_DIRECTOR_LLM_MODEL|DF_ASSISTANT_LLM_MODEL)=' "$ENV"
cd "$PROD_ROOT"; BASE=docker-compose.yml; OV=deploy/production/docker-compose.v3-app.production.yml
[[ -f "$OV" ]] || { cp "$BACKUP_DIR/infra.env.pre-sync" "$ENV"; echo "FAIL: production overlay missing"; exit 5; }
resolved="$(docker compose --env-file infra/.env -f "$BASE" -f "$OV" config)"
if printf '%s\n' "$resolved" | grep -E 'OPENAI_IMAGE_MODEL_(T2I|EDIT):.*gpt-image-1\.5' >/dev/null; then
  cp "$BACKUP_DIR/infra.env.pre-sync" "$ENV"
  echo "FAIL: compose still resolves gpt-image-1.5; env restored and live DB untouched"
  exit 5
fi
echo "PRODUCTION_ENV_PATCH=PASS"
echo "COMPOSE_MODEL_PARITY_PRECHECK=PASS"
REMOTE

# -----------------------------------------------------------------------------
# 5. Apply exactly the clone-certified masterdata upsert to live production.
# -----------------------------------------------------------------------------
echo
echo "===== 5. LIVE MASTERDATA UPSERT ====="
ssh "$PROD_HOST" "STAMP='$STAMP' bash -s" <<'REMOTE'
set -Eeuo pipefail
DB_C=desifaces-db
DB_USER="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_DB"')"
docker exec "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -c 'DROP SCHEMA IF EXISTS sync_stage CASCADE; CREATE SCHEMA sync_stage'
for t in face_generation_regions tts_languages tts_locales tts_locale_aliases tts_providers tts_provider_models tts_model_language_capabilities tts_model_locale_capabilities tts_voices tts_voice_model_capabilities tts_voice_locale_capabilities; do
  docker exec "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -c "CREATE TABLE sync_stage.\"$t\" AS SELECT * FROM public.\"$t\" WITH NO DATA"
  cat "/tmp/$t.csv" | docker exec -i "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -c "COPY sync_stage.\"$t\" FROM STDIN WITH CSV HEADER"
done
cat "/tmp/desifaces-sync-merge-$STAMP.sql" | docker exec -i "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME"
docker exec "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -c 'DROP SCHEMA sync_stage CASCADE'
echo "LIVE_MASTERDATA_UPSERT=PASS"
REMOTE

# -----------------------------------------------------------------------------
# 6. Recreate only application services from existing certified prod package.
#    DB, Redis and web are excluded; no image build and no source replacement.
# -----------------------------------------------------------------------------
echo
echo "===== 6. APPLY RUNTIME MODEL CONFIG ====="
ssh "$PROD_HOST" "PROD_ROOT='$PROD_ROOT' bash -s" <<'REMOTE'
set -Eeuo pipefail
cd "$PROD_ROOT"; BASE=docker-compose.yml; OV=deploy/production/docker-compose.v3-app.production.yml
mapfile -t services < <(docker compose --env-file infra/.env -f "$BASE" -f "$OV" config --services)
apps=()
for s in "${services[@]}"; do case "$s" in db|postgres|redis|web|svc-web) ;; *) apps+=("$s");; esac; done
[[ ${#apps[@]} -gt 0 ]] || exit 5
docker compose --env-file infra/.env -f "$BASE" -f "$OV" up -d --no-deps --force-recreate "${apps[@]}"
echo "APPLICATION_RECREATE=PASS"
REMOTE

# -----------------------------------------------------------------------------
# 7. Surgical Nginx parity for public Director/Assistant prefixes.
# -----------------------------------------------------------------------------
echo
echo "===== 7. PUBLIC API EDGE PARITY ====="
ssh "$PROD_HOST" "BACKUP_DIR='$BACKUP_DIR' bash -s" <<'REMOTE'
set -Eeuo pipefail
CONF=/etc/nginx/sites-enabled/desifaces.conf; TMP="$(mktemp)"; sudo cat "$CONF" > "$TMP"
python3 - "$TMP" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); lines=p.read_text().splitlines(); blocks=[]; start=None; depth=0
for i,line in enumerate(lines):
    if start is None and line.strip().startswith('server') and '{' in line:
        start=i; depth=line.count('{')-line.count('}'); continue
    if start is not None:
        depth += line.count('{')-line.count('}')
        if depth==0: blocks.append((start,i)); start=None
block=None
for a,b in blocks:
    if 'server_name api.desifaces.ai;' in '\n'.join(lines[a:b+1]): block=(a,b); break
if not block: raise SystemExit('api server block not found')
a,b=block; text='\n'.join(lines[a:b+1]); insert=[]
if 'location /director/' not in text:
    insert += ['', '    location /director/ {','        proxy_pass http://127.0.0.1:18011/;','        proxy_http_version 1.1;','        proxy_set_header Host $host;','        proxy_set_header X-Real-IP $remote_addr;','        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;','        proxy_set_header X-Forwarded-Proto https;','    }']
if 'location /assistant/' not in text:
    insert += ['', '    location /assistant/ {','        proxy_pass http://127.0.0.1:18012/;','        proxy_http_version 1.1;','        proxy_set_header Host $host;','        proxy_set_header X-Real-IP $remote_addr;','        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;','        proxy_set_header X-Forwarded-Proto https;','    }']
if insert: lines[b:b]=insert
p.write_text('\n'.join(lines)+'\n')
PY
sudo cp "$TMP" "$CONF"; rm -f "$TMP"
if ! sudo nginx -t; then
  sudo cp "$BACKUP_DIR/nginx.desifaces.conf.pre-sync" "$CONF"; sudo nginx -t; sudo systemctl reload nginx
  echo "FAIL: nginx change rolled back"; exit 6
fi
sudo systemctl reload nginx
echo "NGINX_API_PARITY=PASS"
REMOTE

# -----------------------------------------------------------------------------
# 8. End-to-end certification and exact customer-count preservation check.
# -----------------------------------------------------------------------------
echo
echo "===== 8. FINAL PRODUCTION CERTIFICATION ====="
ssh "$PROD_HOST" "BACKUP_DIR='$BACKUP_DIR' bash -s" <<'REMOTE'
set -Eeuo pipefail
DB_C=desifaces-db; DB_USER="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_USER"')"; DB_NAME="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_DB"')"
for c in $(docker ps --format '{{.Names}}' | grep -E 'svc-(face|audio|core|fusion|pricing|dashboard)'); do
  envs="$(docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}')"
  for k in OPENAI_IMAGE_MODEL_T2I OPENAI_IMAGE_MODEL_EDIT; do
    v="$(printf '%s\n' "$envs" | awk -F= -v K="$k" '$1==K{sub(/^[^=]*=/,"");print;exit}')"
    [[ -z "$v" || "$v" == gpt-image-2 ]] || { echo "FAIL: $c $k=$v"; exit 7; }
  done
done
echo "IMAGE_MODEL_PARITY=PASS"

for spec in 'core|http://127.0.0.1:8000/api/health' 'audio|http://127.0.0.1:8004/api/health' 'pricing|http://127.0.0.1:8009/api/health' 'director|http://127.0.0.1:18011/api/health' 'assistant|http://127.0.0.1:18012/api/health'; do
  n="${spec%%|*}"; u="${spec#*|}"; code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 "$u" || true)"; echo "$n HTTP=$code"; [[ "$code" == 200 ]] || exit 7
done
countries="$(curl -fsS --max-time 8 http://127.0.0.1:8004/api/audio/catalog/countries)"
count="$(python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("items",[])))' <<<"$countries")"
echo "AUDIO_COUNTRY_COUNT=$count"; [[ "$count" -ge 100 ]] || exit 7
for cc in US GB CA AU IN; do
  body="$(curl -fsS --max-time 8 "http://127.0.0.1:8004/api/audio/catalog/target-languages?country_code=$cc")"
  n="$(python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("items",[])))' <<<"$body")"; echo "AUDIO_TARGET_$cc=$n"; [[ "$n" -gt 0 ]] || exit 7
done
echo "AUDIO_GLOBAL_PARITY=PASS"

: > "$BACKUP_DIR/customer-counts.after"
for t in core.users media_assets pricing_credit_accounts pricing_credit_lots pricing_credit_reservations studio_jobs longform_jobs; do
  if docker exec "$DB_C" psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -c "select to_regclass('$t') is not null" | grep -qx t; then
    n="$(docker exec "$DB_C" psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -c "select count(*) from $t" | tr -d '[:space:]')"; echo "$t|$n" >> "$BACKUP_DIR/customer-counts.after"
  fi
done
if ! diff -u "$BACKUP_DIR/customer-counts.before" "$BACKUP_DIR/customer-counts.after"; then echo "FAIL: customer row counts changed"; exit 7; fi
echo "CUSTOMER_DATA_PRESERVED=PASS"
REMOTE

for u in https://web.desifaces.ai/auth/login https://api.desifaces.ai/director/api/health https://api.desifaces.ai/assistant/api/health; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$u" 2>/dev/null || true)"; echo "$u HTTP=$code"; [[ "$code" == 200 ]] || fail "public contract failed: $u"
done
echo "PUBLIC_WEB_API_PARITY=PASS"

echo "============================================================"
echo " PRODUCTION PLATFORM PARITY PASS"
echo "============================================================"
echo "CUSTOMER_DATA_PRESERVED=PASS"
echo "PRICING_DATA_PRESERVED=PASS"
echo "IMAGE_MODEL_PARITY=PASS"
echo "AUDIO_GLOBAL_PARITY=PASS"
echo "PUBLIC_WEB_API_PARITY=PASS"
echo "NEXT_ACTION=MOBILE_RELEASE_BUILD"
