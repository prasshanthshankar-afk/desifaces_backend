#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${V3_ROOT:-/home/azureuser/workspace/desifaces-v3}"
WEB_ROOT="${WEB_ROOT:-/home/azureuser/workspace/desifaces-web-review}"
BRANCH="${AI_FIX_BRANCH:-fix/v3-ai-performance-quality-20260901}"
FUSION_API="${FUSION_API_CONTAINER:-df-v3-svc-fusion}"
FUSION_WORKER="${FUSION_WORKER_CONTAINER:-df-v3-svc-fusion-worker}"
FACE_API="${FACE_API_CONTAINER:-df-v3-svc-face}"
FACE_WORKER="${FACE_WORKER_CONTAINER:-df-v3-svc-face-worker}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="/tmp/v3-ai-performance-quality-${STAMP}"
mkdir -p "$TMP"

exists(){ docker inspect "$1" >/dev/null 2>&1; }
copy_if(){ docker cp "$1:$2" "$3" >/dev/null 2>&1 || true; }

patch_main(){
  local src="$1" dst="$2" import_line="$3" install_call="$4" anchor="$5"
  python3 - "$src" "$dst" "$import_line" "$install_call" "$anchor" <<'PY'
from pathlib import Path
import sys
src_path,dst_path,import_line,install_call,anchor=sys.argv[1:]
s=Path(src_path).read_text()
if import_line not in s:
    if anchor in s:
        s=s.replace(anchor,anchor+"\n"+import_line,1)
    else:
        lines=s.splitlines(); idx=0
        for i,line in enumerate(lines):
            if line.startswith("from ") or line.startswith("import "):
                idx=i+1
        lines.insert(idx,import_line); s="\n".join(lines)+"\n"
if install_call not in s:
    lines=s.splitlines(); inserted=False
    for i,line in enumerate(lines):
        if line.startswith("def create_app(") or line.startswith("def create_app()"):
            indent="    "
            lines.insert(i+1,indent+install_call); inserted=True; break
    if not inserted:
        raise SystemExit(f"cannot find create_app in {src_path}")
    s="\n".join(lines)+"\n"
Path(dst_path).write_text(s)
PY
}

rollback(){
  set +e
  for c in "$FUSION_API" "$FUSION_WORKER" "$FACE_API" "$FACE_WORKER"; do
    exists "$c" || continue
    key="$(echo "$c"|tr '/:' '__')"
    [ -f "$TMP/${key}-main.py" ] && docker cp "$TMP/${key}-main.py" "$c":/app/app/main.py >/dev/null 2>&1
    if [[ "$c" == *fusion* ]]; then
      if [ -f "$TMP/${key}-fusion_quality_policy.py" ]; then docker cp "$TMP/${key}-fusion_quality_policy.py" "$c":/app/app/services/fusion_quality_policy.py >/dev/null 2>&1; else docker exec "$c" rm -f /app/app/services/fusion_quality_policy.py >/dev/null 2>&1; fi
    else
      if [ -f "$TMP/${key}-face_performance_policy.py" ]; then docker cp "$TMP/${key}-face_performance_policy.py" "$c":/app/app/services/face_performance_policy.py >/dev/null 2>&1; else docker exec "$c" rm -f /app/app/services/face_performance_policy.py >/dev/null 2>&1; fi
    fi
    docker restart "$c" >/dev/null 2>&1 || true
  done
  echo "ROLLBACK: restored prior Face/Fusion runtime files"
}
trap 'rc=$?; if [ $rc -ne 0 ]; then rollback; fi; exit $rc' EXIT

echo "============================================================"
echo " desifaces V3 AI PERFORMANCE + VIDEO QUALITY DEPLOY"
echo "============================================================"

echo
echo "===== 1. SOURCE GATE ====="
cd "$ROOT"
git fetch -q origin "$BRANCH"
git show "origin/$BRANCH:services/svc-fusion/app/app/services/fusion_quality_policy.py" > "$TMP/fusion_quality_policy.py"
git show "origin/$BRANCH:services/svc-face/app/app/services/face_performance_policy.py" > "$TMP/face_performance_policy.py"
python3 -m py_compile "$TMP/fusion_quality_policy.py" "$TMP/face_performance_policy.py"
grep -q 'pricing.get("tier_code")' "$TMP/fusion_quality_policy.py"
grep -q 'DF_FACE_VARIANT_CONCURRENCY", "4"' "$TMP/face_performance_policy.py"
echo "PASS: trusted-tier quality + Face concurrency policies validated"

echo
echo "===== 2. PRESERVE LIVE RUNTIME ====="
for c in "$FUSION_API" "$FUSION_WORKER" "$FACE_API" "$FACE_WORKER"; do
  exists "$c" || continue
  key="$(echo "$c"|tr '/:' '__')"
  copy_if "$c" /app/app/main.py "$TMP/${key}-main.py"
  if [[ "$c" == *fusion* ]]; then copy_if "$c" /app/app/services/fusion_quality_policy.py "$TMP/${key}-fusion_quality_policy.py"; else copy_if "$c" /app/app/services/face_performance_policy.py "$TMP/${key}-face_performance_policy.py"; fi
done
echo "PASS: rollback captured at $TMP"

echo
echo "===== 3. PATCH FUSION API + WORKER ====="
for c in "$FUSION_API" "$FUSION_WORKER"; do
  exists "$c" || { echo "FAIL: missing $c" >&2; exit 4; }
  key="$(echo "$c"|tr '/:' '__')"
  patch_main "$TMP/${key}-main.py" "$TMP/${key}-main.patched.py" \
    'from app.services.fusion_quality_policy import install_fusion_quality_policy' \
    'install_fusion_quality_policy()' \
    'from app.services.fusion_orchestrator import FusionOrchestrator'
  docker cp "$TMP/fusion_quality_policy.py" "$c":/app/app/services/fusion_quality_policy.py
  docker cp "$TMP/${key}-main.patched.py" "$c":/app/app/main.py
done
docker restart "$FUSION_API" "$FUSION_WORKER" >/dev/null
echo "PASS: Fusion API + worker restarted only"

echo
echo "===== 4. PATCH FACE API / OPTIONAL WORKER ====="
for c in "$FACE_API" "$FACE_WORKER"; do
  exists "$c" || continue
  key="$(echo "$c"|tr '/:' '__')"
  patch_main "$TMP/${key}-main.py" "$TMP/${key}-main.patched.py" \
    'from app.services.face_performance_policy import install_face_performance_policy' \
    'install_face_performance_policy()' \
    'from app.db import close_pool, get_pool'
  docker cp "$TMP/face_performance_policy.py" "$c":/app/app/services/face_performance_policy.py
  docker cp "$TMP/${key}-main.patched.py" "$c":/app/app/main.py
done
docker restart "$FACE_API" >/dev/null
exists "$FACE_WORKER" && docker restart "$FACE_WORKER" >/dev/null || true
echo "PASS: Face runtime restarted only"

echo
echo "===== 5. RUNTIME CONTRACT ====="
sleep 4
docker exec "$FUSION_API" python -c 'import inspect,app.main; import app.services.fusion_quality_policy as p; from app.services.providers.omnihuman_adapter import OmniHumanAdapter; assert p._INSTALLED; src=inspect.getsource(OmniHumanAdapter._tier_code); assert "_pricing_tier_code" in src; print("PASS: Fusion trusted pricing tier quality policy loaded")'
docker exec "$FACE_API" python -c 'import app.main; import app.services.face_performance_policy as p; from app.services.creator_orchestrator import CreatorOrchestrator; assert p._INSTALLED; v=CreatorOrchestrator.__new__(CreatorOrchestrator)._face_variant_concurrency(); print("face_variant_concurrency=",v); assert 1 <= v <= 8; print("PASS: Face concurrency policy loaded")'
docker exec "$FUSION_API" python -c 'import inspect,app.main; from app.services.fusion_orchestrator import FusionOrchestrator; sig=inspect.signature(FusionOrchestrator._build_initial_pricing_block); assert len(sig.parameters)==2; print("PASS: Multi-Person pricing wrapper preserved",sig)'
echo "PASS: runtime policies loaded"

echo
echo "===== 6. DEPLOY CERTIFIED WEB UX ====="
cd "$WEB_ROOT"
git pull --ff-only origin main
a=bash
bash scripts/deploy-ai-performance-quality-ux.sh

echo
echo "============================================================"
echo " AI PERFORMANCE + VIDEO QUALITY: DEPLOYED"
echo "============================================================"
echo "fusion_quality=trusted-pricing-tier"
echo "face_variant_concurrency=4-default-bounded"
echo "face_price_visibility=deployed"
echo "long_running_hourglass=deployed"
echo "premium_video=1080p-capable"
echo "fast_video=720p-turbo"
echo "economy_video=veed-fabric-480p"
echo "db=untouched"
echo "redis=untouched"
echo "pricing_service=untouched"
echo "audio=untouched"
echo "rollback_dir=$TMP"
echo "============================================================"
trap - EXIT
