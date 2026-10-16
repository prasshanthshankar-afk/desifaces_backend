#!/usr/bin/env bash
# DEV-only real turn-1 face crop validation. Not a SyncNet model inference.
set -Eeuo pipefail
[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "DEV_HOST_REQUIRED"; exit 2; }
ATTEMPT="992b2904-8ddf-4788-8e1e-1aa0d076fb9a"
MODEL_SHA="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
REPO="prasshanthshankar-afk/desifaces_backend"
REF="f0f1d7ccc7c4fd7068232a5472e6790c4050991d"
TMP="$(mktemp -d)"
chmod 700 "$TMP"
trap 'rm -rf "$TMP"' EXIT
docker exec -i -e QC_ATTEMPT="$ATTEMPT" df-svc-fusion-extension python - <<'PY' > "$TMP/original.mp4"
import asyncio,json,os,sys
from urllib.parse import urlparse,unquote
import asyncpg
from azure.storage.blob import BlobServiceClient
async def lookup():
    con=await asyncpg.connect(os.environ["DATABASE_URL"],
        server_settings={"default_transaction_read_only":"on"})
    try:
        raw=await con.fetchval("select metadata_json from public.v3_studio_stage_attempts where attempt_id=$1::uuid",os.environ["QC_ATTEMPT"])
        metadata=json.loads(raw) if isinstance(raw,str) else raw
        children=metadata["children"]
        if len(children)!=6:raise RuntimeError("CHILD_COUNT_INVALID")
        return children[0]["video_url"]
    finally:
        await con.close()
url=asyncio.run(lookup())
p=urlparse(url)
service=BlobServiceClient.from_connection_string(os.environ["AZURE_STORAGE_CONNECTION_STRING"])
if p.scheme!="https" or p.hostname!=urlparse(service.url).hostname:
    raise RuntimeError("STORAGE_ACCOUNT_MISMATCH")
parts=p.path.lstrip("/").split("/",1)
if len(parts)!=2:raise RuntimeError("INVALID_BLOB_PATH")
client=service.get_blob_client(unquote(parts[0]),unquote(parts[1]))
properties=client.get_blob_properties()
limit=20*1024*1024
if not 0<properties.size<=limit:raise RuntimeError("VIDEO_TOO_LARGE")
read=0
for part in client.download_blob(max_concurrency=1).chunks():
    read+=len(part)
    if read>limit:raise RuntimeError("DOWNLOAD_TOO_LARGE")
    sys.stdout.buffer.write(part)
if read!=properties.size:raise RuntimeError("INCOMPLETE_DOWNLOAD")
PY
test -s "$TMP/original.mp4"
curl -fLsS --retry 2 --max-time 60 \
  "https://huggingface.co/opencv/opencv_zoo/resolve/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
  -o "$TMP/yunet.onnx"
echo "$MODEL_SHA  $TMP/yunet.onnx" | sha256sum -c -
gh api "repos/$REPO/contents/scripts/ops/dev_cpu_syncnet_face_crop.py?ref=$REF" \
  --jq .content | base64 -d > "$TMP/crop.py"
python3 -m py_compile "$TMP/crop.py"
IMAGE="$(docker inspect -f '{{.Image}}' df-svc-fusion-extension)"
# 'left' is a spatial test selection only, NOT verified Maya/Arjun identity.
docker run --rm --network none --cpus 1 --memory 1g --pids-limit 128 \
  --user "$(id -u):$(id -g)" --security-opt no-new-privileges --cap-drop ALL \
  -e DESIFACES_DEV_VERIFIED=1 --mount "type=bind,src=$TMP,dst=/qc" \
  --entrypoint python "$IMAGE" /qc/crop.py \
  --video /qc/original.mp4 --model /qc/yunet.onnx \
  --model-sha256 "$MODEL_SHA" --speaker-side left \
  --output /qc/left-prepared.mp4 --max-seconds 20
test -s "$TMP/left-prepared.mp4"
# Verify output inside the image (host lacks ffprobe).
docker run --rm --network none --cpus 1 --memory 512m \
  --user "$(id -u):$(id -g)" --mount "type=bind,src=$TMP,dst=/qc,readonly" \
  --entrypoint ffprobe "$IMAGE" -v error -show_entries \
  stream=codec_type,width,height,avg_frame_rate,sample_rate,channels \
  -of json /qc/left-prepared.mp4 > "$TMP/probe.json"
python3 - "$TMP/probe.json" <<'PY'
import json,sys
tracks=json.load(open(sys.argv[1]))["streams"]
v=next((x for x in tracks if x["codec_type"]=="video"),None)
a=next((x for x in tracks if x["codec_type"]=="audio"),None)
assert v is not None and (v["width"],v["height"],v["avg_frame_rate"])==(224,224,"25/1"), v
assert a is not None and (int(a["sample_rate"]),int(a["channels"]))==(16000,1), a
print("CROP_FORMAT_TEST=PASS")
print("SPEAKER_IDENTITY_VERIFIED=NO")
print("SYNCNET_MODEL_INFERENCE_EXECUTED=NO")
print("LIPSYNC_CERTIFIED=NO")
PY
echo "DEV_TURN1_PREPROCESS_VALIDATION=PASS"
