#!/usr/bin/env bash
# DEV-only read-only six-turn YuNet evaluation. No fusion service changes.
set -Eeuo pipefail
[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "DEV_HOST_REQUIRED" >&2; exit 2; }
command -v docker >/dev/null
command -v gh >/dev/null
command -v curl >/dev/null
REPO="prasshanthshankar-afk/desifaces_backend"
REF="feature/dev-sarvam-contextual-video-qc-20261013"
SHA="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
ATTEMPT="992b2904-8ddf-4788-8e1e-1aa0d076fb9a"
ROOT="${HOME}/desifaces-qc-evidence"
install -d -m 700 "$ROOT"
RUN="$ROOT/$(date -u +%Y%m%dT%H%M%SZ)-${ATTEMPT:0:8}"
mkdir -m 700 "$RUN"
TMP="$(mktemp -d)"
chmod 700 "$TMP"
trap 'rm -rf "$TMP"' EXIT
echo "EVIDENCE_DIR=$RUN"
curl -fLsS --retry 2 --max-time 60 \
  https://huggingface.co/opencv/opencv_zoo/resolve/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
  -o "$TMP/yunet.onnx"
echo "$SHA  $TMP/yunet.onnx" | sha256sum -c -
gh api "repos/$REPO/contents/scripts/ops/dev_cpu_yunet_face_evidence.py?ref=$REF" --jq .content |
  base64 -d > "$TMP/analyzer.py"
python3 -m py_compile "$TMP/analyzer.py"
IMAGE="$(docker inspect -f '{{.Image}}' df-svc-fusion-extension)"
for N in 1 2 3 4 5 6; do
  echo "===== TURN $N ====="
  docker exec -i -e QC_TURN="$N" -e QC_ATTEMPT="$ATTEMPT" df-svc-fusion-extension python - <<'PY' > "$TMP/turn.mp4"
import os, asyncio, json, sys
from urllib.parse import urlparse, unquote
import asyncpg
from azure.storage.blob import BlobServiceClient
async def lookup():
    con = await asyncpg.connect(os.environ["DATABASE_URL"],
        timeout=10, command_timeout=20,
        server_settings={"default_transaction_read_only":"on"})
    try:
        raw=await con.fetchval(
            "SELECT metadata_json FROM public.v3_studio_stage_attempts WHERE attempt_id=$1::uuid",
            os.environ["QC_ATTEMPT"])
        if raw is None: raise RuntimeError("ATTEMPT_MISSING")
        meta=json.loads(raw) if isinstance(raw,str) else raw
        children=meta.get("children")
        if not isinstance(children,list) or len(children)!=6: raise RuntimeError("CHILDREN_NOT_SIX")
        child=children[int(os.environ["QC_TURN"])-1]
        seq=child.get("sequence_no")
        if seq is not None and int(seq) not in (int(os.environ["QC_TURN"])-1,int(os.environ["QC_TURN"])):
            raise RuntimeError("TURN_ORDER_AMBIGUOUS")
        return child.get("video_url")
    finally:
        await con.close()
url=asyncio.run(lookup())
if not isinstance(url,str): raise RuntimeError("VIDEO_URL_MISSING")
p=urlparse(url)
service=BlobServiceClient.from_connection_string(os.environ["AZURE_STORAGE_CONNECTION_STRING"])
if p.scheme!="https" or p.hostname!=urlparse(service.url).hostname:
    raise RuntimeError("BLOB_ACCOUNT_MISMATCH")
parts=p.path.lstrip("/").split("/",1)
if len(parts)!=2: raise RuntimeError("BLOB_PATH_INVALID")
client=service.get_blob_client(container=unquote(parts[0]),blob=unquote(parts[1]))
props=client.get_blob_properties()
limit=20*1024*1024
if not 0<props.size<=limit or props.content_settings.content_type!="video/mp4":
    raise RuntimeError("INVALID_BLOB_SIZE_OR_CONTENT_TYPE")
count=0
for chunk in client.download_blob(max_concurrency=1).chunks():
    count+=len(chunk)
    if count>limit: raise RuntimeError("VIDEO_TOO_LARGE")
    sys.stdout.buffer.write(chunk)
if count!=props.size: raise RuntimeError("DOWNLOAD_INCOMPLETE")
PY
  test -s "$TMP/turn.mp4"
  # Run in separate CPU-limited container. Expected exit 1 means REVIEW_REQUIRED.
  set +e
  docker run --rm --network none --cpus 1 --memory 1g --pids-limit 128 \
    --user "$(id -u):$(id -g)" \
    --security-opt no-new-privileges --cap-drop ALL \
    -e DESIFACES_DEV_VERIFIED=1 \
    --mount "type=bind,src=$TMP,dst=/qc" \
    --entrypoint python "$IMAGE" /qc/analyzer.py \
    --video /qc/turn.mp4 --model /qc/yunet.onnx --model-sha256 "$SHA" \
    --output "/qc/turn-$N.json" --stride 5 --max-samples 600
  RC=$?
  set -e
  [[ "$RC" -eq 1 && -s "$TMP/turn-$N.json" ]] || { echo "TURN_${N}_ANALYSIS_FAILED exit=$RC" >&2; exit 1; }
  mv "$TMP/turn-$N.json" "$RUN/turn-$N.json"
  rm -f "$TMP/turn.mp4"
done
python3 - "$RUN" <<'PY'
import json,sys,pathlib,datetime
folder=pathlib.Path(sys.argv[1])
entries=[]
for n in range(1,7):
    r=json.loads((folder/f"turn-{n}.json").read_text())
    samples=r["samples"]
    counts=[len(s["detected_faces"]) for s in samples]
    entries.append({
        "turn":n,"sampled_frames":len(samples),
        "zero_faces":sum(c==0 for c in counts),
        "one_face":sum(c==1 for c in counts),
        "two_or_more_faces":sum(c>=2 for c in counts),
        "decision":r["decision"]})
report={
    "attempt_id":"992b2904-8ddf-4788-8e1e-1aa0d076fb9a",
    "method":"YuNet 2023mar CPU face detection at frame stride 5",
    "model_sha256":"8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    "visual_face_detection_collected":True,
    "speaker_identity_verified":False,
    "lip_sync_offset_measured":False,
    "decision":"REVIEW_REQUIRED",
    "turns":entries}
(folder/"summary.json").write_text(json.dumps(report,indent=2)+"\n")
print(json.dumps(report,indent=2))
PY
chmod 600 "$RUN"/*.json
echo "SIX_TURN_FACE_EVIDENCE_COLLECTED=YES"
echo "AUDIO_VISUAL_LIPSYNC_CERTIFIED=NO"
echo "EVIDENCE_DIR=$RUN"
