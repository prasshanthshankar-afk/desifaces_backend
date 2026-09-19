#!/usr/bin/env bash
set -Eeuo pipefail

PROD_HOST="${PROD_HOST:-desifaces-gpu}"
DEV_HOST="${DEV_HOST:-desifaces-dev}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${OUT:-/tmp/desifaces-next2-runtime-reconstruction-${STAMP}}"
mkdir -p "$OUT/prod" "$OUT/dev"

fail(){ echo "BLOCKER: $*" >&2; exit 2; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing local command: $1"; }
need ssh
need python3
need tar

printf '%s\n' \
  '============================================================' \
  ' DESIFACES #next2 — PROD RUNTIME RECONSTRUCTION' \
  " prod=${PROD_HOST}" \
  " dev=${DEV_HOST}" \
  ' prod_mutation=NONE' \
  ' dev_mutation=NONE' \
  '============================================================'

collect_host(){
  local host="$1" role="$2" text_out="$3" archive_out="$4"

  echo "===== ${role} RUNTIME CAPTURE ====="
  ssh -o BatchMode=yes -o ConnectTimeout=12 "$host" "ROLE='$role' bash -s" >"$text_out" <<'REMOTE'
set -Eeuo pipefail
ROLE="${ROLE:?ROLE missing}"
TMP="/tmp/next2-${ROLE,,}-runtime"
rm -rf "$TMP"
mkdir -p "$TMP/manifests"

printf 'HOST=%s\n' "$(hostname -s)"
printf 'ROLE=%s\n' "$ROLE"

echo '===== GIT DISCOVERY ====='
if [[ -d "$HOME/workspace" ]]; then
  find "$HOME/workspace" -maxdepth 5 -type d -name .git -print0 2>/dev/null |
  while IFS= read -r -d '' G; do
    R="${G%/.git}"
    O="$(git -C "$R" remote get-url origin 2>/dev/null || true)"
    case "$O" in
      *desifaces_backend*|*desifaces_web*)
        H="$(git -C "$R" rev-parse HEAD 2>/dev/null || true)"
        B="$(git -C "$R" branch --show-current 2>/dev/null || true)"
        D="$(git -C "$R" status --porcelain 2>/dev/null || true)"
        printf 'repo=%s\norigin=%s\nhead=%s\nbranch=%s\ndirty=%s\n---\n' \
          "$R" "$O" "$H" "$B" "$([[ -n "$D" ]] && echo YES || echo NO)"
        ;;
    esac
  done
fi

echo '===== SHARED APPLICATION CONTAINERS ====='
docker ps --format '{{.Names}}' |
grep -E '(^|-)svc-(assistant|audio|core|dashboard|director|face|fusion|pricing)(-|$)|web|stitch-worker' |
sort |
while read -r C; do
  [[ -n "$C" ]] || continue

  IMAGE="$(docker inspect -f '{{.Config.Image}}' "$C")"
  IMAGE_ID="$(docker inspect -f '{{.Image}}' "$C")"
  STATE="$(docker inspect -f '{{.State.Status}}' "$C")"
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$C")"
  PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$C" 2>/dev/null || true)"
  SERVICE="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$C" 2>/dev/null || true)"
  WORKDIR="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$C" 2>/dev/null || true)"
  CONFIG_FILES="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$C" 2>/dev/null || true)"

  printf 'container=%s\nimage=%s\nimage_id=%s\nstate=%s\nhealth=%s\n' \
    "$C" "$IMAGE" "$IMAGE_ID" "$STATE" "$HEALTH"
  printf 'compose_project=%s\ncompose_service=%s\ncompose_workdir=%s\ncompose_files=%s\n' \
    "$PROJECT" "$SERVICE" "$WORKDIR" "$CONFIG_FILES"

  docker exec "$C" sh -lc '
    set -e
    for ROOT in /app/app /app/desifaces_shared /app/src; do
      if [ -d "$ROOT" ]; then
        find "$ROOT" -type f \
          \( -name "*.py" -o -name "*.js" -o -name "*.jsx" -o -name "*.ts" -o -name "*.tsx" -o -name "*.json" \) \
          ! -path "*/__pycache__/*" \
          ! -path "*/node_modules/*" \
          ! -path "*/.next/*" \
          -print0
      fi
    done | sort -z | xargs -0 -r sha256sum
  ' >"$TMP/manifests/${C}.sha256" 2>/dev/null || true

  COUNT="$(wc -l <"$TMP/manifests/${C}.sha256" | tr -d ' ')"
  HASH="$(sha256sum "$TMP/manifests/${C}.sha256" | awk '{print $1}')"
  printf 'runtime_files=%s\nruntime_manifest_sha=%s\n' "$COUNT" "$HASH"

  echo 'behavior_env:'
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$C" |
    grep -E '^(DF_.*(MODEL|PROVIDER|ENABLED|CONCURRENCY|BATCH|POLL|REVISION)=|STITCH_WORKER_ENABLED=|WORKER_ENABLED=|FUSION_RECOVERY_ENABLED=)' |
    sed -E 's/^([^=]*(SECRET|TOKEN|PASSWORD|KEY)[^=]*)=.*/\1=<redacted>/' |
    sort || true
  echo '---'
done

echo '===== DB SCHEMA FINGERPRINT THROUGH APPLICATION CONNECTION ====='
C="$(docker ps --format '{{.Names}}' | grep -E 'svc-core$|svc-pricing$|svc-director$' | head -1 || true)"
if [[ -z "$C" ]]; then
  echo 'DB_SCHEMA_CAPTURE=BLOCKER_NO_APPLICATION_CONTAINER'
else
  docker exec -i "$C" python - <<'PY'
import asyncio, hashlib, os
try:
    import asyncpg
except Exception:
    print('DB_SCHEMA_CAPTURE=BLOCKER_ASYNCPG_UNAVAILABLE')
    raise SystemExit(0)

dsn=(os.getenv('DATABASE_URL') or os.getenv('DF_DATABASE_URL') or os.getenv('POSTGRES_DSN') or '').strip()
dsn=dsn.replace('postgresql+asyncpg://','postgresql://',1)
if not dsn:
    print('DB_SCHEMA_CAPTURE=BLOCKER_DSN_MISSING')
    raise SystemExit(0)

async def main():
    conn=await asyncpg.connect(dsn)
    rows=await conn.fetch('''
      select table_schema, table_name, column_name, data_type, is_nullable,
             coalesce(column_default, '') as column_default
      from information_schema.columns
      where table_schema not in ('pg_catalog','information_schema')
      order by table_schema, table_name, ordinal_position
    ''')
    normalized='\n'.join('|'.join(str(x) for x in row.values()) for row in rows)
    tables=await conn.fetchval('''
      select count(distinct table_schema || '.' || table_name)
      from information_schema.tables
      where table_schema not in ('pg_catalog','information_schema')
    ''')
    print('DB_SCHEMA_CAPTURE=PASS')
    print('DB_SCHEMA_SHA256='+hashlib.sha256(normalized.encode()).hexdigest())
    print('DB_TABLE_COUNT='+str(tables))
    await conn.close()

asyncio.run(main())
PY
fi

tar -C "$TMP" -czf "/tmp/next2-${ROLE,,}-runtime.tgz" .
sha256sum "/tmp/next2-${ROLE,,}-runtime.tgz"
printf 'RUNTIME_CAPTURE_%s=PASS\n' "$ROLE"
REMOTE

  cat "$text_out"
  ssh -o BatchMode=yes -o ConnectTimeout=12 "$host" "cat /tmp/next2-${role,,}-runtime.tgz" >"$archive_out"
  tar -xzf "$archive_out" -C "$OUT/${role,,}"
}

collect_host "$PROD_HOST" PROD "$OUT/prod.txt" "$OUT/prod-runtime.tgz"
collect_host "$DEV_HOST" DEV "$OUT/dev.txt" "$OUT/dev-runtime.tgz"

echo '===== CRITICAL LIVE RUNTIME DIFF ====='
python3 - "$OUT" <<'PY'
from pathlib import Path
import re, sys
root=Path(sys.argv[1])
prod=root/'prod'/'manifests'
dev=root/'dev'/'manifests'

def normalize(name):
    s=name
    for prefix in ('df-v3-','df-','desifaces-v3-','desifaces-'):
        if s.startswith(prefix):
            s=s[len(prefix):]
    s=re.sub(r'-(prod|production|dev|development)$','',s)
    if s in ('v3-web','web-prod','web-production'): s='web'
    return s

def manifests(path):
    out={}
    for p in path.glob('*.sha256'):
        key=normalize(p.stem)
        # Prefer exact service-ish names over accidental auxiliary web containers.
        if key not in out or len(p.stem) < len(out[key].stem): out[key]=p
    return out

def read(path):
    result={}
    for line in path.read_text(errors='ignore').splitlines():
        if '  ' not in line: continue
        digest,fn=line.split('  ',1)
        result[fn]=digest
    return result

pfiles=manifests(prod); dfiles=manifests(dev)
critical={
 'svc-assistant','svc-audio','svc-audio-worker','svc-core','svc-dashboard',
 'svc-director','svc-director-worker','svc-face','svc-face-worker',
 'svc-fusion','svc-fusion-worker','svc-fusion-extension',
 'svc-fusion-extension-worker','svc-fusion-extension-stitch-worker',
 'svc-pricing','web'
}
block=0; drift=0; match=0
for service in sorted(critical):
    p=pfiles.get(service); d=dfiles.get(service)
    if not p or not d:
        print(f'[BLOCKER] {service}: prod_manifest={bool(p)} dev_manifest={bool(d)}')
        block+=1; continue
    pm,dm=read(p),read(d)
    changed=sorted(fn for fn in set(pm)&set(dm) if pm[fn]!=dm[fn])
    prod_only=sorted(set(pm)-set(dm)); dev_only=sorted(set(dm)-set(pm))
    if not changed and not prod_only and not dev_only:
        print(f'[MATCH] {service}')
        match+=1; continue
    drift+=1
    print(f'[DRIFT] {service} changed={len(changed)} prod_only={len(prod_only)} dev_only={len(dev_only)}')
    for fn in changed[:30]: print('  CHANGED '+fn)
    for fn in prod_only[:30]: print('  PROD_ONLY '+fn)
    for fn in dev_only[:30]: print('  DEV_ONLY '+fn)

print(f'CRITICAL_RUNTIME_MATCH={match}')
print(f'CRITICAL_RUNTIME_DRIFT={drift}')
print(f'CRITICAL_RUNTIME_BLOCKER={block}')
if block: print('NEXT2_RECONSTRUCTION=BLOCKED')
elif drift: print('NEXT2_RECONSTRUCTION=READY_FOR_DEV_RECONCILIATION')
else: print('NEXT2_RECONSTRUCTION=ALREADY_MATCHED')
PY

echo '===== DB COMPARISON ====='
PROD_DB="$(grep '^DB_SCHEMA_SHA256=' "$OUT/prod.txt" | tail -1 | cut -d= -f2- || true)"
DEV_DB="$(grep '^DB_SCHEMA_SHA256=' "$OUT/dev.txt" | tail -1 | cut -d= -f2- || true)"
printf 'prod_db_schema=%s\ndev_db_schema=%s\n' "${PROD_DB:-UNAVAILABLE}" "${DEV_DB:-UNAVAILABLE}"
if [[ -n "$PROD_DB" && -n "$DEV_DB" ]]; then
  if [[ "$PROD_DB" == "$DEV_DB" ]]; then echo 'DB_SCHEMA=MATCH'; else echo 'DB_SCHEMA=DRIFT_REQUIRING_CORRECTION'; fi
else
  echo 'DB_SCHEMA=BLOCKER'
fi

echo '===== FINAL ====='
echo 'PROD_TOUCH=NONE'
echo 'DEV_TOUCH=NONE'
echo "EVIDENCE_DIR=$OUT"
echo 'NEXT2_RUNTIME_RECONSTRUCTION=COMPLETE'
