#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo 'FAIL: DEV host required'; exit 1; }
SOURCE_SHA="${1:?Exact candidate SHA required}"
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || exit 1
ROOT=/home/azureuser/workspace/desifaces-runtime
STATE="$ROOT/.desifaces-deploy/single-person-ending-$SOURCE_SHA"
mkdir -p "$STATE"
EXPECTED_BASE=1d353d705863759ca821c9cde73c67daf0ad0a9b3766329f55f4faf2644475e6
SERVICES=(svc-fusion-extension svc-fusion-extension-worker svc-fusion-extension-stitch-worker)
CONTAINERS=(df-svc-fusion-extension df-svc-fusion-extension-worker df-svc-fusion-extension-stitch-worker)

for C in "${CONTAINERS[@]}"; do
  docker inspect "$C" > "$STATE/$C.inspect.json"
  docker exec "$C" python -c '
import hashlib
from pathlib import Path
from app.services import longform_orchestrator as m
print(hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest())
' > "$STATE/$C.source-sha"
  [[ "$(cat "$STATE/$C.source-sha")" == "$EXPECTED_BASE" ]] || {
    echo "FAIL: unexpected live source in $C; nothing deployed"; exit 1;
  }
done

PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' df-svc-fusion-extension)"
FILES="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' df-svc-fusion-extension)"
[[ "$PROJECT" == desifaces ]] || exit 1
IFS=',' read -r -a COMPOSE_FILES <<< "$FILES"
ENV_FILE="$ROOT/infra/.env"
[[ -f "$ENV_FILE" ]] || { echo 'FAIL: runtime env missing'; exit 1; }
# Preserve the existing model setting required when rendering the base compose.
DF_OMNIHUMAN_MODEL_ID="$(docker exec df-svc-fusion printenv DF_OMNIHUMAN_MODEL_ID)"
export DF_OMNIHUMAN_MODEL_ID
COMPOSE=(docker compose --env-file "$ENV_FILE" -p "$PROJECT" --project-directory "$ROOT")
for F in "${COMPOSE_FILES[@]}"; do
  [[ -f "$F" ]] || { echo "FAIL: active compose file missing: $F"; exit 1; }
  COMPOSE+=(-f "$F")
done
"${COMPOSE[@]}" config --format json > "$STATE/compose-before.json"

# Compare resolved command/environment to live containers before any recreation.
# Image defaults stay intact because candidates derive from each live image.
python3 - "$STATE" <<'PY'
import json, shlex, sys
from pathlib import Path
state = Path(sys.argv[1])
config = json.loads((state / 'compose-before.json').read_text())
names = ['svc-fusion-extension', 'svc-fusion-extension-worker', 'svc-fusion-extension-stitch-worker']
def args(value):
    return shlex.split(value) if isinstance(value, str) else (value or [])
for name in names:
    live = json.loads((state / ('df-' + name + '.inspect.json')).read_text())[0]
    wanted = config['services'][name]
    current_env = dict(item.split('=', 1) for item in live['Config']['Env'])
    assert all(str(value) == current_env.get(key) for key, value in wanted.get('environment', {}).items()), 'environment drift: ' + name
    for compose_key, docker_key in [('command', 'Cmd'), ('entrypoint', 'Entrypoint')]:
        if wanted.get(compose_key) is not None:
            assert args(wanted[compose_key]) == args(live['Config'][docker_key]), 'command drift: ' + name
    assert live['Config']['Labels']['com.docker.compose.project'] == 'desifaces'
    expected_networks = {config['networks'][key]['name'] for key in wanted.get('networks', {})}
    assert expected_networks == set(live['NetworkSettings']['Networks']), 'network drift: ' + name
    expected_mounts = {}
    for mount in wanted.get('volumes', []):
        source = mount.get('source')
        if mount['type'] == 'volume' and source:
            source = config.get('volumes', {}).get(source, {}).get('name', source)
        expected_mounts[mount['target']] = (mount['type'], source, not mount.get('read_only', False))
    actual_mounts = {m['Destination']: (m['Type'], m.get('Name') if m['Type']=='volume' else m.get('Source'), m['RW']) for m in live.get('Mounts', [])}
    assert expected_mounts == actual_mounts, 'mount drift: ' + name
    assert wanted.get('restart', 'no') == live['HostConfig']['RestartPolicy']['Name'], 'restart policy drift: ' + name
    expected_ports = {}
    for port in wanted.get('ports', []):
        key = str(port['target']) + '/' + port.get('protocol', 'tcp')
        expected_ports.setdefault(key, []).append((port.get('host_ip') or '', str(port['published'])))
    actual_ports = {key: [(item.get('HostIp') or '', item['HostPort']) for item in values] for key, values in (live['HostConfig'].get('PortBindings') or {}).items()}
    assert expected_ports == actual_ports, 'port drift: ' + name
    assert not any('/app/app/services/longform_orchestrator.py' == m['Destination'] or
                   '/app/app/services/longform_orchestrator.py'.startswith(m['Destination'].rstrip('/') + '/')
                   for m in live.get('Mounts', [])), 'code mount would hide candidate: ' + name
print('LIVE_CONFIGURATION_COMPARISON=PASS')
PY

for FILE in longform_orchestrator.py talking_video_finish.py; do
  gh api "repos/prasshanthshankar-afk/desifaces_backend/contents/services/svc-fusion-extension/app/app/services/$FILE?ref=$SOURCE_SHA" --jq .content |
    base64 -d > "$STATE/$FILE"
done
gh api "repos/prasshanthshankar-afk/desifaces_backend/contents/services/svc-fusion-extension/tests/test_talking_video_finish.py?ref=$SOURCE_SHA" --jq .content |
  base64 -d > "$STATE/test_talking_video_finish.py"
python3 - "$STATE" "$EXPECTED_BASE" <<'PY'
import hashlib, sys
from pathlib import Path
p = Path(sys.argv[1])
s = (p / 'longform_orchestrator.py').read_text()
a = s.index('            if should_finish_single_person(tags, len(rows)):')
b = s.index('            storage_path, signed_url = upload_final_mp4(final_local)', a)
base = (s[:a] + s[b:]).replace('from app.services.talking_video_finish import finish_single_person_video, should_finish_single_person\n', '')
assert hashlib.sha256((base.rstrip()+'\n').encode()).hexdigest() == sys.argv[2], 'candidate changes outside approved scope'
t = (p / 'test_talking_video_finish.py').read_text().replace(
    'MODULE = Path(__file__).parents[1] / "app/app/services/talking_video_finish.py"',
    'MODULE = Path("/app/app/services/talking_video_finish.py")')
(p / 'runtime_test.py').write_text(t)
print('SOURCE_MUTATION_BOUNDARY=PASS')
PY

mkdir -p "$STATE/build"
cp "$STATE/longform_orchestrator.py" "$STATE/talking_video_finish.py" "$STATE/runtime_test.py" "$STATE/build/"
for I in "${!CONTAINERS[@]}"; do
  C="${CONTAINERS[$I]}"
  IMAGE_ID="$(docker inspect -f '{{.Image}}' "$C")"
  BASE_TAG="desifaces-ending-baseline-${SERVICES[$I]}:$SOURCE_SHA"
  docker tag "$IMAGE_ID" "$BASE_TAG"
  cat > "$STATE/build/Dockerfile" <<EOF
FROM $BASE_TAG
COPY longform_orchestrator.py talking_video_finish.py /app/app/services/
COPY runtime_test.py /opt/desifaces-ending-certification.py
EOF
  docker build -t "desifaces-ending-${SERVICES[$I]}:$SOURCE_SHA" "$STATE/build"
  docker run --rm --network none --entrypoint python \
    "desifaces-ending-${SERVICES[$I]}:$SOURCE_SHA" /opt/desifaces-ending-certification.py
done

python3 - "$STATE" "$SOURCE_SHA" <<'PY'
import json, sys
from pathlib import Path
p, sha = Path(sys.argv[1]), sys.argv[2]
names = ['svc-fusion-extension', 'svc-fusion-extension-worker', 'svc-fusion-extension-stitch-worker']
candidate, rollback = {}, {}
for name in names:
    current = json.loads((p / ('df-' + name + '.inspect.json')).read_text())[0]
    candidate[name] = {'image': f'desifaces-ending-{name}:{sha}'}
    rollback[name] = {'image': current['Image']}
(p / 'candidate.json').write_text(json.dumps({'services': candidate}))
(p / 'rollback.json').write_text(json.dumps({'services': rollback}))
PY

idle(){
  docker exec -i df-svc-fusion-extension python - <<'PY'
import asyncio, os, asyncpg
async def main():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg://','postgresql://'))
    try:
        async with conn.transaction(readonly=True):
            n = await conn.fetchval("SELECT count(*) FROM public.longform_jobs WHERE status IN ('queued','running','stitching','stitching_running')")
            print(int(n))
    finally:
        await conn.close()
asyncio.run(main())
PY
}
[[ "$(idle)" == 0 ]] || { echo 'FAIL: active longform work; candidates built, no runtime changed'; exit 1; }
rollback(){
  if [[ "${CUTOVER_STARTED:-0}" == 0 ]]; then
    "${COMPOSE[@]}" start svc-fusion-extension
    return
  fi
  echo 'Rolling back Fusion Extension containers to their exact previous images'
  "${COMPOSE[@]}" -f "$STATE/rollback.json" up -d --no-deps --no-build --pull never --force-recreate "${SERVICES[@]}"
}
CUTOVER_STARTED=0
trap 'CODE=$?; trap - ERR; rollback || true; exit "$CODE"' ERR
# Pause creation first, then recheck the queue before stopping workers.
"${COMPOSE[@]}" stop svc-fusion-extension
# The stopped API cannot run SQL: use the still-running stitch worker instead.
ACTIVE="$(docker exec df-svc-fusion-extension-stitch-worker python -c '
import asyncio, os, asyncpg
async def main():
 c=await asyncpg.connect(os.environ["DATABASE_URL"].replace("postgresql+asyncpg://","postgresql://"))
 try:
  async with c.transaction(readonly=True):
   print(await c.fetchval("SELECT count(*) FROM public.longform_jobs WHERE status IN (\x27queued\x27,\x27running\x27,\x27stitching\x27,\x27stitching_running\x27)"))
 finally: await c.close()
asyncio.run(main())
')"
[[ "$ACTIVE" == 0 ]]
CUTOVER_STARTED=1
"${COMPOSE[@]}" stop svc-fusion-extension-worker svc-fusion-extension-stitch-worker
"${COMPOSE[@]}" -f "$STATE/candidate.json" up -d --no-deps --no-build --pull never --force-recreate "${SERVICES[@]}"
for C in "${CONTAINERS[@]}"; do
  docker exec "$C" python -c '
import inspect
from app.services.longform_orchestrator import stitch_if_ready
from app.services.talking_video_finish import should_finish_single_person
assert "finish_single_person_video" in inspect.getsource(stitch_if_ready)
assert not should_finish_single_person({"source_surface":"director"},1)
print("RUNTIME_IMPORT=PASS")
'
done
for I in $(seq 1 30); do
  if docker exec df-svc-fusion-extension python -c 'import os, urllib.request; urllib.request.urlopen("http://127.0.0.1:" + os.environ.get("PORT", "8006") + "/api/health", timeout=2)' >/dev/null 2>&1; then
    HEALTH_OK=1
    break
  fi
  sleep 2
done
[[ "${HEALTH_OK:-0}" == 1 ]]
trap - ERR
echo 'SINGLE_PERSON_ENDING_DEV_DEPLOY=PASS'
echo 'PRODUCTION_UNTOUCHED=PASS'
echo "rollback_overlay=$STATE/rollback.json"
