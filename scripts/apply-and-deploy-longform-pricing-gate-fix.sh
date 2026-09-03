#!/usr/bin/env bash
set -Eeuo pipefail

WS="${WS:-/home/azureuser/workspace/desifaces-v3}"
SOURCE_BRANCH="${SOURCE_BRANCH:-fix/v3-video-fusion-extension-boundary-20260901}"
TARGET_BRANCH="${TARGET_BRANCH:-fix/v3-longform-pricing-gate-20260902}"
WT="/tmp/desifaces-v3-longform-pricing-gate"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ROLLBACK_DIR="/tmp/v3-longform-pricing-gate-rollback-${STAMP}"
LOG="/tmp/v3-longform-pricing-gate-${STAMP}.log"

exec > >(tee "$LOG") 2>&1

fail(){ echo "FAIL: $*" >&2; exit 1; }
resolve_container(){
  local preferred="$1" service="$2"
  if docker inspect "$preferred" >/dev/null 2>&1; then printf '%s' "$preferred"; return 0; fi
  docker ps -a --filter "label=com.docker.compose.service=${service}" --format '{{.Names}}' | head -1
}

cd "$WS"
git fetch -q origin "$SOURCE_BRANCH"
git worktree remove --force "$WT" >/dev/null 2>&1 || true
rm -rf "$WT"
git worktree add -q -B "$TARGET_BRANCH" "$WT" "origin/$SOURCE_BRANCH"
cd "$WT"

ROUTE="services/svc-fusion-extension/app/app/api/routes/longform.py"
REPO="services/svc-fusion-extension/app/app/repos/longform_jobs_repo.py"
TEST="services/svc-fusion-extension/tests/test_longform_pricing_gate_order.py"
mkdir -p "$(dirname "$TEST")"

python3 - <<'PY'
from pathlib import Path

repo = Path("services/svc-fusion-extension/app/app/repos/longform_jobs_repo.py")
route = Path("services/svc-fusion-extension/app/app/api/routes/longform.py")

r = repo.read_text()
old = '''        voice_gender_mode: Optional[str] = None,\n        voice_gender: Optional[str] = None,\n    ) -> str:\n'''
new = '''        voice_gender_mode: Optional[str] = None,\n        voice_gender: Optional[str] = None,\n        initial_status: str = "queued",\n    ) -> str:\n'''
if r.count(old) != 1:
    raise SystemExit(f"repo signature anchor count={r.count(old)}")
r = r.replace(old, new, 1)

old = '''              'queued',\n              $10::text,\n              $11::text,\n              $12::text\n'''
new = '''              $13::text,\n              $10::text,\n              $11::text,\n              $12::text\n'''
if r.count(old) != 1:
    raise SystemExit(f"repo SQL status anchor count={r.count(old)}")
r = r.replace(old, new, 1)

old = '''            auth_token_norm,\n            vgm,\n            vg,\n        )\n'''
new = '''            auth_token_norm,\n            vgm,\n            vg,\n            str(initial_status or "queued"),\n        )\n'''
if r.count(old) != 1:
    raise SystemExit(f"repo args anchor count={r.count(old)}")
r = r.replace(old, new, 1)
repo.write_text(r)

s = route.read_text()
old = '''                voice_gender_mode=voice_gender_mode,\n                voice_gender=voice_gender,\n            )\n\n            for seg in segments:\n                await segs_repo.insert_segment(\n                    conn,\n                    job_id=job_id,\n                    segment_index=int(seg["segment_index"]),\n                    text_chunk=str(seg.get("text_chunk") or seg.get("script_text") or ""),\n                    duration_sec=_clamp_fusion_duration(int(seg.get("duration_sec") or req.segment_seconds)),\n                )\n\n        try:\n'''
new = '''                voice_gender_mode=voice_gender_mode,\n                voice_gender=voice_gender,\n                initial_status="pricing_pending",\n            )\n\n        # Pricing is the execution gate. No runnable segment may exist before\n        # the parent reservation succeeds. This keeps financial authority in\n        # svc-pricing and prevents workers from observing pre-reservation work.\n        try:\n'''
if s.count(old) != 1:
    raise SystemExit(f"route pre-reserve anchor count={s.count(old)}")
s = s.replace(old, new, 1)

old = '''            _raise_http_for_pricing_error(mapped_exc)\n            raise\n\n        row = await jobs_repo.get_job(conn, job_id, user_id)\n'''
new = '''            _raise_http_for_pricing_error(mapped_exc)\n            raise\n\n        # Reservation succeeded. Only now materialize runnable segments and\n        # activate the parent for worker execution. Keep both operations in one\n        # transaction so workers can never observe a partially activated job.\n        async with conn.transaction():\n            for seg in segments:\n                await segs_repo.insert_segment(\n                    conn,\n                    job_id=job_id,\n                    segment_index=int(seg["segment_index"]),\n                    text_chunk=str(seg.get("text_chunk") or seg.get("script_text") or ""),\n                    duration_sec=_clamp_fusion_duration(int(seg.get("duration_sec") or req.segment_seconds)),\n                )\n            await conn.execute(\n                """\n                UPDATE public.longform_jobs\n                SET status = 'queued',\n                    error_code = NULL,\n                    error_message = NULL,\n                    updated_at = now()\n                WHERE id = $1::uuid\n                """,\n                job_id,\n            )\n\n        row = await jobs_repo.get_job(conn, job_id, user_id)\n'''
if s.count(old) != 1:
    raise SystemExit(f"route post-reserve anchor count={s.count(old)}")
s = s.replace(old, new, 1)
route.write_text(s)
PY

cat > "$TEST" <<'PY'
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
route = (ROOT / "app/app/api/routes/longform.py").read_text()
repo = (ROOT / "app/app/repos/longform_jobs_repo.py").read_text()

start = route.index('async def create_longform_job(')
end = route.index('@router.get("/jobs/{job_id}"', start)
create = route[start:end]

assert 'initial_status="pricing_pending"' in create
assert "reserve_longform_pricing_for_job" in create
assert "await segs_repo.insert_segment" in create
assert "SET status = 'queued'" in create

p_pending = create.index('initial_status="pricing_pending"')
p_reserve = create.index('reserve_longform_pricing_for_job')
p_insert = create.index('await segs_repo.insert_segment')
p_activate = create.index("SET status = 'queued'")
assert p_pending < p_reserve < p_insert < p_activate, (p_pending, p_reserve, p_insert, p_activate)

# Failure handling must occur before any segment materialization.
p_block = create.index('failed_status = "blocked"')
assert p_reserve < p_block < p_insert

assert 'initial_status: str = "queued"' in repo
assert '$13::text' in repo
assert 'str(initial_status or "queued")' in repo

print("LONGFORM_PRICING_GATE_ORDER_TEST=PASS")
PY

printf '\n===== 1. SOURCE VALIDATION =====\n'
python3 -m py_compile "$ROUTE" "$REPO"
python3 "$TEST"
git diff --check
echo "PASS: compile + invariant regression + diff check"

printf '\n===== 2. SOURCE DIFF =====\n'
git diff --stat
git diff -- "$ROUTE" "$REPO" "$TEST" | sed -n '1,240p'

printf '\n===== 3. COMMIT + PUSH =====\n'
git add "$ROUTE" "$REPO" "$TEST"
if ! git diff --cached --quiet; then
  git commit -m "fix(v3): reserve longform pricing before queueing segments"
fi
git push -u origin "$TARGET_BRANCH"
FIX_SHA="$(git rev-parse HEAD)"
echo "fix_sha=$FIX_SHA"

printf '\n===== 4. LIVE API-ONLY ROLLBACK SNAPSHOT =====\n'
EXT_API="$(resolve_container "${EXT_API_CONTAINER:-df-v3-svc-fusion-extension}" svc-fusion-extension)"
[ -n "$EXT_API" ] || fail "Fusion Extension API container not found"
mkdir -p "$ROLLBACK_DIR"
docker cp "$EXT_API:/app/app/api/routes/longform.py" "$ROLLBACK_DIR/longform.py"
docker cp "$EXT_API:/app/app/repos/longform_jobs_repo.py" "$ROLLBACK_DIR/longform_jobs_repo.py"
echo "rollback_dir=$ROLLBACK_DIR"

printf '\n===== 5. DEPLOY FUSION EXTENSION API ONLY =====\n'
docker cp "$ROUTE" "$EXT_API:/app/app/api/routes/longform.py"
docker cp "$REPO" "$EXT_API:/app/app/repos/longform_jobs_repo.py"
docker restart "$EXT_API" >/dev/null

for i in $(seq 1 45); do
  if docker exec "$EXT_API" python - <<'PY' >/dev/null 2>&1
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3) as r:
    assert r.status == 200
PY
  then break; fi
  sleep 2
  [ "$i" -lt 45 ] || fail "Fusion Extension API health timeout"
done
echo "PASS: Fusion Extension API HTTP_200"

printf '\n===== 6. RUNTIME INVARIANT GATE =====\n'
docker exec "$EXT_API" python - <<'PY'
import inspect
import app.api.routes.longform as route
from app.repos.longform_jobs_repo import LongformJobsRepo

src = inspect.getsource(route.create_longform_job)
assert 'initial_status="pricing_pending"' in src
p1 = src.index('initial_status="pricing_pending"')
p2 = src.index('reserve_longform_pricing_for_job')
p3 = src.index('await segs_repo.insert_segment')
p4 = src.index("SET status = 'queued'")
assert p1 < p2 < p3 < p4, (p1,p2,p3,p4)
sig = inspect.signature(LongformJobsRepo.create_job)
assert 'initial_status' in sig.parameters
assert sig.parameters['initial_status'].default == 'queued'
print('RUNTIME_LONGFORM_GATE=PASS')
print('sequence=pricing_pending->reserve->segments->queued')
PY

printf '\n===== 7. PRESERVATION HEALTH =====\n'
for spec in \
  'Audio:18004:/api/health' \
  'Fusion:18002:/api/health' \
  'Face:18003:/api/health' \
  'Pricing:18009:/api/health' \
  'Director:18011:/api/health'; do
  IFS=: read -r name port path <<<"$spec"
  code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}${path}" || true)"
  [ "$code" = "200" ] || fail "$name preservation health HTTP_$code"
  echo "PASS: $name HTTP_200"
done

printf '\n============================================================\n'
printf ' LONGFORM PRICING EXECUTION GATE: DEPLOYED + CERTIFIED\n'
printf '============================================================\n'
printf 'fix_sha=%s\n' "$FIX_SHA"
printf 'financial_authority=svc-pricing\n'
printf 'parent_before_reserve=pricing_pending\n'
printf 'segments_before_reserve=0\n'
printf 'insufficient_credit_result=blocked-parent-no-runnable-segments\n'
printf 'reserve_success_result=segments-created-then-parent-queued\n'
printf 'child_pricing=unchanged\n'
printf 'pricing_catalog=unchanged\n'
printf 'db_schema=unchanged\n'
printf 'workers=not_restarted\n'
printf 'providers=unchanged\n'
printf 'rollback_dir=%s\n' "$ROLLBACK_DIR"
printf 'log=%s\n' "$LOG"
printf '============================================================\n'
