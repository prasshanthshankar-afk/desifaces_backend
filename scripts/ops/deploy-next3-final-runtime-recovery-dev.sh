#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_SHA="${1:-}"
WEB_SHA="${2:-}"
EXPECTED_HOST="desifaces-dev"
LIVE_ENV="/home/azureuser/workspace/desifaces-v3/infra/.env"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DURABLE_ROOT="/home/azureuser/workspace/desifaces-next3-final-runtime-${STAMP}"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
[[ "$BACKEND_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "exact backend SHA required"
[[ "$WEB_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "exact web SHA required"
[[ -f "$LIVE_ENV" ]] || fail "missing DEV V3 runtime env"
command -v docker >/dev/null || fail "docker missing"
command -v git >/dev/null || fail "git missing"
command -v python3 >/dev/null || fail "python3 missing"

FREE_KB="$(df -Pk / | awk 'NR==2{print $4}')"
(( FREE_KB >= 20*1024*1024 )) || fail "need at least 20 GiB free before video-runtime deployment"

TARGETS=(
  df-v3-svc-director
  df-v3-svc-director-worker
  df-v3-svc-fusion-worker
  df-v3-svc-fusion-extension-stitch-worker
)
for c in "${TARGETS[@]}"; do
  docker inspect "$c" >/dev/null 2>&1 || fail "required current DEV container missing: $c"
done

UNTOUCHED=(
  df-v3-svc-face
  df-v3-svc-face-worker
  df-v3-svc-audio
  df-v3-svc-audio-worker
  df-v3-svc-pricing
  df-v3-svc-core
  df-v3-svc-dashboard
  df-v3-svc-fusion
  df-v3-svc-fusion-extension
  desifaces-v3-db
  desifaces-v3-redis
)
declare -A UNTOUCHED_BEFORE
for c in "${UNTOUCHED[@]}"; do
  if docker inspect "$c" >/dev/null 2>&1; then
    UNTOUCHED_BEFORE["$c"]="$(docker inspect -f '{{.State.StartedAt}}|{{.Image}}|{{.RestartCount}}' "$c")"
  fi
done

OLD_FUSION_WORKER_CONCURRENCY="$(
  docker inspect df-v3-svc-fusion-worker --format '{{range .Config.Env}}{{println .}}{{end}}' |
  awk -F= '$1=="DF_FUSION_WORKER_CONCURRENCY"{print $2; exit}'
)"
[[ -n "$OLD_FUSION_WORKER_CONCURRENCY" ]] || OLD_FUSION_WORKER_CONCURRENCY=""

echo "============================================================"
echo " desifaces #next3 — FINAL DEV RUNTIME RECOVERY"
echo " host=$(hostname -s)"
echo " backend_sha=$BACKEND_SHA"
echo " web_sha=$WEB_SHA"
echo " durable_root=$DURABLE_ROOT"
echo " production_touch=NONE"
echo " database_migration=NONE"
echo "============================================================"

mkdir -p "$DURABLE_ROOT"

LIVE_ROOT="/home/azureuser/workspace/desifaces-v3"

copy_runtime_tree(){
  local container="$1" dest="$2" source_dir fallback_reason=""
  source_dir="$(docker inspect "$container" -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"

  # Older DEV cutovers were launched from ephemeral /tmp worktrees. Those
  # source directories can disappear while the containers continue running.
  # Reconstruct from the canonical DEV V3 repository, then overlay the exact
  # currently-running service code from the container below.
  if [[ -z "$source_dir" || ! -d "$source_dir" ]]; then
    [[ -d "$LIVE_ROOT/.git" || -f "$LIVE_ROOT/.git" ]] || \
      fail "canonical DEV V3 repository unavailable: $LIVE_ROOT"
    fallback_reason="missing_ephemeral_compose_source"
    source_dir="$LIVE_ROOT"
  fi

  mkdir -p "$dest"
  cp -a --reflink=auto "$source_dir/." "$dest/"
  mkdir -p "$dest/infra"
  rm -f "$dest/infra/.env"
  ln -s "$LIVE_ENV" "$dest/infra/.env"

  echo "RUNTIME_TREE_SOURCE container=$container source=$source_dir fallback=${fallback_reason:-none}"
}

DIRECTOR_ROOT="$DURABLE_ROOT/director"
FUSION_ROOT="$DURABLE_ROOT/fusion-worker"
STITCH_ROOT="$DURABLE_ROOT/stitch-worker"

copy_runtime_tree df-v3-svc-director "$DIRECTOR_ROOT"
copy_runtime_tree df-v3-svc-fusion-worker "$FUSION_ROOT"
copy_runtime_tree df-v3-svc-fusion-extension-stitch-worker "$STITCH_ROOT"

echo "DURABLE_RUNTIME_RECONSTRUCTION=PASS"

# The running containers are the no-regression source of truth. Overlay only the
# affected service source from those containers, then apply the bounded repair.
rm -rf "$DIRECTOR_ROOT/services/svc-director/app/app"
mkdir -p "$DIRECTOR_ROOT/services/svc-director/app/app"
docker cp df-v3-svc-director:/app/app/. "$DIRECTOR_ROOT/services/svc-director/app/app/"

rm -rf "$STITCH_ROOT/services/svc-fusion-extension/app/app"
mkdir -p "$STITCH_ROOT/services/svc-fusion-extension/app/app"
docker cp df-v3-svc-fusion-extension-stitch-worker:/app/app/. "$STITCH_ROOT/services/svc-fusion-extension/app/app/"

python3 - "$DIRECTOR_ROOT" <<'PY'
from pathlib import Path
import sys

root=Path(sys.argv[1])
route=root/"services/svc-director/app/app/studio_e2e_routes.py"
parallel=root/"services/svc-director/app/app/fusion_execution_parallel_dispatch.py"

text=route.read_text()
if "ParallelOrphanReconciledParentPricedSceneFusionExecutionService" not in text:
    old="from .fusion_execution import SceneFusionBridgeError, SceneFusionExecutionService\n"
    if old not in text:
        raise SystemExit("director route import contract changed; refusing unsafe patch")
    text=text.replace(
        old,
        "from .fusion_execution import SceneFusionBridgeError\n"
        "from .fusion_execution_parallel_dispatch import (\n"
        "    ParallelOrphanReconciledParentPricedSceneFusionExecutionService,\n"
        ")\n",
        1,
    )
old_ctor="fusion_execution = SceneFusionExecutionService(\n"
if old_ctor in text:
    text=text.replace(
        old_ctor,
        "fusion_execution = ParallelOrphanReconciledParentPricedSceneFusionExecutionService(\n",
        1,
    )
if "fusion_execution = ParallelOrphanReconciledParentPricedSceneFusionExecutionService(" not in text:
    raise SystemExit("director resilient executor wiring not established")
route.write_text(text)

text=parallel.read_text()
marker="retry_scope\": \"failed_child_only"
if "A terminal child failure has already been persisted" not in text:
    old="""        result = await super().sync(
            pool,
            account_id=account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
            headers=headers,
        )

        async with pool.acquire() as conn:
"""
    if old not in text:
        raise SystemExit("parallel sync contract changed; refusing unsafe patch")
    new="""        try:
            result = await super().sync(
                pool,
                account_id=account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
                headers=headers,
            )
        except SceneFusionBridgeError as exc:
            # A terminal child failure has already been persisted by the resilient
            # execution chain and parent pricing has already been released by the
            # parent-priced layer. Return durable child states instead of a 409 so
            # successful renders remain visible and retryable without regeneration.
            if str(exc) != "fusion_child_job_failed":
                raise
            async with pool.acquire() as conn:
                context = await load_fusion_scene_context(
                    conn,
                    account_id=account_id,
                    workflow_id=workflow_id,
                    stage_run_id=stage_run_id,
                )
                failed_row = await conn.fetchrow(
                    '''
                    select created_at,metadata_json
                    from public.v3_studio_stage_attempts
                    where stage_run_id=$1
                    order by attempt_no desc
                    limit 1
                    ''',
                    stage_run_id,
                )
            failed_meta = _as_dict(failed_row["metadata_json"]) if failed_row else {}
            failed_children = list(failed_meta.get("children") or [])
            preserved_count = sum(
                1
                for item in failed_children
                if _clean(item.get("status")).lower() in _TERMINAL_SUCCESS
                and bool(_clean(item.get("video_url")))
            )
            result = {
                "workflow_id": str(workflow_id),
                "stage_run_id": str(stage_run_id),
                "scene_id": str(context.scene_id),
                "provider_state": "failed",
                "stage_state": "failed",
                "media_asset_id": None,
                "video_url": None,
                "review_item_id": None,
                "review_decision": None,
                "children": failed_children,
                "error_code": "fusion_child_failed",
                "error_message": "one_or_more_child_fusion_jobs_failed",
                "retryable": True,
                "retry_scope": "failed_child_only",
                "preserved_child_count": preserved_count,
            }

        async with pool.acquire() as conn:
"""
    text=text.replace(old,new,1)
if marker not in text:
    raise SystemExit("failed-child retry status contract missing after patch")
parallel.write_text(text)
PY

python3 - "$STITCH_ROOT" <<'PY'
from pathlib import Path
import re, sys

path=Path(sys.argv[1])/"services/svc-fusion-extension/app/app/services/stitch_service.py"
text=path.read_text()

m=re.search(r"(?ms)^def _xfade_pair\(.*?(?=^def |\Z)", text)
if not m:
    raise SystemExit("_xfade_pair not found")

required_helpers=(
    "_require_nonempty_file",
    "_ensure_parent_dir",
    "_probe_duration_seconds",
    "_safe_transition_duration",
    "_transition_style",
    "_transition_audio_curve",
    "_run",
    "_ffmpeg_threads",
    "_ffmpeg_preset",
    "_ffmpeg_crf",
)
missing=[name for name in required_helpers if name not in text]
if missing:
    raise SystemExit("xfade helper contract missing: "+",".join(missing))

replacement = '''def _xfade_pair(
    left_mp4: str,
    right_mp4: str,
    out_mp4: str,
    *,
    transition_duration_sec: float,
) -> None:
    _require_nonempty_file(left_mp4)
    _require_nonempty_file(right_mp4)
    _ensure_parent_dir(out_mp4)

    left_duration = _probe_duration_seconds(left_mp4)
    right_duration = _probe_duration_seconds(right_mp4)
    if left_duration is None:
        raise RuntimeError(f"Unable to probe duration for {left_mp4}")
    if right_duration is None:
        raise RuntimeError(f"Unable to probe duration for {right_mp4}")

    xfade_duration = _safe_transition_duration(
        transition_duration_sec, left_duration, right_duration
    )
    offset = max(0.0, float(left_duration) - xfade_duration)
    transition_style = _transition_style()
    audio_curve = _transition_audio_curve()

    filter_complex = (
        f"[0:v]fps=30,settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[v0];"
        f"[1:v]fps=30,settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[v1];"
        f"[v0][v1]xfade=transition={transition_style}:duration={xfade_duration:.3f}:offset={offset:.3f}[v];"
        f"[0:a]aresample=48000,asetpts=PTS-STARTPTS[a0];"
        f"[1:a]aresample=48000,asetpts=PTS-STARTPTS[a1];"
        f"[a0][a1]acrossfade=d={xfade_duration:.3f}:c1={audio_curve}:c2={audio_curve}[a]"
    )

    _run([
        "ffmpeg", "-y",
        "-threads", _ffmpeg_threads(),
        "-i", left_mp4,
        "-i", right_mp4,
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "[a]",
        "-c:v", "libx264",
        "-preset", _ffmpeg_preset(),
        "-crf", _ffmpeg_crf(),
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-movflags", "+faststart",
        out_mp4,
    ])
    _require_nonempty_file(out_mp4)

'''

text=text[:m.start()]+replacement+text[m.end():]
patched=re.search(r"(?ms)^def _xfade_pair\(.*?(?=^def |\Z)", text)
if not patched:
    raise SystemExit("xfade patch disappeared")
patched_text=patched.group(0)
for marker in ("fps=30", "settb=AVTB", "setpts=PTS-STARTPTS", "aresample=48000", "xfade=transition="):
    if marker not in patched_text:
        raise SystemExit("CFR xfade marker missing: "+marker)
path.write_text(text)
PY
python3 - "$FUSION_ROOT" <<'PY'
from pathlib import Path
import re, sys

root=Path(sys.argv[1])
v3=root/"docker-compose.v3.yml"
text=v3.read_text()
m=re.search(r"(?ms)^  svc-fusion-worker:\n(.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)", text)
if not m:
    raise SystemExit("svc-fusion-worker V3 override missing")
block=m.group(0)
if "DF_SYNC3_PROVIDER_CONCURRENCY:" in block:
    block=re.sub(
        r"(?m)^(\s*DF_SYNC3_PROVIDER_CONCURRENCY:\s*).*$",
        r'\1"1"',
        block,
        count=1,
    )
else:
    env=re.search(r"(?m)^(\s+environment:\s*)$", block)
    if not env:
        raise SystemExit("svc-fusion-worker environment block missing")
    insert='\n      DF_SYNC3_PROVIDER_CONCURRENCY: "1"\n      DF_SYNC3_CONCURRENCY_WAIT_SECONDS: "900"'
    block=block[:env.end()]+insert+block[env.end():]
text=text[:m.start()]+block+text[m.end():]
v3.write_text(text)
PY

python3 - "$DIRECTOR_ROOT" <<'PY'
from pathlib import Path
import re, sys
path=Path(sys.argv[1])/"docker-compose.v3.yml"
text=path.read_text()
for service in ("svc-director","svc-director-worker"):
    m=re.search(rf"(?ms)^  {re.escape(service)}:\n(.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)", text)
    if not m:
        raise SystemExit(f"{service} override missing")
    block=m.group(0)
    if "restart:" not in block:
        lines=block.splitlines()
        lines.insert(2,'    restart: unless-stopped')
        block="\n".join(lines)+"\n"
        text=text[:m.start()]+block+text[m.end():]
path.write_text(text)
PY

python3 -m py_compile   "$DIRECTOR_ROOT/services/svc-director/app/app/studio_e2e_routes.py"   "$DIRECTOR_ROOT/services/svc-director/app/app/fusion_execution_parallel_dispatch.py"   "$STITCH_ROOT/services/svc-fusion-extension/app/app/services/stitch_service.py"
echo "PATCH_SYNTAX_GATE=PASS"

compose(){
  local root="$1" project="$2"; shift 2
  docker compose     --env-file "$LIVE_ENV"     -f "$root/docker-compose.yml"     -f "$root/docker-compose.v3.yml"     -p "$project" "$@"
}

DIRECTOR_PROJECT="$(docker inspect df-v3-svc-director -f '{{index .Config.Labels "com.docker.compose.project"}}')"
FUSION_PROJECT="$(docker inspect df-v3-svc-fusion-worker -f '{{index .Config.Labels "com.docker.compose.project"}}')"
STITCH_PROJECT="$(docker inspect df-v3-svc-fusion-extension-stitch-worker -f '{{index .Config.Labels "com.docker.compose.project"}}')"

compose "$DIRECTOR_ROOT" "$DIRECTOR_PROJECT" config >/tmp/df-next3-final-director-compose.yml
compose "$FUSION_ROOT" "$FUSION_PROJECT" config >/tmp/df-next3-final-fusion-compose.yml
compose "$STITCH_ROOT" "$STITCH_PROJECT" config >/tmp/df-next3-final-stitch-compose.yml
echo "COMPOSE_PREFLIGHT=PASS"

compose "$DIRECTOR_ROOT" "$DIRECTOR_PROJECT" build svc-director svc-director-worker
compose "$STITCH_ROOT" "$STITCH_PROJECT" --profile v3-execution build svc-fusion-extension-stitch-worker
echo "TARGET_IMAGE_BUILD=PASS"

declare -A OLD_STATE OLD_RESTART ROLLBACK
for c in "${TARGETS[@]}"; do
  OLD_STATE["$c"]="$(docker inspect -f '{{.State.Status}}' "$c")"
  OLD_RESTART["$c"]="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c")"
  rb="${c}-rollback-${STAMP}"
  ROLLBACK["$c"]="$rb"
  docker stop "$c" >/dev/null 2>&1 || true
  docker rename "$c" "$rb"
  docker update --restart=no "$rb" >/dev/null
done

rollback(){
  local rc=$?
  set +e
  echo "===== AUTOMATIC DEV ROLLBACK ====="
  for c in "${TARGETS[@]}"; do
    docker rm -f "$c" >/dev/null 2>&1 || true
    rb="${ROLLBACK[$c]}"
    if docker inspect "$rb" >/dev/null 2>&1; then
      docker rename "$rb" "$c" >/dev/null
      policy="${OLD_RESTART[$c]}"
      [[ -n "$policy" ]] || policy="no"
      docker update --restart="$policy" "$c" >/dev/null 2>&1 || true
      [[ "${OLD_STATE[$c]}" == "running" ]] && docker start "$c" >/dev/null 2>&1 || true
    fi
  done
  echo "DEV_ROLLBACK=COMPLETE"
  exit "$rc"
}
trap rollback ERR

export DF_SYNC3_PROVIDER_CONCURRENCY=1

compose "$DIRECTOR_ROOT" "$DIRECTOR_PROJECT" up -d --no-deps --force-recreate svc-director
compose "$DIRECTOR_ROOT" "$DIRECTOR_PROJECT" --profile v3-orchestration up -d --no-deps --force-recreate svc-director-worker
compose "$FUSION_ROOT" "$FUSION_PROJECT" --profile v3-execution up -d --no-deps --force-recreate svc-fusion-worker
compose "$STITCH_ROOT" "$STITCH_PROJECT" --profile v3-execution up -d --no-deps --force-recreate svc-fusion-extension-stitch-worker

for _ in $(seq 1 45); do
  all_running=1
  for c in "${TARGETS[@]}"; do
    [[ "$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true)" == "running" ]] || all_running=0
  done
  (( all_running == 1 )) && break
  sleep 2
done
(( all_running == 1 )) || fail "target runtime did not become running"

docker exec -i df-v3-svc-director python - <<'PY'
from app.studio_e2e_routes import fusion_execution
name=type(fusion_execution).__name__
assert name == "ParallelOrphanReconciledParentPricedSceneFusionExecutionService", name
print("NEXT3_RESILIENT_PARALLEL_EXECUTOR=PASS")
PY

docker exec -i df-v3-svc-director python - <<'PY'
from pathlib import Path
p=Path("/app/app/fusion_execution_parallel_dispatch.py")
s=p.read_text()
assert "failed-child-only retry" in s or '"retry_scope": "failed_child_only"' in s
assert "A terminal child failure has already been persisted" in s
print("NEXT3_FAILED_CHILD_STATUS_CONTRACT=PASS")
PY

SYNC_LIMIT="$(
  docker inspect df-v3-svc-fusion-worker --format '{{range .Config.Env}}{{println .}}{{end}}' |
  awk -F= '$1=="DF_SYNC3_PROVIDER_CONCURRENCY"{print $2; exit}'
)"
[[ "$SYNC_LIMIT" == "1" ]] || fail "Sync3 provider concurrency is not 1: $SYNC_LIMIT"
NEW_WORKER_CONCURRENCY="$(
  docker inspect df-v3-svc-fusion-worker --format '{{range .Config.Env}}{{println .}}{{end}}' |
  awk -F= '$1=="DF_FUSION_WORKER_CONCURRENCY"{print $2; exit}'
)"
[[ "$NEW_WORKER_CONCURRENCY" == "$OLD_FUSION_WORKER_CONCURRENCY" ]] ||   fail "general Fusion worker concurrency changed: before=$OLD_FUSION_WORKER_CONCURRENCY after=$NEW_WORKER_CONCURRENCY"
echo "SYNC3_PROVIDER_CONCURRENCY=1"
echo "FUSION_WORKER_CONCURRENCY_PRESERVED=${NEW_WORKER_CONCURRENCY:-unset}"

docker exec -i df-v3-svc-fusion-extension-stitch-worker python - <<'PY'
import inspect
from app.services import stitch_service
src=inspect.getsource(stitch_service._xfade_pair)
assert "fps=30" in src
src2=inspect.getsource(stitch_service.stitch_videos)
assert "concat" in src2
print("NEXT3_CFR_XFADE_GUARD=PASS")
print("NEXT3_STITCH_FALLBACK_PRESERVED=PASS")
PY

[[ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' df-v3-svc-director-worker)" == "unless-stopped" ]] ||   fail "director worker restart policy not durable"
echo "DIRECTOR_WORKER_DURABILITY=PASS"

for c in "${UNTOUCHED[@]}"; do
  [[ -n "${UNTOUCHED_BEFORE[$c]:-}" ]] || continue
  after="$(docker inspect -f '{{.State.StartedAt}}|{{.Image}}|{{.RestartCount}}' "$c")"
  [[ "$after" == "${UNTOUCHED_BEFORE[$c]}" ]] || fail "no-regression guard changed untouched runtime: $c"
done
echo "BACKEND_NO_REGRESSION_RUNTIME_GUARD=PASS"

# Deploy the exact web repair SHA through the already-certified candidate/rollback
# web deployment path. The web branch is based on the currently active DEV SHA.
WEB_ROOT=""
for candidate in   /home/azureuser/workspace/desifaces_web   /home/azureuser/workspace/desifaces-web   /home/azureuser/workspace/desifaces_frontend
do
  [[ -d "$candidate/.git" || -f "$candidate/.git" ]] || continue
  remote="$(git -C "$candidate" remote get-url origin 2>/dev/null || true)"
  if [[ "$remote" == *"prasshanthshankar-afk/desifaces_web"* ]]; then
    WEB_ROOT="$candidate"
    break
  fi
done
[[ -n "$WEB_ROOT" ]] || fail "desifaces_web repository not found"

WEB_DEPLOY="/tmp/deploy-next3-web-dev-${WEB_SHA}.sh"
git -C "$WEB_ROOT" fetch --no-tags origin "$WEB_SHA"
git -C "$WEB_ROOT" show "$WEB_SHA:scripts/ops/deploy-next3-web-dev.sh" > "$WEB_DEPLOY"
chmod 700 "$WEB_DEPLOY"
WEB_REPO_ROOT="$WEB_ROOT" bash "$WEB_DEPLOY" "$WEB_SHA"
rm -f "$WEB_DEPLOY"
echo "NEXT3_WEB_STATUS_RECONCILIATION_DEPLOY=PASS"

df -h /
echo "============================================================"
echo " NEXT3_FINAL_FIX_DEPLOY=PASS"
echo " EXISTING_SUCCESSFUL_CHILDREN=PRESERVED_BY_RETRY_CONTRACT"
echo " SYNC3_PROVIDER_CONCURRENCY=1"
echo " GENERAL_FUSION_WORKER_CONCURRENCY=PRESERVED"
echo " CFR_STITCH_GUARD=ENABLED"
echo " DIRECTOR_WORKER_DURABLE=PASS"
echo " PRODUCTION_TOUCH=NONE"
echo "============================================================"

trap - ERR
