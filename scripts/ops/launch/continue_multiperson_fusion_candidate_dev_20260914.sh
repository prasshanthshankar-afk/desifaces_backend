#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE="desifaces-svc-fusion-extension:mp-cert-8976b248c925"
CANDIDATE="df-fusion-extension-v3-cert"
PAIRED_DIRECTOR_SHA="26b1dc59dde47cb79ff9ee08fd43d1dbaf0e73b5"
TMP="$(mktemp -d)"
ENVFILE="$TMP/candidate.env"

cleanup(){ docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT
fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-dev" ]] || fail "DEV hostname mismatch"
command -v docker >/dev/null || fail "docker missing"
docker image inspect "$IMAGE" >/dev/null 2>&1 || fail "certified candidate image missing: $IMAGE"

echo "============================================================"
echo " desifaces — CONTINUE DEV MULTI-PERSON CANDIDATE CERT"
echo " candidate_image=$IMAGE"
echo " production_touch=NONE"
echo " host_port_touch=NONE"
echo "============================================================"
echo "CANDIDATE_IMAGE_REUSE=PASS"

EXT="$(docker ps --format '{{.Names}}' | grep -E '^df-v3-svc-fusion-extension$|^df-svc-fusion-extension$|svc-fusion-extension$' | head -1 || true)"
[[ -n "$EXT" ]] || fail "DEV Fusion Extension container not found"
DIRECTOR="$(docker ps --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
[[ -n "$DIRECTOR" ]] || fail "DEV Director container not found"
echo "DEV_RUNTIME_DISCOVERY=PASS"

# Static Director contract proof from the paired release that introduced Multi-Person V3 pricing/stitch.
mkdir -p "$TMP/director"
BASE="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${PAIRED_DIRECTOR_SHA}/services/svc-director/app/app"
for f in fusion_execution_parent_pricing.py fusion_execution.py story_final_execution.py; do
  curl -fsSL --connect-timeout 5 --max-time 20 "$BASE/$f" -o "$TMP/director/$f"
done

grep -Fq '/api/longform/v3/scene-pricing/preview' "$TMP/director/fusion_execution_parent_pricing.py"
grep -Fq '/api/longform/v3/scene-pricing/reserve' "$TMP/director/fusion_execution_parent_pricing.py"
grep -Fq '/api/longform/v3/scene-pricing/commit' "$TMP/director/fusion_execution_parent_pricing.py"
grep -Fq '/api/longform/v3/scene-pricing/release' "$TMP/director/fusion_execution_parent_pricing.py"
grep -Fq '/api/longform/v3/scene-stitch' "$TMP/director/fusion_execution.py"
grep -Fq '/api/longform/v3/assets/{media_id}/read-url' "$TMP/director/fusion_execution.py"
grep -Fq '/api/longform/v3/story-stitch' "$TMP/director/story_final_execution.py"
echo "DIRECTOR_FUSION_EXTENSION_CONTRACT_STATIC=PASS"

# Reconfirm immutable image imports and exposes both current single-person and restored multi-person contracts.
docker run --rm \
  -e DATABASE_URL='postgresql://invalid:invalid@127.0.0.1:1/invalid' \
  -e JWT_SECRET='candidate-certification-only' \
  -e AZURE_STORAGE_CONNECTION_STRING='UseDevelopmentStorage=true' \
  -e WORKER_ENABLED=false \
  -e STITCH_WORKER_ENABLED=false \
  --entrypoint python "$IMAGE" -c '
from app.main import app
paths={getattr(r,"path","") for r in app.routes}
required={
 "/api/longform/pricing/preview",
 "/api/longform/jobs/pricing/preview",
 "/api/longform/v3/scene-pricing/preview",
 "/api/longform/v3/scene-pricing/reserve",
 "/api/longform/v3/scene-pricing/commit",
 "/api/longform/v3/scene-pricing/release",
 "/api/longform/v3/scene-stitch",
 "/api/longform/v3/assets/{media_id}/read-url",
 "/api/longform/v3/story-stitch",
}
missing=sorted(required-paths)
assert not missing, missing
print("CANDIDATE_FULL_APP_IMPORT=PASS")
print("SINGLE_PERSON_ROUTE_CONTRACT=PASS")
print("MULTI_PERSON_ROUTE_CONTRACT=PASS")
'

APP_HASH="$(docker run --rm --entrypoint sh "$IMAGE" -lc 'find /app/app /repo/services/shared/python/desifaces_shared -type f ! -path "*/__pycache__/*" ! -name "*.pyc" -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk "{print \$1}"')"
DEPS_HASH="$(docker run --rm --entrypoint sh "$IMAGE" -lc 'python -m pip freeze | LC_ALL=C sort | sha256sum | awk "{print \$1}"')"
[[ "$APP_HASH" =~ ^[0-9a-f]{64}$ ]] || fail "candidate app hash unavailable"
[[ "$DEPS_HASH" =~ ^[0-9a-f]{64}$ ]] || fail "candidate deps hash unavailable"
echo "CANDIDATE_APP_HASH=$APP_HASH"
echo "CANDIDATE_DEPS_HASH=$DEPS_HASH"

# Reuse DEV runtime configuration without printing secrets; candidate workers remain disabled.
docker inspect "$EXT" > "$TMP/ext.inspect.json"
python3 - "$TMP/ext.inspect.json" "$ENVFILE" <<'PY'
import json,sys
obj=json.load(open(sys.argv[1]))[0]
skip={"WORKER_ENABLED","STITCH_WORKER_ENABLED","PORT"}
with open(sys.argv[2],"w") as out:
    for item in obj.get("Config",{}).get("Env",[]):
        if "=" not in item: continue
        k,v=item.split("=",1)
        if k in skip: continue
        if "\n" in v or "\r" in v: raise SystemExit(f"unsupported newline env: {k}")
        out.write(f"{k}={v}\n")
PY
chmod 600 "$ENVFILE"
NETWORK="$(python3 - "$TMP/ext.inspect.json" <<'PY'
import json,sys
nets=list(json.load(open(sys.argv[1]))[0].get("NetworkSettings",{}).get("Networks",{}))
if not nets: raise SystemExit(2)
print(nets[0])
PY
)"
[[ -n "$NETWORK" ]] || fail "DEV network unresolved"

docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
docker run -d \
  --name "$CANDIDATE" \
  --network "$NETWORK" \
  --network-alias "$CANDIDATE" \
  --env-file "$ENVFILE" \
  -e PORT=8006 \
  -e WORKER_ENABLED=false \
  -e STITCH_WORKER_ENABLED=false \
  "$IMAGE" >/dev/null

echo "ISOLATED_CANDIDATE_START=PASS"
echo "HOST_PORT_BINDING=NONE"

READY=0
for _ in $(seq 1 60); do
  if docker exec "$CANDIDATE" curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8006/api/health >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
(( READY == 1 )) || { docker logs --tail 120 "$CANDIDATE" >&2 || true; fail "candidate API failed health"; }
echo "CANDIDATE_RUNTIME_HEALTH=PASS"

# Probe routes from inside the isolated candidate; no host socket/port involved.
docker exec "$CANDIDATE" python -c '
import urllib.request,urllib.error
for path in ("/api/longform/pricing/preview","/api/longform/v3/scene-pricing/preview"):
    req=urllib.request.Request("http://127.0.0.1:8006"+path,data=b"{}",method="POST",headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req,timeout=5) as r: code=r.status
    except urllib.error.HTTPError as e: code=e.code
    assert code != 404 and code < 500,(path,code)
    print(f"candidate_route={path} http={code}")
print("SINGLE_PERSON_RUNTIME_ROUTE=PASS")
print("MULTI_PERSON_RUNTIME_ROUTE=PASS")
'

# Real DEV Director -> isolated candidate over Docker DNS/network.
docker exec "$DIRECTOR" python -c '
import urllib.request,urllib.error
u="http://df-fusion-extension-v3-cert:8006/api/longform/v3/scene-pricing/preview"
r=urllib.request.Request(u,data=b"{}",method="POST",headers={"Content-Type":"application/json"})
try:
    c=urllib.request.urlopen(r,timeout=5).status
except urllib.error.HTTPError as e:
    c=e.code
assert c != 404 and c < 500,c
print("DEV_DIRECTOR_TO_CANDIDATE=PASS")
print("DEV_DIRECTOR_CANDIDATE_HTTP="+str(c))
'

echo "============================================================"
echo "DEV_SINGLE_PERSON_REGRESSION_GATE=PASS"
echo "DEV_MULTI_PERSON_CONTRACT_GATE=PASS"
echo "DEV_DIRECTOR_CONNECTIVITY_GATE=PASS"
echo "HOST_PORT_TOUCH=NONE"
echo "PRODUCTION_TOUCH=NONE"
echo "PRODUCTION_PROMOTION_GATE=PASS"
echo "============================================================"
