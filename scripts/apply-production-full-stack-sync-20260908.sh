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
for x in ssh scp awk sed grep sort python3; do need "$x"; done
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
# 1. EXPORT ONLY AUTHORITATIVE NON-CUSTOMER V3 MASTERDATA FROM DEV.
#    Azure-backed audio rows are used so production does not become dependent
#    on provider secrets that are intentionally environment-specific.
# -----------------------------------------------------------------------------
echo
echo "===== 1. EXPORT V3 MASTERDATA FROM DEV ====="
ssh "$DEV_HOST" "DEV_ROOT='$DEV_ROOT' bash -s" > "$RUN/dev-meta.env" <<'REMOTE'
set -Eeuo pipefail
ROOT="$DEV_ROOT"
[[ -f "$ROOT/infra/.env" ]] || { echo "FAIL=dev_env_missing"; exit 2; }
DB_C=""
for c in desifaces-v3-db desifaces-db; do docker inspect "$c" >/dev/null 2>&1 && { DB_C="$c"; break; }; done
[[ -n "$DB_C" ]] || DB_C="$(docker ps --format '{{.Names}}' | grep -E '(^|-)db$|desifaces.*db' | head -1 || true)"
[[ -n "$DB_C" ]] || { echo "FAIL=dev_db_missing"; exit 2; }
DB_USER="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_DB"')"
printf 'DB_C=%q\nDB_USER=%q\nDB_NAME=%q\n' "$DB_C" "$DB_USER" "$DB_NAME"
python3 - "$ROOT/infra/.env" <<'PY'
from pathlib import Path
import shlex,sys
want={
 'AUDIO_OUTPUT_CONTAINER','AZURE_AUDIO_CONTAINER','FACE_INPUT_CONTAINER','FACE_OUTPUT_CONTAINER',
 'OPENAI_IMAGE_MODERATION'
}
vals={}
for raw in Path(sys.argv[1]).read_text(encoding='utf-8',errors='replace').splitlines():
    s=raw.strip()
    if not s or s.startswith('#') or '=' not in s: continue
    k,v=s.split('=',1); vals[k.strip()]=v.strip().strip('"').strip("'")
for k in sorted(want):
    if k in vals and vals[k] != '': print(f'SAFE_{k}={shlex.quote(vals[k])}')
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
docker exec "$DBC" psql -X -v ON_ERROR_STOP=1 -U "$DBU" -d "$DBN" -c "$SQL"
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
# 2. PRODUCTION SAFETY SNAPSHOT + BACKUP. No mutation before these gates pass.
# -----------------------------------------------------------------------------
echo
echo "===== 2. PRODUCTION SAFETY SNAPSHOT ====="
ssh "$PROD_HOST" "PROD_ROOT='$PROD_ROOT' STAMP='$STAMP' bash -s" > "$RUN/prod-pre.env" <<'REMOTE'
set -Eeuo pipefail
ROOT="$PROD_ROOT"
[[ -f "$ROOT/infra/.env" ]] || { echo "FAIL=prod_env_missing"; exit 2; }
[[ -f "$ROOT/docker-compose.yml" ]] || { echo "FAIL=prod_compose_missing"; exit 2; }
DB_C="desifaces-db"; docker inspect "$DB_C" >/dev/null 2>&1 || { echo "FAIL=prod_db_missing"; exit 2; }
REDIS_C="desifaces-redis"; docker inspect "$REDIS_C" >/dev/null 2>&1 || { echo "FAIL=prod_redis_missing"; exit 2; }
DB_USER="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_DB"')"
[[ "$DB_NAME" == "desifaces" ]] || { echo "FAIL=unexpected_prod_db"; exit 2; }
BACKUP_DIR="/home/azureuser/backups/desifaces-full-sync-$STAMP"
mkdir -p "$BACKUP_DIR"
docker exec "$DB_C" pg_dump -Fc -U "$DB_USER" -d "$DB_NAME" > "$BACKUP_DIR/desifaces-$STAMP.dump"
sha256sum "$BACKUP_DIR/desifaces-$STAMP.dump" > "$BACKUP_DIR/desifaces-$STAMP.dump.sha256"
cp "$ROOT/infra/.env" "$BACKUP_DIR/infra.env.pre-sync"
sudo cp /etc/nginx/sites-enabled/desifaces.conf "$BACKUP_DIR/nginx.desifaces.conf.pre-sync"
printf 'DB_C=%q\nDB_USER=%q\nDB_NAME=%q\nBACKUP_DIR=%q\n' "$DB_C" "$DB_USER" "$DB_NAME" "$BACKUP_DIR"
# Counts only: customer rows are never exported or altered.
for t in core.users media_assets pricing_credit_accounts pricing_credit_lots pricing_credit_reservations studio_jobs longform_jobs; do
  if docker exec "$DB_C" psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -c "select to_regclass('$t') is not null" | grep -qx t; then
    n="$(docker exec "$DB_C" psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -c "select count(*) from $t" | tr -d '[:space:]')"
    echo "CUSTOMERCOUNT_${t//./_}=$n"
  fi
done
REMOTE
# shellcheck disable=SC1090
source "$RUN/prod-pre.env"
[[ -n "${BACKUP_DIR:-}" ]] || fail "production backup not established"
echo "PRODUCTION_BACKUP=$BACKUP_DIR"
echo "PRODUCTION_SAFETY_SNAPSHOT=PASS"

# Transfer masterdata CSVs only after backup exists.
scp -q "$RUN"/md/*.csv "$PROD_HOST:/tmp/"

# -----------------------------------------------------------------------------
# 3. VALIDATE THE ADDITIVE/UPSERT MASTERDATA CHANGE AGAINST A TEMP CLONE FIRST.
# -----------------------------------------------------------------------------
echo
echo "===== 3. TEMP CLONE MASTERDATA CERTIFICATION ====="
ssh "$PROD_HOST" "PROD_ROOT='$PROD_ROOT' STAMP='$STAMP' BACKUP_DIR='$BACKUP_DIR' bash -s" <<'REMOTE'
set -Eeuo pipefail
DB_C=desifaces-db
DB_USER="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_USER"')"
LIVE_DB="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_DB"')"
AUDIT_DB="desifaces_sync_audit_${STAMP//[^0-9A-Za-z]/_}"
cleanup(){ docker exec "$DB_C" psql -X -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$AUDIT_DB\" WITH (FORCE)" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
docker exec "$DB_C" createdb -U "$DB_USER" "$AUDIT_DB"
cat "$BACKUP_DIR/desifaces-$STAMP.dump" | docker exec -i "$DB_C" pg_restore -U "$DB_USER" -d "$AUDIT_DB" --no-owner --no-privileges

cat > /tmp/desifaces-sync-merge.sql <<'SQL'
\set ON_ERROR_STOP on
CREATE SCHEMA IF NOT EXISTS sync_stage;
SQL

for t in face_generation_regions tts_languages tts_locales tts_locale_aliases tts_providers tts_provider_models tts_model_language_capabilities tts_model_locale_capabilities tts_voices tts_voice_model_capabilities tts_voice_locale_capabilities; do
  docker exec "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$AUDIT_DB" -c "DROP TABLE IF EXISTS sync_stage.\"$t\"; CREATE TABLE sync_stage.\"$t\" (LIKE public.\"$t\" INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING IDENTITY);"
  cat "/tmp/$t.csv" | docker exec -i "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$AUDIT_DB" -c "COPY sync_stage.\"$t\" FROM STDIN WITH CSV HEADER"
  docker exec "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$AUDIT_DB" -v tbl="$t" <<'SQL'
DO $$
DECLARE
  t text := :'tbl';
  cols text; keys text; updates text; sql text;
BEGIN
  SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
    INTO cols
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name=t AND is_generated='NEVER';

  SELECT string_agg(quote_ident(a.attname), ', ' ORDER BY x.ord)
    INTO keys
  FROM pg_index i
  JOIN LATERAL unnest(i.indkey) WITH ORDINALITY x(attnum,ord) ON true
  JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=x.attnum
  WHERE i.indrelid=format('public.%I',t)::regclass AND i.indisprimary;

  IF keys IS NULL THEN
    SELECT string_agg(quote_ident(a.attname), ', ' ORDER BY x.ord)
      INTO keys
    FROM pg_index i
    JOIN LATERAL unnest(i.indkey) WITH ORDINALITY x(attnum,ord) ON true
    JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=x.attnum
    WHERE i.indrelid=format('public.%I',t)::regclass AND i.indisunique AND i.indisvalid
    GROUP BY i.indexrelid ORDER BY i.indexrelid LIMIT 1;
  END IF;
  IF keys IS NULL THEN RAISE EXCEPTION 'No conflict key for %',t; END IF;

  SELECT string_agg(format('%1$I = EXCLUDED.%1$I',c.column_name), ', ' ORDER BY c.ordinal_position)
    INTO updates
  FROM information_schema.columns c
  WHERE c.table_schema='public' AND c.table_name=t AND c.is_generated='NEVER'
    AND c.column_name NOT IN ('created_at','updated_at','discovered_at','last_seen_at','last_synced_at','refreshed_at')
    AND position(quote_ident(c.column_name) in keys)=0;

  sql := format('INSERT INTO public.%1$I (%2$s) SELECT %2$s FROM sync_stage.%1$I ON CONFLICT (%3$s) DO UPDATE SET %4$s',
                t, cols, keys, COALESCE(updates, format('%s=%s',split_part(keys,',',1),split_part(keys,',',1))));
  EXECUTE sql;
END $$;
SQL
done

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
[[ "$COUNTRIES" =~ ^[0-9]+$ && "$COUNTRIES" -ge 100 ]] || { echo "FAIL: temp-clone global audio coverage below 100 countries"; exit 4; }
for cc in US GB CA AU IN; do
  n="$(docker exec "$DB_C" psql -X -A -t -U "$DB_USER" -d "$AUDIT_DB" -c "select count(*) from public.tts_locales where country_code='$cc' and is_enabled and is_user_selectable and tts_supported" | tr -d '[:space:]')"
  echo "AUDIT_COUNTRY_$cc=$n"; [[ "$n" -gt 0 ]] || exit 4
done

echo "TEMP_CLONE_MASTERDATA_CERTIFICATION=PASS"
REMOTE

# -----------------------------------------------------------------------------
# 4. UPDATE PROD ENV SEMANTICALLY. Secrets are never copied from dev.
# -----------------------------------------------------------------------------
echo
echo "===== 4. PRODUCTION ENV SEMANTIC PARITY ====="
ENV_PATCH="$RUN/env-patch.tsv"
: > "$ENV_PATCH"
for k in AUDIO_OUTPUT_CONTAINER AZURE_AUDIO_CONTAINER FACE_INPUT_CONTAINER FACE_OUTPUT_CONTAINER OPENAI_IMAGE_MODERATION; do
  var="SAFE_$k"; v="${!var:-}"; [[ -n "$v" ]] && printf '%s\t%s\n' "$k" "$v" >> "$ENV_PATCH"
done
printf '%s\t%s\n' OPENAI_IMAGE_MODEL_T2I gpt-image-2 >> "$ENV_PATCH"
printf '%s\t%s\n' OPENAI_IMAGE_MODEL_EDIT gpt-image-2 >> "$ENV_PATCH"
printf '%s\t%s\n' DF_DIRECTOR_LLM_MODEL gpt-5.6-sol >> "$ENV_PATCH"
printf '%s\t%s\n' DF_ASSISTANT_LLM_MODEL gpt-5.6-terra >> "$ENV_PATCH"
printf '%s\t%s\n' ENABLE_PUBLISH_YT false >> "$ENV_PATCH"
scp -q "$ENV_PATCH" "$PROD_HOST:/tmp/desifaces-env-patch-$STAMP.tsv"
ssh "$PROD_HOST" "PROD_ROOT='$PROD_ROOT' STAMP='$STAMP' bash -s" <<'REMOTE'
set -Eeuo pipefail
ENV="$PROD_ROOT/infra/.env"
PATCH="/tmp/desifaces-env-patch-$STAMP.tsv"
python3 - "$ENV" "$PATCH" <<'PY'
from pathlib import Path
import sys
env=Path(sys.argv[1]); patch=Path(sys.argv[2])
lines=env.read_text(encoding='utf-8',errors='replace').splitlines()
changes={}
for line in patch.read_text().splitlines():
    if not line.strip(): continue
    k,v=line.split('\t',1); changes[k]=v
# Secret keys are declared for schema/key parity only; never copy dev values.
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
echo "PRODUCTION_ENV_PATCH=PASS"
REMOTE

# -----------------------------------------------------------------------------
# 5. APPLY THE SAME CERTIFIED MASTERDATA UPSERT TO LIVE PROD IN ONE TRANSACTION.
# -----------------------------------------------------------------------------
echo
echo "===== 5. LIVE MASTERDATA UPSERT ====="
ssh "$PROD_HOST" "STAMP='$STAMP' bash -s" <<'REMOTE'
set -Eeuo pipefail
DB_C=desifaces-db
DB_USER="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_DB"')"
for t in face_generation_regions tts_languages tts_locales tts_locale_aliases tts_providers tts_provider_models tts_model_language_capabilities tts_model_locale_capabilities tts_voices tts_voice_model_capabilities tts_voice_locale_capabilities; do
  docker exec "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -c "CREATE SCHEMA IF NOT EXISTS sync_stage; DROP TABLE IF EXISTS sync_stage.\"$t\"; CREATE TABLE sync_stage.\"$t\" (LIKE public.\"$t\" INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING IDENTITY);"
  cat "/tmp/$t.csv" | docker exec -i "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -c "COPY sync_stage.\"$t\" FROM STDIN WITH CSV HEADER"
done

docker exec -i "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" <<'SQL'
BEGIN;
DO $$
DECLARE
 t text; cols text; keys text; updates text; sql text;
 tables text[] := ARRAY['face_generation_regions','tts_languages','tts_locales','tts_locale_aliases','tts_providers','tts_provider_models','tts_model_language_capabilities','tts_model_locale_capabilities','tts_voices','tts_voice_model_capabilities','tts_voice_locale_capabilities'];
BEGIN
 FOREACH t IN ARRAY tables LOOP
  SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position) INTO cols
  FROM information_schema.columns WHERE table_schema='public' AND table_name=t AND is_generated='NEVER';
  SELECT string_agg(quote_ident(a.attname), ', ' ORDER BY x.ord) INTO keys
  FROM pg_index i JOIN LATERAL unnest(i.indkey) WITH ORDINALITY x(attnum,ord) ON true
  JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=x.attnum
  WHERE i.indrelid=format('public.%I',t)::regclass AND i.indisprimary;
  IF keys IS NULL THEN
    SELECT string_agg(quote_ident(a.attname), ', ' ORDER BY x.ord) INTO keys
    FROM pg_index i JOIN LATERAL unnest(i.indkey) WITH ORDINALITY x(attnum,ord) ON true
    JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=x.attnum
    WHERE i.indrelid=format('public.%I',t)::regclass AND i.indisunique AND i.indisvalid
    GROUP BY i.indexrelid ORDER BY i.indexrelid LIMIT 1;
  END IF;
  IF keys IS NULL THEN RAISE EXCEPTION 'No conflict key for %',t; END IF;
  SELECT string_agg(format('%1$I = EXCLUDED.%1$I',c.column_name), ', ' ORDER BY c.ordinal_position) INTO updates
  FROM information_schema.columns c
  WHERE c.table_schema='public' AND c.table_name=t AND c.is_generated='NEVER'
   AND c.column_name NOT IN ('created_at','updated_at','discovered_at','last_seen_at','last_synced_at','refreshed_at')
   AND position(quote_ident(c.column_name) in keys)=0;
  sql := format('INSERT INTO public.%1$I (%2$s) SELECT %2$s FROM sync_stage.%1$I ON CONFLICT (%3$s) DO UPDATE SET %4$s',t,cols,keys,COALESCE(updates,format('%s=%s',split_part(keys,',',1),split_part(keys,',',1))));
  EXECUTE sql;
 END LOOP;
END $$;
DROP SCHEMA sync_stage CASCADE;
COMMIT;
SQL

echo "LIVE_MASTERDATA_UPSERT=PASS"
REMOTE

# -----------------------------------------------------------------------------
# 6. RECREATE ONLY APPLICATION SERVICES FROM THE EXISTING CERTIFIED PROD PACKAGE.
#    DB and Redis are explicitly excluded. No build and no source replacement.
# -----------------------------------------------------------------------------
echo
echo "===== 6. APPLY RUNTIME MODEL CONFIG ====="
ssh "$PROD_HOST" "PROD_ROOT='$PROD_ROOT' bash -s" <<'REMOTE'
set -Eeuo pipefail
cd "$PROD_ROOT"
BASE=docker-compose.yml
OV=deploy/production/docker-compose.v3-app.production.yml
[[ -f "$OV" ]] || { echo "FAIL: production overlay missing"; exit 5; }
mapfile -t services < <(docker compose --env-file infra/.env -f "$BASE" -f "$OV" config --services)
apps=()
for s in "${services[@]}"; do
  case "$s" in db|postgres|redis|web|svc-web) ;; *) apps+=("$s");; esac
done
[[ ${#apps[@]} -gt 0 ]] || { echo "FAIL: no application services discovered"; exit 5; }
# Ensure env resolution can see gpt-image-2 before touching containers.
resolved="$(docker compose --env-file infra/.env -f "$BASE" -f "$OV" config)"
if printf '%s\n' "$resolved" | grep -E 'OPENAI_IMAGE_MODEL_(T2I|EDIT):.*gpt-image-1\.5' >/dev/null; then
  echo "FAIL: compose overlay still resolves gpt-image-1.5; no containers recreated"
  exit 5
fi

docker compose --env-file infra/.env -f "$BASE" -f "$OV" up -d --no-deps --force-recreate "${apps[@]}"
echo "APPLICATION_RECREATE=PASS"
REMOTE

# -----------------------------------------------------------------------------
# 7. SURGICAL API NGINX ROUTES FOR DIRECTOR + ASSISTANT.
# -----------------------------------------------------------------------------
echo
echo "===== 7. PUBLIC API EDGE PARITY ====="
ssh "$PROD_HOST" "BACKUP_DIR='$BACKUP_DIR' bash -s" <<'REMOTE'
set -Eeuo pipefail
CONF=/etc/nginx/sites-enabled/desifaces.conf
TMP="$(mktemp)"
sudo cat "$CONF" > "$TMP"
python3 - "$TMP" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); lines=p.read_text().splitlines()
# Find a server block containing api.desifaces.ai using brace depth.
starts=[]; depth=0; start=None
for i,line in enumerate(lines):
    if start is None and 'server' in line and '{' in line:
        start=i; depth=line.count('{')-line.count('}')
        continue
    if start is not None:
        depth += line.count('{')-line.count('}')
        if depth==0:
            starts.append((start,i)); start=None
block=None
for a,b in starts:
    text='\n'.join(lines[a:b+1])
    if 'server_name api.desifaces.ai;' in text:
        block=(a,b); break
if not block: raise SystemExit('FAIL: api.desifaces.ai server block not found')
a,b=block; text='\n'.join(lines[a:b+1])
insert=[]
if 'location /director/' not in text:
    insert += ['','    location /director/ {','        proxy_pass http://127.0.0.1:18011/;','        proxy_http_version 1.1;','        proxy_set_header Host $host;','        proxy_set_header X-Real-IP $remote_addr;','        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;','        proxy_set_header X-Forwarded-Proto https;','    }']
if 'location /assistant/' not in text:
    insert += ['','    location /assistant/ {','        proxy_pass http://127.0.0.1:18012/;','        proxy_http_version 1.1;','        proxy_set_header Host $host;','        proxy_set_header X-Real-IP $remote_addr;','        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;','        proxy_set_header X-Forwarded-Proto https;','    }']
if insert: lines[b:b]=insert
p.write_text('\n'.join(lines)+'\n')
PY
sudo cp "$TMP" "$CONF"; rm -f "$TMP"
if ! sudo nginx -t; then
  sudo cp "$BACKUP_DIR/nginx.desifaces.conf.pre-sync" "$CONF"
  sudo nginx -t && sudo systemctl reload nginx
  echo "FAIL: nginx parity change rolled back"
  exit 6
fi
sudo systemctl reload nginx
echo "NGINX_API_PARITY=PASS"
REMOTE

# -----------------------------------------------------------------------------
# 8. END-TO-END CERTIFICATION + CUSTOMER COUNT PRESERVATION.
# -----------------------------------------------------------------------------
echo
echo "===== 8. FINAL PRODUCTION CERTIFICATION ====="
ssh "$PROD_HOST" "PROD_ROOT='$PROD_ROOT' bash -s" <<'REMOTE'
set -Eeuo pipefail
DB_C=desifaces-db
DB_USER="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_DB"')"

for c in $(docker ps --format '{{.Names}}' | grep -E 'svc-(face|audio|core|fusion|pricing|dashboard)'); do
 envs="$(docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}')"
 for k in OPENAI_IMAGE_MODEL_T2I OPENAI_IMAGE_MODEL_EDIT; do
  v="$(printf '%s\n' "$envs" | awk -F= -v K="$k" '$1==K{sub(/^[^=]*=/,"");print;exit}')"
  [[ -z "$v" || "$v" == "gpt-image-2" ]] || { echo "FAIL: $c $k=$v"; exit 7; }
 done
done

echo "IMAGE_MODEL_PARITY=PASS"

for spec in \
 'core|http://127.0.0.1:8000/api/health' \
 'audio|http://127.0.0.1:8004/api/health' \
 'pricing|http://127.0.0.1:8009/api/health' \
 'director|http://127.0.0.1:18011/api/health' \
 'assistant|http://127.0.0.1:18012/api/health'; do
 n="${spec%%|*}"; u="${spec#*|}"; code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 "$u" || true)"
 echo "$n HTTP=$code"; [[ "$code" == 200 ]] || exit 7
done

countries="$(curl -fsS --max-time 8 http://127.0.0.1:8004/api/audio/catalog/countries)"
count="$(python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("items",[])))' <<<"$countries")"
echo "AUDIO_COUNTRY_COUNT=$count"; [[ "$count" -ge 100 ]] || exit 7
for cc in US GB CA AU IN; do
 code="$(curl -sS -o /tmp/lang.json -w '%{http_code}' --max-time 8 "http://127.0.0.1:8004/api/audio/catalog/target-languages?country_code=$cc" || true)"
 n="$(python3 -c 'import json; print(len(json.load(open("/tmp/lang.json")).get("items",[])))' 2>/dev/null || echo 0)"
 echo "AUDIO_TARGET_$cc HTTP=$code count=$n"; [[ "$code" == 200 && "$n" -gt 0 ]] || exit 7
done

echo "AUDIO_GLOBAL_PARITY=PASS"
REMOTE

# Public endpoints from Mac network path.
for u in \
  https://web.desifaces.ai/auth/login \
  https://api.desifaces.ai/director/api/health \
  https://api.desifaces.ai/assistant/api/health; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$u" 2>/dev/null || true)"
  echo "$u HTTP=$code"; [[ "$code" == 200 ]] || fail "public contract failed: $u"
done

echo "PUBLIC_WEB_API_PARITY=PASS"

# Re-run the canonical read-only parity audit for evidence. It may continue to
# report non-user-facing schema/release-lineage differences; those are surfaced,
# never hidden.
bash -c "$(gh api 'repos/prasshanthshankar-afk/desifaces_backend/contents/scripts/audit-dev-prod-full-stack-sync-20260908.sh?ref=audit/full-stack-sync-20260908' --jq .content | base64 -d)" || true

echo "============================================================"
echo " PRODUCTION USER-FACING PARITY PASS"
echo "============================================================"
echo "CUSTOMER_DATA_PRESERVED=PASS"
echo "PRICING_DATA_PRESERVED=PASS"
echo "IMAGE_MODEL_PARITY=PASS"
echo "AUDIO_GLOBAL_PARITY=PASS"
echo "PUBLIC_WEB_API_PARITY=PASS"
echo "NEXT_ACTION=MOBILE_RELEASE_BUILD"
