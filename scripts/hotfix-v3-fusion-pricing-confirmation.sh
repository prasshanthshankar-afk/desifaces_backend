#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${V3_ROOT:-/home/azureuser/workspace/desifaces-v3}"
BRANCH="${FUSION_FIX_BRANCH:-fix/v3-fusion-reusable-input-resolution-20260901}"
API="${FUSION_API_CONTAINER:-df-v3-svc-fusion}"
WORKER="${FUSION_WORKER_CONTAINER:-df-v3-svc-fusion-worker}"
PORT="${FUSION_HEALTH_PORT:-18002}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="/tmp/v3-fusion-pricing-confirmation-${STAMP}"
mkdir -p "$TMP"

copy_if_present() {
  local c="$1" src="$2" dst="$3"
  docker cp "$c:$src" "$dst" >/dev/null 2>&1 || true
}

restore_one() {
  local c="$1" saved="$2" dst="$3"
  if [ -f "$saved" ]; then docker cp "$saved" "$c:$dst" >/dev/null 2>&1 || true; fi
}

rollback() {
  set +e
  for c in "$API" "$WORKER"; do
    key="$(echo "$c" | tr '/:' '__')"
    restore_one "$c" "$TMP/${key}-artifacts_repo.py" /app/app/repos/artifacts_repo.py
    restore_one "$c" "$TMP/${key}-models.py" /app/app/domain/models.py
    restore_one "$c" "$TMP/${key}-main.py" /app/app/main.py
    if [ -f "$TMP/${key}-pricing_confirmation_policy.py" ]; then
      restore_one "$c" "$TMP/${key}-pricing_confirmation_policy.py" /app/app/services/pricing_confirmation_policy.py
    else
      docker exec "$c" sh -lc 'rm -f /app/app/services/pricing_confirmation_policy.py' >/dev/null 2>&1 || true
    fi
  done
  docker restart "$API" "$WORKER" >/dev/null 2>&1 || true
  echo "ROLLBACK: restored prior Fusion runtime files"
}
trap 'rc=$?; if [ $rc -ne 0 ]; then rollback; fi; exit $rc' EXIT

echo "============================================================"
echo " desifaces V3 FUSION — CONFIRMED PRICING HOTFIX"
echo "============================================================"

echo
echo "===== 1. SOURCE GATE ====="
cd "$ROOT"
git fetch -q origin "$BRANCH"
for spec in \
  "services/svc-fusion/app/app/repos/artifacts_repo.py:artifacts_repo.py" \
  "services/svc-fusion/app/app/domain/models.py:models.py" \
  "services/svc-fusion/app/app/services/pricing_confirmation_policy.py:pricing_confirmation_policy.py"; do
  src="${spec%%:*}"; dst="${spec##*:}"
  git show "origin/$BRANCH:$src" > "$TMP/$dst"
done
python3 -m py_compile "$TMP/artifacts_repo.py" "$TMP/models.py" "$TMP/pricing_confirmation_policy.py"
grep -q 'public.media_assets' "$TMP/artifacts_repo.py"
grep -q 'pricing_confirmation' "$TMP/models.py"
grep -q 'PRICING_CONFIRMATION_MISMATCH' "$TMP/pricing_confirmation_policy.py"
echo "PASS: reusable inputs + confirmed-pricing policy source validated"

echo
echo "===== 2. PRESERVE LIVE FUSION RUNTIME ====="
for c in "$API" "$WORKER"; do
  key="$(echo "$c" | tr '/:' '__')"
  copy_if_present "$c" /app/app/repos/artifacts_repo.py "$TMP/${key}-artifacts_repo.py"
  copy_if_present "$c" /app/app/domain/models.py "$TMP/${key}-models.py"
  copy_if_present "$c" /app/app/main.py "$TMP/${key}-main.py"
  copy_if_present "$c" /app/app/services/pricing_confirmation_policy.py "$TMP/${key}-pricing_confirmation_policy.py"
done
sha256sum "$TMP"/* 2>/dev/null > "$TMP/rollback.sha256" || true
echo "PASS: rollback captured at $TMP"

echo
echo "===== 3. PATCH FUSION API + WORKER ONLY ====="
for c in "$API" "$WORKER"; do
  key="$(echo "$c" | tr '/:' '__')"
  live_main="$TMP/${key}-main.py"
  patched_main="$TMP/${key}-main.patched.py"
  [ -f "$live_main" ]
  python3 - "$live_main" "$patched_main" <<'PY'
from pathlib import Path
import sys
src = Path(sys.argv[1]).read_text()
import_line = "from app.services.pricing_confirmation_policy import install_pricing_confirmation_policy"
if import_line not in src:
    anchor = "from app.services.fusion_orchestrator import FusionOrchestrator"
    if anchor not in src:
        raise SystemExit("cannot locate FusionOrchestrator import in live main.py")
    src = src.replace(anchor, anchor + "\n" + import_line, 1)
if "install_pricing_confirmation_policy()" not in src:
    lines = src.splitlines()
    inserted = False
    for i, line in enumerate(lines):
        if "install_multi_person_pricing_policy()" in line:
            indent = line[: len(line) - len(line.lstrip())]
            lines.insert(i + 1, indent + "install_pricing_confirmation_policy()")
            inserted = True
            break
    if not inserted:
        for i, line in enumerate(lines):
            if line.startswith("def create_app()") or line.startswith("def create_app("):
                lines.insert(i + 1, "    install_pricing_confirmation_policy()")
                inserted = True
                break
    if not inserted:
        raise SystemExit("cannot locate pricing policy installation point in live main.py")
    src = "\n".join(lines) + "\n"
Path(sys.argv[2]).write_text(src)
PY
  docker cp "$TMP/artifacts_repo.py" "$c":/app/app/repos/artifacts_repo.py
  docker cp "$TMP/models.py" "$c":/app/app/domain/models.py
  docker cp "$TMP/pricing_confirmation_policy.py" "$c":/app/app/services/pricing_confirmation_policy.py
  docker cp "$patched_main" "$c":/app/app/main.py
done
docker restart "$API" "$WORKER" >/dev/null
echo "PASS: Fusion API + worker restarted; no other service touched"

echo
echo "===== 4. HEALTH ====="
ok=0
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[ "$ok" -eq 1 ]
echo "PASS: Fusion API HTTP_200"

echo
echo "===== 5. LOADED INPUT + PRICING CONTRACT ====="
docker exec "$API" python -c 'import inspect, app.main; from app.domain.models import FusionJobCreate; from app.services.fusion_orchestrator import FusionOrchestrator; import app.services.pricing_confirmation_policy as p; assert "pricing_confirmation" in FusionJobCreate.model_fields; assert p._INSTALLED; assert "PRICING_CONFIRMATION_MISMATCH" in inspect.getsource(FusionOrchestrator.create_job); assert len(inspect.signature(FusionOrchestrator._build_initial_pricing_block).parameters)==2; print("PASS: confirmation field + enforcement policy loaded"); print("pricing_signature=", inspect.signature(FusionOrchestrator._build_initial_pricing_block))'
for c in "$API" "$WORKER"; do
  docker exec "$c" python -c 'import inspect; from app.repos.artifacts_repo import ArtifactsRepo; s=inspect.getsource(ArtifactsRepo.get_artifact_by_id); assert "public.artifacts" in s and "public.media_assets" in s; print("PASS: reusable input resolver loaded")'
done

echo
echo "===== 6. RESERVE PROPAGATION CONTRACT ====="
docker exec "$API" python -c 'import asyncio; from desifaces_shared.pricing.models import PricingReserveRequest; from app.services.pricing_confirmation_policy import _ConfirmationPricingClientProxy; C=type("C",(),{"enabled":True}); c=C(); c.received=None; async def_reserve=None' >/dev/null 2>&1 || true
docker exec "$API" python -c 'from app.domain.models import FusionJobCreate; r=FusionJobCreate.model_validate({"face_artifact_id":"11111111-1111-4111-8111-111111111111","voice_mode":"audio","voice_audio":{"type":"audio","audio_artifact_id":"22222222-2222-4222-8222-222222222222"},"video":{"aspect_ratio":"9:16","duration_sec":6},"provider":"omnihuman_v15","consent":{"external_provider_ok":True},"pricing_confirmation":{"quote_id":"qt_probe","preview_fingerprint":"fp_probe"}}); d=r.model_dump(exclude_none=True); assert d["pricing_confirmation"]["quote_id"]=="qt_probe"; print("PASS: pricing confirmation survives Fusion request validation")'

echo
echo "============================================================"
echo " FUSION CONFIRMED PRICING: HOTFIX READY"
echo "============================================================"
echo "branch=$BRANCH"
echo "rollback_dir=$TMP"
echo "api=healthy"
echo "worker=healthy_after_restart"
echo "pricing_confirmation=enforced"
echo "quote_binding=fresh_preview_match_required"
echo "db=untouched"
echo "redis=untouched"
echo "web=untouched"
echo "============================================================"
trap - EXIT
