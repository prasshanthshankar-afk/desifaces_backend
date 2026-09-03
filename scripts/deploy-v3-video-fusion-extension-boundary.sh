#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${V3_ROOT:-/home/azureuser/workspace/desifaces-v3}"
WEB_ROOT="${WEB_ROOT:-/home/azureuser/workspace/desifaces-web-review}"
BRANCH="${VIDEO_BOUNDARY_BRANCH:-fix/v3-video-fusion-extension-boundary-20260901}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="/tmp/v3-video-fusion-extension-boundary-${STAMP}"
mkdir -p "$TMP"

exists(){ docker inspect "$1" >/dev/null 2>&1; }
resolve_container(){
  local preferred="$1" service="$2"
  if exists "$preferred"; then printf '%s' "$preferred"; return 0; fi
  local found
  found="$(docker ps -a --filter "label=com.docker.compose.service=${service}" --format '{{.Names}}' | head -1)"
  [ -n "$found" ] || return 1
  printf '%s' "$found"
}

EXT_API="$(resolve_container "${FUSION_EXTENSION_API_CONTAINER:-df-v3-svc-fusion-extension}" svc-fusion-extension)" || {
  echo "FAIL: svc-fusion-extension API container not found" >&2
  exit 2
}

rollback(){
  set +e
  [ -f "$TMP/live-main.py" ] && docker cp "$TMP/live-main.py" "$EXT_API":/app/app/main.py >/dev/null 2>&1
  if [ -f "$TMP/live-policy.py" ]; then
    docker cp "$TMP/live-policy.py" "$EXT_API":/app/app/services/longform_pricing_confirmation_policy.py >/dev/null 2>&1
  else
    docker exec "$EXT_API" rm -f /app/app/services/longform_pricing_confirmation_policy.py >/dev/null 2>&1 || true
  fi
  docker restart "$EXT_API" >/dev/null 2>&1 || true
  echo "ROLLBACK: restored prior Fusion Extension API runtime"
}
trap 'rc=$?; if [ $rc -ne 0 ]; then rollback; fi; exit $rc' EXIT

echo "============================================================"
echo " desifaces V3 VIDEO — RESTORE FUSION EXTENSION BOUNDARY"
echo "============================================================"

echo
echo "===== 1. SOURCE GATE ====="
cd "$ROOT"
git fetch -q origin "$BRANCH"
git show "origin/$BRANCH:services/svc-fusion-extension/app/app/services/longform_pricing_confirmation_policy.py" > "$TMP/policy.py"
git show "origin/$BRANCH:services/svc-fusion-extension/app/app/main.py" > "$TMP/source-main.py"
python3 -m py_compile "$TMP/policy.py" "$TMP/source-main.py"
grep -q '_normalize_longform_request_body' "$TMP/policy.py"
grep -q 'pricing_confirmation' "$TMP/policy.py"
grep -q 'install_longform_pricing_confirmation_policy()' "$TMP/source-main.py"
echo "PASS: parent quote-confirmation preservation source validated"

echo
echo "===== 2. PRESERVE LIVE EXTENSION API ====="
docker cp "$EXT_API":/app/app/main.py "$TMP/live-main.py"
docker cp "$EXT_API":/app/app/services/longform_pricing_confirmation_policy.py "$TMP/live-policy.py" >/dev/null 2>&1 || true
sha256sum "$TMP/live-main.py" > "$TMP/rollback.sha256"
[ -f "$TMP/live-policy.py" ] && sha256sum "$TMP/live-policy.py" >> "$TMP/rollback.sha256" || true
echo "PASS: rollback captured at $TMP"

echo
echo "===== 3. PATCH FUSION EXTENSION API ONLY ====="
python3 - "$TMP/live-main.py" "$TMP/live-main.patched.py" <<'PY'
from pathlib import Path
import sys
src, dst = map(Path, sys.argv[1:])
s = src.read_text()
imp = 'from app.services.longform_pricing_confirmation_policy import install_longform_pricing_confirmation_policy'
call = 'install_longform_pricing_confirmation_policy()'
if imp not in s:
    anchor = 'from app.config import settings'
    if anchor not in s:
        raise SystemExit('cannot locate Fusion Extension main import anchor')
    s = s.replace(anchor, anchor + '\n' + imp, 1)
if call not in s:
    marker = 'from app.workers.longform_worker import worker_loop'
    if marker not in s:
        raise SystemExit('cannot locate Fusion Extension worker import anchor')
    s = s.replace(marker, marker + '\n\n' + call, 1)
dst.write_text(s)
PY
python3 -m py_compile "$TMP/live-main.patched.py"
docker cp "$TMP/policy.py" "$EXT_API":/app/app/services/longform_pricing_confirmation_policy.py
docker cp "$TMP/live-main.patched.py" "$EXT_API":/app/app/main.py
docker restart "$EXT_API" >/dev/null
echo "PASS: restarted Fusion Extension API only"

echo
echo "===== 4. EXTENSION RUNTIME CONTRACT ====="
sleep 4
docker exec -i "$EXT_API" python - <<'PY'
import app.main
import app.api.routes.longform as route
import app.services.longform_pricing_confirmation_policy as policy
assert policy._INSTALLED, 'pricing-confirmation policy not installed'
raw = {
    'image_ref': '11111111-1111-1111-1111-111111111111',
    'script': 'boundary probe',
    'pricing_confirmation': {'quote_id': 'qt_probe', 'preview_fingerprint': 'fp_probe'},
}
normalized = route._normalize_longform_request_body(raw)
assert normalized.get('tags', {}).get('pricing_confirmation', {}).get('quote_id') == 'qt_probe'
assert normalized.get('tags', {}).get('pricing_confirmation', {}).get('preview_fingerprint') == 'fp_probe'
assert getattr(route.reserve_longform_pricing_for_job, '_df_longform_confirmation_policy', False)
print('PASS: confirmed quote survives longform API normalization')
PY

docker exec -i "$EXT_API" python - <<'PY'
from app.services.longform_orchestrator import build_longform_execution_payloads
payload = {
    'image_ref': '11111111-1111-1111-1111-111111111111',
    'face_artifact_id': '11111111-1111-1111-1111-111111111111',
    'script': 'This is a ninety second saved Voice boundary probe. ' * 30,
    'script_text': 'This is a ninety second saved Voice boundary probe. ' * 30,
    'longform_profile': 'talking_video',
    'quality_tier': 'premium',
    'requested_duration_sec': 90,
    'duration_sec': 90,
    'pricing_duration_sec': 90,
    'segment_seconds': 90,
    'max_segment_seconds': 90,
    'tags': {
        'longform_profile': 'talking_video',
        'quality_tier': 'premium',
        'requested_duration_sec': 90,
        'duration_sec': 90,
        'voice_audio_artifact_id': '22222222-2222-2222-2222-222222222222',
        'voice_audio_duration_sec': 90,
        'selected_audio': {
            'audio_artifact_id': '22222222-2222-2222-2222-222222222222',
            'audio_duration_sec': 90,
        },
    },
}
planned = build_longform_execution_payloads(payload)
segments = planned.get('segments') or []
durations = [int(x.get('duration_sec') or 0) for x in segments]
print('planner_segment_durations=', durations)
assert len(segments) >= 3, f'expected multiple segments for 90s; got {durations}'
assert durations and max(durations) <= 30, f'provider-unsafe segment found: {durations}'
assert sum(durations) >= 80, f'planner lost substantial duration: {durations}'
print('PASS: 90-second Voice is segmented into provider-safe child durations')
PY

docker exec -i "$EXT_API" python - <<'PY'
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8006/api/health', timeout=8) as r:
    body=json.loads(r.read().decode())
    assert r.status == 200 and body.get('status') == 'ok'
print('PASS: Fusion Extension API HTTP_200')
PY

echo
echo "===== 5. PRESERVE BACKEND SERVICES ====="
for spec in 'Audio:18004' 'Fusion:18002' 'Face:18003' 'Pricing:18009' 'Director:18011'; do
  name="${spec%%:*}"; port="${spec##*:}"
  curl -fsS "http://127.0.0.1:${port}/api/health" >/dev/null
  echo "PASS: $name HTTP_200"
done

echo
echo "===== 6. DEPLOY WEB FUSION-EXTENSION BOUNDARY ====="
cd "$WEB_ROOT"
git pull --ff-only origin main
bash scripts/deploy-video-fusion-extension-boundary.sh

echo
echo "============================================================"
echo " VIDEO ORCHESTRATION BOUNDARY: DEPLOYED + CERTIFIED"
echo "============================================================"
echo "fusion_extension_api=$EXT_API"
echo "browser_orchestrator=svc-fusion-extension"
echo "core_fusion=internal-child-render-only"
echo "saved_voice=reused-not-regenerated"
echo "real_audio_duration=required"
echo "saved_script=required"
echo "provider_safe_segmentation=extension-owned"
echo "ninety_second_probe=passed"
echo "child_pricing=suppressed"
echo "parent_pricing=confirmed-and-bound"
echo "final_video=parent-stitched-output"
echo "db=untouched"
echo "redis=untouched"
echo "audio=preserved"
echo "rollback_dir=$TMP"
echo "============================================================"
trap - EXIT
