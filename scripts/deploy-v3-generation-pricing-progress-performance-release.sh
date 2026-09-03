#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${BACKEND_ROOT:-/home/azureuser/workspace/desifaces-v3}"
BRANCH="${BACKEND_BRANCH:-fix/v3-video-pricing-progress-performance-20260903}"
TMP="/tmp/deploy-v3-generation-pricing-progress-performance.final.sh"

cd "$ROOT"
git fetch -q origin "$BRANCH"
git show "origin/$BRANCH:scripts/deploy-v3-generation-pricing-progress-performance.sh" > "$TMP"

# Release hardening performed before the underlying launcher runs:
# 1) use structural Face-progress patcher v2 rather than the brittle whole-method
#    literal replacement in v1;
# 2) rely on explicit Docker source regression/typecheck/Next build, not searching
#    optimized .next bundles for a source component symbol;
# 3) snapshot the actually running web container with docker commit. The original
#    image object may have been garbage-collected even while the container remains
#    healthy, so tagging docker-inspect .Image is not a reliable rollback mechanism;
# 4) arm rollback flags before the first runtime/web mutation, so partial copy or
#    recreate failures are still recoverable;
# 5) wait boundedly for backend readiness instead of assuming five seconds.
python3 - "$TMP" <<'PY'
from pathlib import Path
import sys

p = Path(sys.argv[1])
s = p.read_text()

old = 'python3 scripts/apply-v3-video-pricing-progress-performance-source.py\n'
new = 'python3 scripts/apply-v3-video-pricing-progress-performance-source-v2.py\n'
if old not in s:
    raise SystemExit('release source patcher invocation anchor missing')
s = s.replace(old, new, 1)

# Optimized Next output is not a source-code contract. The Docker build already
# runs the explicit generation-progress regression and full Next build.
needle = 'docker exec "$WEB_CONTAINER" sh -lc "grep -R -q \'GenerationProgress\' /app/.next" || fail "deployed web progress component not found in build output"\n'
if needle in s:
    s = s.replace(needle, '', 1)

# Capture rollback from the live container itself. This remains valid even when
# the image ID reported by docker inspect is no longer present in the image store.
old_snapshot = '''WEB_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$WEB_CONTAINER")"\nOLD_WEB_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$WEB_CONTAINER")"\nWEB_ROLLBACK_TAG="desifaces-web-before-generation-release:${STAMP}"\ndocker tag "$OLD_WEB_IMAGE_ID" "$WEB_ROLLBACK_TAG"\nlog "ROLLBACK_SNAPSHOT=PASS"\n'''
new_snapshot = '''WEB_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$WEB_CONTAINER")"\nWEB_ROLLBACK_TAG="desifaces-web-before-generation-release:${STAMP}"\ndocker commit "$WEB_CONTAINER" "$WEB_ROLLBACK_TAG" >/dev/null\ndocker image inspect "$WEB_ROLLBACK_TAG" >/dev/null 2>&1 || fail "web rollback snapshot image was not created"\nlog "ROLLBACK_SNAPSHOT=PASS web_source=live_container_commit"\n'''
if old_snapshot not in s:
    raise SystemExit('web rollback snapshot anchor missing')
s = s.replace(old_snapshot, new_snapshot, 1)

# Arm backend rollback before the first docker cp. If a copy fails halfway,
# previously copied files must still be restored.
backend_anchor = '''log "===== 6. DEPLOY BACKEND FILES — REQUIRED SERVICES ONLY ====="\nfor c in "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER"; do\n'''
backend_replacement = '''log "===== 6. DEPLOY BACKEND FILES — REQUIRED SERVICES ONLY ====="\nRUNTIME_CHANGED=1\nfor c in "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER"; do\n'''
if backend_anchor not in s:
    raise SystemExit('backend mutation flag anchor missing')
s = s.replace(backend_anchor, backend_replacement, 1)
s = s.replace('''done\nRUNTIME_CHANGED=1\ndocker restart "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER" "$FACE_API" "$FACE_WORKER" >/dev/null\n''', '''done\ndocker restart "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER" "$FACE_API" "$FACE_WORKER" >/dev/null\n''', 1)

# Arm web rollback before compose can replace the running container.
web_anchor = '''log "===== 8. DEPLOY WEB ONLY ====="\ncd "$WWT"\ndocker compose -f docker-compose.web.yml up -d --no-deps --force-recreate desifaces-web >"$RUN/web-deploy.log" 2>&1\nWEB_CHANGED=1\n'''
web_replacement = '''log "===== 8. DEPLOY WEB ONLY ====="\ncd "$WWT"\nWEB_CHANGED=1\ndocker compose -f docker-compose.web.yml up -d --no-deps --force-recreate desifaces-web >"$RUN/web-deploy.log" 2>&1\n'''
if web_anchor not in s:
    raise SystemExit('web mutation flag anchor missing')
s = s.replace(web_anchor, web_replacement, 1)

# Restart time varies with host load. Wait for the Extension API and Face API
# to become reachable rather than certifying against a fixed five-second sleep.
ready_anchor = '''log "===== 7. BACKEND RUNTIME CERTIFICATION ====="\nsleep 5\ndocker exec -i "$EXT_API" python - <<'PY'\n'''
ready_replacement = '''log "===== 7. BACKEND RUNTIME CERTIFICATION ====="\nBACKEND_READY=0\nfor _ in $(seq 1 45); do\n  ext_ok="$(docker exec "$EXT_API" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8006/api/health', timeout=3).read(); print('ok')" 2>/dev/null || true)"\n  face_code="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 4 http://127.0.0.1:18003/api/health 2>/dev/null || true)"\n  if [[ "$ext_ok" == *ok* && "$face_code" == 200 ]]; then BACKEND_READY=1; break; fi\n  sleep 2\ndone\n(( BACKEND_READY == 1 )) || fail "modified backend services did not become ready within 90 seconds"\ndocker exec -i "$EXT_API" python - <<'PY'\n'''
if ready_anchor not in s:
    raise SystemExit('backend readiness anchor missing')
s = s.replace(ready_anchor, ready_replacement, 1)

p.write_text(s)
PY

# Fail before any DB/runtime mutation if the corrected source patcher is absent.
git cat-file -e "origin/$BRANCH:scripts/apply-v3-video-pricing-progress-performance-source-v2.py" 2>/dev/null || {
  echo 'FAIL: corrected source patcher v2 missing from release branch' >&2
  exit 2
}

exec bash "$TMP"
