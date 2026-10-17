#!/usr/bin/env bash
# DEV-only OFFLINE re-stitch experiment. Existing live containers and production unaffected.
set -Eeuo pipefail
[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "DEV_HOST_REQUIRED"; exit 2; }
IMAGE="$(docker inspect -f '{{.Image}}' df-svc-fusion-extension)"
RUN="$HOME/desifaces-qc-evidence/offline-restitch-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 "$RUN"
echo "EVIDENCE_DIR=$RUN"
for TURN in 1 2 3 4 5 6; do
 echo "FETCH_TURN=$TURN"
 docker exec -i -e QC_TURN="$TURN" df-svc-fusion-extension python - <<'PY' > "$RUN/turn-$TURN.mp4"
import asyncio,json,os,sys
from urllib.parse import urlparse,unquote
import asyncpg
from azure.storage.blob import BlobServiceClient
async def get():
 c=await asyncpg.connect(os.environ["DATABASE_URL"],server_settings={"default_transaction_read_only":"on"})
 try:
  raw=await c.fetchval("SELECT metadata_json FROM public.v3_studio_stage_attempts WHERE attempt_id=$1::uuid","992b2904-8ddf-4788-8e1e-1aa0d076fb9a")
  m=json.loads(raw) if isinstance(raw,str) else raw
  if len(m["children"])!=6:raise RuntimeError("INVALID_LINEAGE")
  return m["children"][int(os.environ["QC_TURN"])-1]["video_url"]
 finally: await c.close()
p=urlparse(asyncio.run(get()))
svc=BlobServiceClient.from_connection_string(os.environ["AZURE_STORAGE_CONNECTION_STRING"])
if p.scheme!="https" or p.hostname!=urlparse(svc.url).hostname:raise RuntimeError("STORAGE_ACCOUNT_MISMATCH")
bits=p.path.lstrip("/").split("/",1)
if len(bits)!=2:raise RuntimeError("INVALID_BLOB_PATH")
blob=svc.get_blob_client(unquote(bits[0]),unquote(bits[1]))
size=blob.get_blob_properties().size
if not 0<size<=20*1024*1024:raise RuntimeError("BOUNDED_BLOB_REQUIRED")
count=0
for part in blob.download_blob(max_concurrency=1).chunks():
 count+=len(part)
 if count>20*1024*1024:raise RuntimeError("SIZE_LIMIT")
 sys.stdout.buffer.write(part)
if count!=size:raise RuntimeError("INCOMPLETE")
PY
 test -s "$RUN/turn-$TURN.mp4"
done
# Run exactly the source-code stitch implementation in a disposable container,
# isolated from production and DEV networking. Never upload.
docker run --rm --network none --cpus 2 --memory 2g --pids-limit 160 \
  --user "$(id -u):$(id -g)" --security-opt no-new-privileges --cap-drop ALL \
  -e LONGFORM_SEGMENT_STITCH_MODE=shared_dialogue \
  --mount "type=bind,src=$RUN,dst=/qc" --entrypoint python "$IMAGE" -c '
import json,os
from pathlib import Path
from app.services.stitch_service import normalize_segment_mp4,stitch_videos
inputs=[f"/qc/turn-{i}.mp4" for i in range(1,7)]
# Each normalized copy is retained for timeline/packet analysis.
for i,path in enumerate(inputs,1):
 normalize_segment_mp4(path,f"/qc/normalized-{i}.mp4",
   edge_fade_override=0.0,audio_edge_fade=False)
stitch_videos(inputs,"/qc/candidate-stitch.mp4",
 stitch_mode_override="shared_dialogue")
print("OFFLINE_STITCH=COMPLETE")
'
# Metadata-based comparison is not proof of word-level continuity.
docker run --rm --network none --cpus 1 --memory 512m \
 --user "$(id -u):$(id -g)" --mount "type=bind,src=$RUN,dst=/qc,readonly" \
 --entrypoint python "$IMAGE" -c '
import subprocess,json
def probe(name):
 p=subprocess.run(["ffprobe","-v","error","-show_format","-show_streams","-of","json",name],
  capture_output=True,text=True,check=True)
 d=json.loads(p.stdout); tracks={}
 for t in ("audio","video"):
  s=next((x for x in d["streams"] if x["codec_type"]==t),None)
  tracks[t]={"start":s.get("start_time"),"duration":s.get("duration"),
   "codec":s.get("codec_name")} if s else None
 return {"duration":d["format"].get("duration"),"tracks":tracks}
data={"originals":[probe(f"/qc/turn-{i}.mp4") for i in range(1,7)],
"normalized":[probe(f"/qc/normalized-{i}.mp4") for i in range(1,7)],
"offline_candidate":probe("/qc/candidate-stitch.mp4"),
"qc_decision":"REVIEW_REQUIRED","audio_content_integrity_verified":False,
"lip_sync_certified":False}
print(json.dumps(data,indent=2))
' > "$RUN/timeline.json"
chmod 600 "$RUN"/*
echo "OFFLINE_CANDIDATE_CREATED=YES"
echo "NO_PRODUCTION_UPLOAD_OR_DEPLOYMENT=YES"
echo "EVIDENCE_DIR=$RUN"
echo "NEXT=COMPARE_DECODED_AUDIO_AND_REVIEW_PLAYBACK"
