#!/usr/bin/env bash
set -Eeuo pipefail

PROD_HOST="${PROD_HOST:-desifaces-gpu}"
ROOT="/home/azureuser/workspace/desifaces"
LEGACY="/home/azureuser/workspace/desifaces-v2"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

fail(){ echo "FAIL: $*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"; }
for x in ssh; do need "$x"; done
[[ "$(uname -s)" == "Darwin" ]] || fail "run from Mac release environment"
ssh -o BatchMode=yes -o ConnectTimeout=12 "$PROD_HOST" 'hostname -s' >/dev/null || fail "cannot SSH to $PROD_HOST"

ssh "$PROD_HOST" "ROOT='$ROOT' LEGACY='$LEGACY' STAMP='$STAMP' bash -s" <<'REMOTE'
set -Eeuo pipefail
fail(){ echo "FAIL: $*" >&2; exit 1; }

echo "============================================================"
echo " desifaces.ai — FINAL PRODUCTION RUNTIME CONSOLIDATION"
echo "============================================================"
echo "TARGET_BACKEND=$ROOT"
echo "LEGACY=$LEGACY"
echo "CUSTOMER_DATA_ACTION=NONE"
echo "DB_VOLUME_REPLACEMENT=FORBIDDEN"
echo "REDIS_VOLUME_REPLACEMENT=FORBIDDEN"
echo "LEGACY_DELETE=NONE"

ENV="$ROOT/infra/.env"
BASE="$ROOT/docker-compose.yml"
OVERLAY="$ROOT/deploy/production/docker-compose.v3-app.production.yml"
[[ -f "$ENV" && -f "$BASE" && -f "$OVERLAY" ]] || fail "canonical production compose/env missing"
docker inspect desifaces-db >/dev/null 2>&1 || fail "desifaces-db missing"
docker inspect desifaces-redis >/dev/null 2>&1 || fail "desifaces-redis missing"

BACKUP="/home/azureuser/backups/desifaces-runtime-consolidation-$STAMP"
mkdir -p "$BACKUP"

# ---------------------------------------------------------------------------
# 1. Fresh safety snapshot and exact current persistence identity.
# ---------------------------------------------------------------------------
echo
echo "===== 1. SAFETY SNAPSHOT ====="
DB_USER="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_DB"')"
[[ "$DB_NAME" == "desifaces" ]] || fail "unexpected production DB $DB_NAME"

docker exec desifaces-db pg_dump -Fc -U "$DB_USER" -d "$DB_NAME" > "$BACKUP/desifaces-$STAMP.dump"
sha256sum "$BACKUP/desifaces-$STAMP.dump" > "$BACKUP/desifaces-$STAMP.dump.sha256"

for c in desifaces-db desifaces-redis; do
  docker inspect "$c" > "$BACKUP/$c.inspect.before.json"
  docker inspect "$c" --format '{{range .Mounts}}{{println .Type "|" .Name "|" .Source "|" .Destination}}{{end}}' | sort > "$BACKUP/$c.mounts.before"
  docker inspect "$c" --format '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' | sort > "$BACKUP/$c.networks.before"
  docker inspect "$c" --format '{{json .HostConfig.PortBindings}}' > "$BACKUP/$c.ports.before"
done

: > "$BACKUP/customer-counts.before"
for t in core.users media_assets pricing_credit_accounts pricing_credit_lots pricing_credit_reservations studio_jobs longform_jobs; do
  exists="$(docker exec desifaces-db psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -c "select to_regclass('$t') is not null" | tr -d '[:space:]')"
  if [[ "$exists" == t ]]; then
    n="$(docker exec desifaces-db psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -c "select count(*) from $t" | tr -d '[:space:]')"
    echo "$t|$n" >> "$BACKUP/customer-counts.before"
  fi
done

echo "BACKUP_DIR=$BACKUP"
echo "PRODUCTION_SAFETY_SNAPSHOT=PASS"

# ---------------------------------------------------------------------------
# 2. Canonical compose persistence preflight. Refuse to recreate if the
#    rendered canonical services do not resolve to the same existing volumes.
# ---------------------------------------------------------------------------
echo
echo "===== 2. CANONICAL DB / REDIS PERSISTENCE PREFLIGHT ====="
cd "$ROOT"
docker compose --env-file "$ENV" -f "$BASE" -f "$OVERLAY" config --format json > "$BACKUP/compose.canonical.json" || fail "docker compose JSON config unavailable"

python3 - "$BACKUP/compose.canonical.json" "$BACKUP/desifaces-db.mounts.before" "$BACKUP/desifaces-redis.mounts.before" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1]))
services=cfg.get('services',{})
for service, mountfile in [('desifaces-db',sys.argv[2]),('desifaces-redis',sys.argv[3])]:
    if service not in services:
        raise SystemExit(f'FAIL: canonical service missing: {service}')
    live=[]
    for line in open(mountfile):
        p=[x.strip() for x in line.split('|')]
        if len(p)>=4 and p[0]=='volume': live.append((p[1],p[3]))
    if not live:
        raise SystemExit(f'FAIL: no persistent Docker volume on live {service}')
    desired=services[service].get('volumes') or []
    targets=set()
    for v in desired:
        if isinstance(v,str):
            parts=v.split(':')
            if len(parts)>=2: targets.add(parts[1])
        elif isinstance(v,dict):
            if v.get('type','volume')=='volume' and v.get('target'): targets.add(v['target'])
    for name,target in live:
        if target not in targets:
            raise SystemExit(f'FAIL: canonical {service} does not preserve live volume target {target}')
        print(f'PERSISTENCE_PREFLIGHT|{service}|volume={name}|target={target}')
print('CANONICAL_PERSISTENCE_PREFLIGHT=PASS')
PY

# Prevent accidental project/name drift: canonical project must resolve to desifaces.
PROJECT="$(docker compose --env-file "$ENV" -f "$BASE" -f "$OVERLAY" config --format json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("name") or "")')"
[[ "$PROJECT" == "desifaces" ]] || fail "canonical compose project is '$PROJECT', expected desifaces"
echo "CANONICAL_COMPOSE_PROJECT=desifaces"

# ---------------------------------------------------------------------------
# 3. Recreate ONLY DB + Redis from canonical compose. No volume deletion.
# ---------------------------------------------------------------------------
echo
echo "===== 3. RETIRE DB / REDIS LEGACY COMPOSE ORIGIN ====="
docker compose --env-file "$ENV" -f "$BASE" -f "$OVERLAY" up -d --no-deps --force-recreate desifaces-db desifaces-redis

# Wait for DB health and Redis running.
for i in $(seq 1 60); do
  db_health="$(docker inspect desifaces-db --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' 2>/dev/null || true)"
  redis_state="$(docker inspect desifaces-redis --format '{{.State.Status}}' 2>/dev/null || true)"
  [[ "$db_health" == healthy && "$redis_state" == running ]] && break
  sleep 2
done
[[ "$(docker inspect desifaces-db --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}')" == healthy ]] || fail "DB not healthy after canonical recreation"
[[ "$(docker inspect desifaces-redis --format '{{.State.Status}}')" == running ]] || fail "Redis not running after canonical recreation"

# ---------------------------------------------------------------------------
# 4. Exact persistence/network/port identity certification.
# ---------------------------------------------------------------------------
echo
echo "===== 4. PERSISTENCE IDENTITY CERTIFICATION ====="
for c in desifaces-db desifaces-redis; do
  docker inspect "$c" --format '{{range .Mounts}}{{println .Type "|" .Name "|" .Source "|" .Destination}}{{end}}' | sort > "$BACKUP/$c.mounts.after"
  docker inspect "$c" --format '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' | sort > "$BACKUP/$c.networks.after"
  docker inspect "$c" --format '{{json .HostConfig.PortBindings}}' > "$BACKUP/$c.ports.after"
  diff -u "$BACKUP/$c.mounts.before" "$BACKUP/$c.mounts.after" || fail "$c persistent mounts changed"
  diff -u "$BACKUP/$c.networks.before" "$BACKUP/$c.networks.after" || fail "$c network membership changed"
  diff -u "$BACKUP/$c.ports.before" "$BACKUP/$c.ports.after" || fail "$c port bindings changed"
  origin="$(docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
  configs="$(docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')"
  echo "INFRA_ORIGIN|$c|working_dir=$origin|config=$configs"
  [[ "$origin" == "$ROOT" ]] || fail "$c still has non-canonical compose origin"
  [[ "$configs" != *"$LEGACY"* ]] || fail "$c config_files still reference legacy"
done

echo "DB_PERSISTENCE_IDENTITY=PASS"
echo "REDIS_PERSISTENCE_IDENTITY=PASS"
echo "DB_REDIS_CANONICAL_ORIGIN=PASS"

# ---------------------------------------------------------------------------
# 5. Customer data + application + public web certification.
# ---------------------------------------------------------------------------
echo
echo "===== 5. DATA + PLATFORM CERTIFICATION ====="
DB_USER="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_DB"')"
: > "$BACKUP/customer-counts.after"
while IFS='|' read -r t before; do
  after="$(docker exec desifaces-db psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -c "select count(*) from $t" | tr -d '[:space:]')"
  echo "$t|$after" >> "$BACKUP/customer-counts.after"
  [[ "$before" == "$after" ]] || fail "customer count changed: $t before=$before after=$after"
done < "$BACKUP/customer-counts.before"
echo "CUSTOMER_DATA_PRESERVED=PASS"

for spec in 'core|http://127.0.0.1:8000/api/health' 'audio|http://127.0.0.1:8004/api/health' 'pricing|http://127.0.0.1:8009/api/health'; do
  n="${spec%%|*}"; u="${spec#*|}"
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 "$u" || true)"
  echo "HEALTH|$n|$code"
  [[ "$code" == 200 ]] || fail "$n health failed"
done
web="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 https://web.desifaces.ai/auth/login || true)"
echo "PUBLIC_WEB_HTTP=$web"
[[ "$web" == 200 ]] || fail "public web failed"

# ---------------------------------------------------------------------------
# 6. Final zero-reference gate for legacy workspace.
# ---------------------------------------------------------------------------
echo
echo "===== 6. LEGACY ZERO-REFERENCE GATE ====="
mount_refs=0; origin_refs=0; process_refs=0; config_refs=0; symlink_refs=0
for c in $(docker ps --format '{{.Names}}'); do
  if docker inspect "$c" --format '{{range .Mounts}}{{println .Source}}{{end}}' 2>/dev/null | grep -Fq "$LEGACY"; then
    echo "LEGACY_MOUNT_REF=$c"; mount_refs=$((mount_refs+1))
  fi
  wd="$(docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
  cf="$(docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' 2>/dev/null || true)"
  if [[ "$wd$cf" == *"$LEGACY"* ]]; then echo "LEGACY_ORIGIN_REF=$c|$wd|$cf"; origin_refs=$((origin_refs+1)); fi
done

while IFS= read -r line; do [[ -n "$line" ]] && { echo "LEGACY_PROCESS_REF=$line"; process_refs=$((process_refs+1)); }; done < <(ps -eo pid=,args= | grep -F "$LEGACY" | grep -v -E 'grep -F|bash -s' || true)
while IFS= read -r line; do [[ -n "$line" ]] && { echo "LEGACY_CONFIG_REF=$line"; config_refs=$((config_refs+1)); }; done < <(sudo grep -RIlF "$LEGACY" /etc/nginx /etc/systemd/system /etc/cron.d /etc/cron.daily /etc/cron.hourly 2>/dev/null || true)
while IFS= read -r line; do [[ -n "$line" ]] && { echo "LEGACY_SYMLINK_REF=$line"; symlink_refs=$((symlink_refs+1)); }; done < <(find /home/azureuser/workspace -xdev -type l -lname "$LEGACY*" -print 2>/dev/null || true)

echo "LEGACY_MOUNT_REFERENCE_COUNT=$mount_refs"
echo "LEGACY_ORIGIN_REFERENCE_COUNT=$origin_refs"
echo "LEGACY_PROCESS_REFERENCE_COUNT=$process_refs"
echo "LEGACY_CONFIG_REFERENCE_COUNT=$config_refs"
echo "LEGACY_SYMLINK_REFERENCE_COUNT=$symlink_refs"

total=$((mount_refs+origin_refs+process_refs+config_refs+symlink_refs))
if (( total == 0 )); then
  echo "V2_RETIREMENT_READY=YES"
else
  echo "V2_RETIREMENT_READY=NO"
fi

# ---------------------------------------------------------------------------
# 7. Archive legacy workspace only when zero references exist. Do not delete it.
# ---------------------------------------------------------------------------
echo
echo "===== 7. LEGACY WORKSPACE ARCHIVE ====="
if (( total == 0 )); then
  DEST="/home/azureuser/backups/desifaces-v2-retired-$STAMP"
  [[ -d "$LEGACY" ]] || fail "legacy workspace unexpectedly absent"
  [[ ! -e "$DEST" ]] || fail "archive destination already exists"
  mv "$LEGACY" "$DEST"
  [[ ! -e "$LEGACY" && -d "$DEST" ]] || fail "legacy archive move failed"
  echo "LEGACY_ARCHIVE=$DEST"
  echo "LEGACY_WORKSPACE_RETIRED=PASS"
else
  echo "LEGACY_WORKSPACE_RETIRED=SKIPPED_REFERENCES_REMAIN"
fi

echo "============================================================"
echo " FINAL PRODUCTION LAYOUT RESULT"
echo "============================================================"
echo "BACKEND_ROOT=$ROOT"
echo "WEB_ROOT=/home/azureuser/workspace/desifaces-web"
echo "DB_REDIS_CANONICAL_ORIGIN=PASS"
echo "CUSTOMER_DATA_PRESERVED=PASS"
echo "PUBLIC_WEB=PASS"
if (( total == 0 )); then
  echo "LEGACY_RUNTIME_DEPENDENCY=NONE"
  echo "CLEAN_PRODUCTION_LAYOUT=PASS"
else
  echo "LEGACY_RUNTIME_DEPENDENCY=BLOCKED_BY_REFERENCE"
  echo "CLEAN_PRODUCTION_LAYOUT=PHASE_2_PASS_ARCHIVE_PENDING"
fi
REMOTE
