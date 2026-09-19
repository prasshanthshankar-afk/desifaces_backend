#!/usr/bin/env bash
set -Eeuo pipefail

PROD_HOST="${PROD_HOST:-desifaces-gpu}"
DEV_HOST="${DEV_HOST:-desifaces-dev}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${OUT:-/tmp/desifaces-next2-apply-${STAMP}}"
DEV_STATE="/home/azureuser/.local/state/desifaces-next2/${STAMP}"
mkdir -p "$OUT/prod" "$OUT/dev"

fail(){ echo "BLOCKER: $*" >&2; exit 2; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing local command: $1"; }
need ssh
need python3
need tar

printf '%s\n' \
  '============================================================' \
  ' DESIFACES #next2 — PROD -> DEV V3 RUNTIME RECONCILIATION' \
  " prod=${PROD_HOST}" \
  " dev=${DEV_HOST}" \
  ' prod_mutation=NONE' \
  ' target=DEV_V3_ONLY' \
  " stamp=${STAMP}" \
  '============================================================'

MAPPINGS='
assistant|df-v3-svc-assistant|df-v3-svc-assistant
audio|df-svc-audio|df-v3-svc-audio
audio-worker|df-svc-audio-worker|df-v3-svc-audio-worker
core|df-svc-core|df-v3-svc-core
dashboard|df-svc-dashboard|df-v3-svc-dashboard
dashboard-worker|df-svc-dashboard-worker|df-v3-svc-dashboard-worker
director|df-v3-svc-director|df-v3-svc-director
director-worker|df-v3-svc-director-worker|df-v3-svc-director-worker
face|df-svc-face|df-v3-svc-face
face-worker|df-svc-face-worker|df-v3-svc-face-worker
fusion|df-svc-fusion|df-v3-svc-fusion
fusion-worker|df-svc-fusion-worker|df-v3-svc-fusion-worker
fusion-extension|df-svc-fusion-extension|df-v3-svc-fusion-extension
fusion-extension-worker|df-svc-fusion-extension-worker|df-v3-svc-fusion-extension-worker
fusion-extension-stitch-worker|df-svc-fusion-extension-stitch-worker|df-v3-svc-fusion-extension-stitch-worker
pricing|df-svc-pricing|df-v3-svc-pricing
'

remote_container_exists(){
  local host="$1" c="$2"
  ssh -o BatchMode=yes -o ConnectTimeout=12 "$host" "docker inspect '$c' >/dev/null 2>&1"
}

runtime_manifest(){
  local host="$1" c="$2" out="$3"
  ssh -o BatchMode=yes -o ConnectTimeout=12 "$host" "C='$c' sh -s" >"$out" <<'REMOTE'
set -Eeuo pipefail
docker exec "$C" sh -lc '
  set -e
  for ROOT in /app/app /app/desifaces_shared /app/src; do
    if [ -d "$ROOT" ]; then
      find "$ROOT" -type f \
        \( -name "*.py" -o -name "*.js" -o -name "*.jsx" -o -name "*.ts" -o -name "*.tsx" -o -name "*.json" \) \
        ! -name "._*" \
        ! -path "*/._*" \
        ! -path "*/__pycache__/*" \
        ! -path "*/node_modules/*" \
        ! -path "*/.next/*" \
        -print0
    fi
  done | sort -z | xargs -0 -r sha256sum
'
REMOTE
}

runtime_bundle(){
  local host="$1" c="$2" out="$3"
  ssh -o BatchMode=yes -o ConnectTimeout=12 "$host" "C='$c' sh -s" >"$out" <<'REMOTE'
set -Eeuo pipefail
docker exec "$C" sh -lc '
  set -e
  cd /app
  set --
  [ -d app ] && set -- "$@" app
  [ -d desifaces_shared ] && set -- "$@" desifaces_shared
  [ -d src ] && set -- "$@" src
  [ "$#" -gt 0 ]
  tar \
    --exclude="._*" \
    --exclude="*/._*" \
    --exclude="*/__pycache__/*" \
    --exclude="*.pyc" \
    --exclude="*/node_modules/*" \
    --exclude="*/.next/*" \
    -czf - "$@"
'
REMOTE
}

echo '===== 1. HOST / CONTAINER PREFLIGHT ====='
while IFS='|' read -r svc prod_c dev_c; do
  [[ -n "${svc:-}" ]] || continue
  remote_container_exists "$PROD_HOST" "$prod_c" || fail "PROD container missing: $prod_c"
  remote_container_exists "$DEV_HOST" "$dev_c" || fail "DEV V3 container missing: $dev_c"
  echo "PREFLIGHT_CONTAINER=PASS service=$svc prod=$prod_c dev=$dev_c"
done <<<"$MAPPINGS"

echo '===== 2. DB COMPATIBILITY GATE ====='
capture_schema(){
  local host="$1" outfile="$2"
  ssh -o BatchMode=yes -o ConnectTimeout=12 "$host" "python3 -" >"$outfile" <<'PY'
import json, subprocess
names=subprocess.check_output(["docker","ps","--format","{{.Names}}"], text=True).splitlines()
preferred=[n for n in names if n.endswith("svc-core") or n.endswith("svc-pricing") or n.endswith("svc-director")]
if not preferred:
    print(json.dumps({"error":"no application container"})); raise SystemExit(0)
preferred.sort(key=lambda n:(0 if "v3" in n else 1, n))
c=preferred[0]
code='''
import asyncio, json, os
import asyncpg
dsn=(os.getenv("DATABASE_URL") or os.getenv("DF_DATABASE_URL") or os.getenv("POSTGRES_DSN") or "").strip()
dsn=dsn.replace("postgresql+asyncpg://","postgresql://",1)
if not dsn:
    print(json.dumps({"error":"dsn missing"})); raise SystemExit(0)
async def main():
    conn=await asyncpg.connect(dsn)
    rows=await conn.fetch("""
      select table_schema, table_name, column_name, data_type, udt_name, is_nullable
      from information_schema.columns
      where table_schema not in ('pg_catalog','information_schema')
      order by table_schema, table_name, ordinal_position
    """)
    tables=await conn.fetch("""
      select table_schema, table_name
      from information_schema.tables
      where table_schema not in ('pg_catalog','information_schema')
      order by table_schema, table_name
    """)
    print(json.dumps({
      "columns":[list(r.values()) for r in rows],
      "tables":[list(r.values()) for r in tables]
    }))
    await conn.close()
asyncio.run(main())
'''
p=subprocess.run(["docker","exec","-i",c,"python","-"], input=code, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
if p.returncode:
    print(json.dumps({"error":"schema query failed","detail":p.stderr[-500:]}))
else:
    print(p.stdout.strip())
PY
}

capture_schema "$PROD_HOST" "$OUT/prod-schema.json"
capture_schema "$DEV_HOST" "$OUT/dev-schema.json"

python3 - "$OUT/prod-schema.json" "$OUT/dev-schema.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1])); d=json.load(open(sys.argv[2]))
if p.get("error") or d.get("error"):
    raise SystemExit(f"BLOCKER: schema capture failed prod={p.get('error')} dev={d.get('error')}")
pt={tuple(x) for x in p["tables"]}; dt={tuple(x) for x in d["tables"]}
missing_tables=sorted(pt-dt)
pc={(x[0],x[1],x[2]):tuple(x[3:]) for x in p["columns"]}
dc={(x[0],x[1],x[2]):tuple(x[3:]) for x in d["columns"]}
missing_cols=sorted(k for k in pc if k not in dc)
incompat=sorted((k,pc[k],dc[k]) for k in pc if k in dc and pc[k]!=dc[k])
extra_tables=sorted(dt-pt)
extra_cols=sorted(k for k in dc if k not in pc)
print(f"DB_PROD_TABLES={len(pt)}")
print(f"DB_DEV_TABLES={len(dt)}")
print(f"DB_DEV_EXTRA_TABLES={len(extra_tables)}")
print(f"DB_DEV_EXTRA_COLUMNS={len(extra_cols)}")
if missing_tables or missing_cols or incompat:
    print(f"DB_MISSING_PROD_TABLES={len(missing_tables)}")
    print(f"DB_MISSING_PROD_COLUMNS={len(missing_cols)}")
    print(f"DB_INCOMPATIBLE_COLUMNS={len(incompat)}")
    for x in missing_tables[:20]: print("  MISSING_TABLE "+ ".".join(x))
    for x in missing_cols[:20]: print("  MISSING_COLUMN "+ ".".join(x))
    for k,a,b in incompat[:20]: print("  INCOMPATIBLE_COLUMN "+".".join(k)+f" prod={a} dev={b}")
    raise SystemExit("BLOCKER: DEV schema is not a compatible superset of PROD")
print("DB_SCHEMA_COMPATIBILITY=PASS_DEV_SUPERSET")
print("DB_SCHEMA_MUTATION=NONE")
PY

echo '===== 3. CAPTURE ALL PROD BUNDLES + DEV BACKUPS BEFORE MUTATION ====='
DRIFT_FILE="$OUT/drift.txt"
: >"$DRIFT_FILE"

while IFS='|' read -r svc prod_c dev_c; do
  [[ -n "${svc:-}" ]] || continue
  runtime_manifest "$PROD_HOST" "$prod_c" "$OUT/prod/${svc}.manifest"
  runtime_manifest "$DEV_HOST" "$dev_c" "$OUT/dev/${svc}.manifest"
  runtime_bundle "$PROD_HOST" "$prod_c" "$OUT/prod/${svc}.tgz"
  runtime_bundle "$DEV_HOST" "$dev_c" "$OUT/dev/${svc}.tgz"

  if cmp -s "$OUT/prod/${svc}.manifest" "$OUT/dev/${svc}.manifest"; then
    echo "RUNTIME_MATCH service=$svc"
  else
    echo "$svc|$prod_c|$dev_c" >>"$DRIFT_FILE"
    PCOUNT="$(wc -l <"$OUT/prod/${svc}.manifest" | tr -d ' ')"
    DCOUNT="$(wc -l <"$OUT/dev/${svc}.manifest" | tr -d ' ')"
    echo "RUNTIME_DRIFT service=$svc prod_files=$PCOUNT dev_files=$DCOUNT"
  fi
done <<<"$MAPPINGS"

DRIFT_COUNT="$(wc -l <"$DRIFT_FILE" | tr -d ' ')"
echo "DEV_V3_RUNTIME_DRIFT_COUNT=$DRIFT_COUNT"

echo '===== 4. CREATE RESTART-SAFE DEV STATE + BACKUPS ====='
ssh "$DEV_HOST" "mkdir -p '$DEV_STATE/backups' '$DEV_STATE/prod-baseline' '$DEV_STATE/logs'; printf 'STATUS=IN_PROGRESS\nSTAMP=$STAMP\n' > '$DEV_STATE/status.env'; : > '$DEV_STATE/applied.txt'"

while IFS='|' read -r svc prod_c dev_c; do
  [[ -n "${svc:-}" ]] || continue
  cat "$OUT/dev/${svc}.tgz" | ssh "$DEV_HOST" "cat > '$DEV_STATE/backups/${svc}.tgz'"
  cat "$OUT/prod/${svc}.tgz" | ssh "$DEV_HOST" "cat > '$DEV_STATE/prod-baseline/${svc}.tgz'"
done <<<"$MAPPINGS"

cat "$DRIFT_FILE" | ssh "$DEV_HOST" "cat > '$DEV_STATE/drift.txt'"

rollback_dev(){
  echo '===== ROLLBACK DEV ====='
  ssh "$DEV_HOST" "STATE='$DEV_STATE' bash -s" <<'REMOTE'
set -Eeuo pipefail
if [[ ! -f "$STATE/applied.txt" ]]; then exit 0; fi
tac "$STATE/applied.txt" 2>/dev/null | while IFS='|' read -r svc c; do
  [[ -n "${c:-}" ]] || continue
  echo "ROLLBACK service=$svc container=$c"
  docker cp "$STATE/backups/${svc}.tgz" "$c:/tmp/next2-rollback.tgz"
  docker exec "$c" sh -lc '
    set -e
    rm -rf /app/app /app/desifaces_shared /app/src
    mkdir -p /app
    tar -xzf /tmp/next2-rollback.tgz -C /app
    rm -f /tmp/next2-rollback.tgz
  ' || true
  docker restart "$c" >/dev/null 2>&1 || true
done
printf 'STATUS=ROLLED_BACK\n' > "$STATE/status.env"
echo 'DEV_ROLLBACK=COMPLETE'
REMOTE
}

trap 'rc=$?; if [[ $rc -ne 0 ]]; then rollback_dev || true; fi; exit $rc' EXIT

echo '===== 5. APPLY PROD RUNTIME TO DRIFTED DEV V3 CONTAINERS ====='
while IFS='|' read -r svc prod_c dev_c; do
  [[ -n "${svc:-}" ]] || continue

  echo "APPLY service=$svc target=$dev_c"
  ssh "$DEV_HOST" "STATE='$DEV_STATE' SVC='$svc' C='$dev_c' bash -s" <<'REMOTE'
set -Eeuo pipefail
printf '%s|%s\n' "$SVC" "$C" >> "$STATE/applied.txt"
docker cp "$STATE/prod-baseline/${SVC}.tgz" "$C:/tmp/next2-prod-runtime.tgz"
docker exec "$C" sh -lc '
  set -e
  rm -rf /app/app /app/desifaces_shared /app/src
  mkdir -p /app
  tar -xzf /tmp/next2-prod-runtime.tgz -C /app
  rm -f /tmp/next2-prod-runtime.tgz
  for P in /app/app /app/desifaces_shared /app/src; do
    [ ! -d "$P" ] || python -m compileall -q "$P"
  done
  if [ -f /app/app/main.py ]; then
    PYTHONDONTWRITEBYTECODE=1 python - <<'"'"'PY'"'"'
import importlib
importlib.import_module("app.main")
print("APP_IMPORT=PASS")
PY
  fi
'
docker restart "$C" >/dev/null

state=''; health=''
for _ in $(seq 1 40); do
  state="$(docker inspect -f '{{.State.Status}}' "$C" 2>/dev/null || true)"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$C" 2>/dev/null || true)"
  if [[ "$state" == running && ( "$health" == healthy || "$health" == no-healthcheck ) ]]; then break; fi
  sleep 3
done
[[ "$state" == running ]] || { docker logs --tail 100 "$C" >&2 || true; exit 31; }
[[ "$health" == healthy || "$health" == no-healthcheck ]] || { docker logs --tail 100 "$C" >&2 || true; exit 32; }
echo "SERVICE_HEALTH=PASS service=$SVC state=$state health=$health"
REMOTE

done <"$DRIFT_FILE"

echo '===== 6. POST-APPLY RUNTIME PARITY ====='
PARITY_FAIL=0
while IFS='|' read -r svc prod_c dev_c; do
  [[ -n "${svc:-}" ]] || continue
  runtime_manifest "$PROD_HOST" "$prod_c" "$OUT/prod/${svc}.post.manifest"
  runtime_manifest "$DEV_HOST" "$dev_c" "$OUT/dev/${svc}.post.manifest"
  if cmp -s "$OUT/prod/${svc}.post.manifest" "$OUT/dev/${svc}.post.manifest"; then
    echo "POST_SYNC_MATCH service=$svc"
  else
    echo "POST_SYNC_DRIFT service=$svc"
    PARITY_FAIL=1
  fi
done <<<"$MAPPINGS"
[[ "$PARITY_FAIL" == 0 ]] || fail "post-sync backend runtime parity failed"

echo '===== 7. CERTIFY GENERATION CONTRACTS ====='
ssh "$DEV_HOST" "bash -s" <<'REMOTE'
set -Eeuo pipefail

check(){
  local c="$1"
  state="$(docker inspect -f '{{.State.Status}}' "$c")"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$c")"
  [[ "$state" == running ]]
  [[ "$health" == healthy || "$health" == no-healthcheck ]]
  echo "HEALTH_PASS=$c state=$state health=$health"
}

for c in \
  df-v3-svc-assistant \
  df-v3-svc-audio df-v3-svc-audio-worker \
  df-v3-svc-core \
  df-v3-svc-dashboard df-v3-svc-dashboard-worker \
  df-v3-svc-director df-v3-svc-director-worker \
  df-v3-svc-face df-v3-svc-face-worker \
  df-v3-svc-fusion df-v3-svc-fusion-worker \
  df-v3-svc-fusion-extension df-v3-svc-fusion-extension-worker \
  df-v3-svc-fusion-extension-stitch-worker \
  df-v3-svc-pricing
do check "$c"; done

docker exec -i df-v3-svc-fusion-extension python - <<'PY'
from app.main import app
paths={getattr(r,'path','') for r in app.routes}
required={
 '/api/longform/v3/scene-pricing/preview',
 '/api/longform/v3/scene-pricing/reserve',
 '/api/longform/v3/scene-pricing/commit',
 '/api/longform/v3/scene-pricing/release',
 '/api/longform/v3/scene-stitch',
 '/api/longform/v3/story-stitch',
}
missing=sorted(required-paths)
assert not missing, missing
print('V3_FUSION_EXTENSION_CONTRACT=PASS')
PY

docker exec -i df-v3-svc-face python - <<'PY'
from app.main import app
paths={getattr(r,'path','') for r in app.routes}
assert '/api/face/assets/{media_id}/read-url' in paths
print('V3_FACE_CANONICAL_ASSET_CONTRACT=PASS')
PY

docker exec -i df-v3-svc-fusion-extension-stitch-worker python - <<'PY'
import os
assert (os.getenv('STITCH_WORKER_ENABLED') or '').lower() == 'true'
assert (os.getenv('DF_V3_SCENE_COORDINATOR_ENABLED') or '').lower() == 'true'
print('V3_STITCH_WORKER_ENABLED=PASS')
print('V3_SCENE_COORDINATOR_ENABLED=PASS')
PY

echo 'DEV_GENERATION_RUNTIME_CERTIFICATION=PASS'
REMOTE

echo '===== 8. FREEZE CERTIFIED DEV IMAGES ====='
ssh "$DEV_HOST" "STATE='$DEV_STATE' STAMP='$STAMP' bash -s" <<'REMOTE'
set -Eeuo pipefail
while IFS='|' read -r svc prod_c c; do
  [[ -n "${svc:-}" ]] || continue
  image="desifaces-next2-certified/${svc}:${STAMP}"
  docker commit "$c" "$image" >/dev/null
  printf '%s=%s\n' "$svc" "$image" >> "$STATE/certified-images.env"
  echo "CERTIFIED_IMAGE service=$svc image=$image"
done < "$STATE/drift.txt"
printf 'STATUS=CERTIFIED\n' > "$STATE/status.env"
echo "DEV_STATE_DIR=$STATE"
REMOTE

trap - EXIT

echo '===== FINAL #next2 BACKEND VERDICT ====='
echo "PROD_TOUCH=NONE"
echo "DEV_V3_RUNTIME_PARITY=PASS"
echo "DEV_DB_SCHEMA_COMPATIBILITY=PASS_DEV_SUPERSET"
echo "DEV_DB_SCHEMA_MUTATION=NONE"
echo "DEV_GENERATION_RUNTIME_CERTIFICATION=PASS"
echo "DEV_ROLLBACK_STATE=$DEV_STATE"
echo "NEXT2_BACKEND_SYNC=PASS"
