#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo DEV_ONLY; exit 2; }
APP_IMAGE="$(docker inspect -f '{{.Image}}' df-svc-fusion-extension)"
MODEL_IMAGE="desifaces-syncnet-cpu-smoke:dev-20261016"
docker image inspect "$MODEL_IMAGE" >/dev/null
REF_DIR="$HOME/desifaces-qc-evidence/syncnet-reference-demo/isolated.N31CECuI/src"
test -f "$REF_DIR/data/syncnet_v2.model"
echo "961e8696f888fce4f3f3a6c3d5b3267cf5b343100b238e79b2659bff2c605442  $REF_DIR/data/syncnet_v2.model" | sha256sum -c -
RUN="$HOME/desifaces-qc-evidence/real-av-sixturn-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 "$RUN"
echo "EVIDENCE_DIR=$RUN"
gh api "repos/prasshanthshankar-afk/desifaces_backend/contents/scripts/ops/dev_cpu_syncnet_face_crop.py?ref=f0f1d7ccc7c4fd7068232a5472e6790c4050991d" --jq .content | base64 -d > "$RUN/crop.py"
curl -fLsS --max-time 60 "https://huggingface.co/opencv/opencv_zoo/resolve/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx" -o "$RUN/yunet.onnx"
echo "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4  $RUN/yunet.onnx" | sha256sum -c -
for TURN in 1 2 3 4 5 6; do
 echo "===== TURN $TURN ====="
 docker exec -i -e QC_TURN="$TURN" -e QC_ATTEMPT=992b2904-8ddf-4788-8e1e-1aa0d076fb9a df-svc-fusion-extension python - <<'PY' > "$RUN/original.mp4"
import asyncio,json,os,sys
from urllib.parse import urlparse,unquote
import asyncpg
from azure.storage.blob import BlobServiceClient
async def resolve():
 c=await asyncpg.connect(os.environ["DATABASE_URL"],server_settings={"default_transaction_read_only":"on"})
 try:
  r=await c.fetchval("select metadata_json from public.v3_studio_stage_attempts where attempt_id=$1::uuid",os.environ["QC_ATTEMPT"])
  obj=json.loads(r) if isinstance(r,str) else r
  if len(obj["children"])!=6:raise RuntimeError("CHILDREN_NOT_SIX")
  return obj["children"][int(os.environ["QC_TURN"])-1]["video_url"]
 finally:await c.close()
p=urlparse(asyncio.run(resolve()))
s=BlobServiceClient.from_connection_string(os.environ["AZURE_STORAGE_CONNECTION_STRING"])
if p.scheme!="https" or p.hostname!=urlparse(s.url).hostname:raise RuntimeError("BLOB_ACCOUNT_MISMATCH")
parts=p.path.lstrip("/").split("/",1)
if len(parts)!=2:raise RuntimeError("INVALID_BLOB_PATH")
b=s.get_blob_client(unquote(parts[0]),unquote(parts[1]))
size=b.get_blob_properties().size
if not 0<size<=20*1024*1024:raise RuntimeError("SIZE_LIMIT")
n=0
for chunk in b.download_blob(max_concurrency=1).chunks():
 n+=len(chunk)
 if n>20*1024*1024:raise RuntimeError("SIZE_LIMIT")
 sys.stdout.buffer.write(chunk)
if n!=size:raise RuntimeError("TRUNCATION")
PY
test -s "$RUN/original.mp4"
for SIDE in left right; do
 echo "==== FACE CROP $SIDE ===="
 set +e
 docker run --rm --network none --cpus 1 --memory 1g --pids-limit 128 --user "$(id -u):$(id -g)" --security-opt no-new-privileges --cap-drop ALL -e DESIFACES_DEV_VERIFIED=1 --mount "type=bind,src=$RUN,dst=/qc" --entrypoint python "$APP_IMAGE" /qc/crop.py --video /qc/original.mp4 --model /qc/yunet.onnx --model-sha256 8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4 --speaker-side "$SIDE" --output "/qc/$SIDE.mp4" --max-seconds 20 >"$RUN/$SIDE.crop.stdout" 2>"$RUN/$SIDE.crop.stderr"
 RC=$?
 set -e
 if [[ "$RC" -ne 0 ]]; then echo "CROP_$SIDE=REVIEW_REQUIRED"; tail -n 3 "$RUN/$TURN-$SIDE.crop.stderr"; continue; fi
 mkdir -m 700 "$RUN/work-$TURN-$SIDE"
 echo "==== SYNCNET $SIDE ===="
 set +e
 docker run --rm --network none --cpus 2 --memory 3g --pids-limit 160 --user "$(id -u):$(id -g)" --security-opt no-new-privileges --cap-drop ALL --mount "type=bind,src=$REF_DIR,dst=/eval,readonly" --mount "type=bind,src=$RUN,dst=/qc" --entrypoint python "$MODEL_IMAGE" /eval/demo_syncnet.py --initial_model /eval/data/syncnet_v2.model --videofile "/qc/$SIDE.mp4" --tmp_dir "/qc/work-$TURN-$SIDE" --reference demo --batch_size 4 --vshift 15 >"$RUN/$TURN-$SIDE.sync.stdout" 2>"$RUN/$TURN-$SIDE.sync.stderr"
 RC=$?
 set -e
 if [[ "$RC" -ne 0 ]]; then echo "SYNCNET_$SIDE=REVIEW_REQUIRED"; tail -n 5 "$RUN/$TURN-$SIDE.sync.stderr"; continue; fi
 grep -E "AV offset:|Confidence:" "$RUN/$TURN-$SIDE.sync.stderr" | tail -n 2
done
python3 - "$RUN" "$TURN" <<'PY'
import pathlib,json,re,sys
root=pathlib.Path(sys.argv[1]); turn=int(sys.argv[2]); out={}
for side in ("left","right"):
 p=root/(str(turn)+"-"+side+".sync.stderr")
 if not p.exists():out[side]={"status":"NOT_MEASURED"};continue
 t=p.read_text();o=re.findall(r"AV offset:\s*(-?\d+)",t);c=re.findall(r"Confidence:\s*([\d.]+)",t)
 out[side]={"status":"UNVALIDATED_ESTIMATE","raw_offset_frames":int(o[-1]),"raw_offset_ms":40*int(o[-1]),"raw_confidence":float(c[-1])} if o and c else {"status":"NOT_MEASURED"}
report={"turn":turn,"candidate_faces":out,"speaker_identity_verified":False,"av_model_validated_on_desifaces":False,"decision":"REVIEW_REQUIRED"}
(root/f"turn-{turn}-measurements.json").write_text(json.dumps(report,indent=2)+"\n")
print(json.dumps(report,indent=2))
PY
rm -f "$RUN/original.mp4" "$RUN/left.mp4" "$RUN/right.mp4"
done
echo "SIX_TURN_CPU_SYNCNET_EXPERIMENT_COMPLETE=YES"
python3 - "$RUN" <<'PY'
import pathlib,json,sys
root=pathlib.Path(sys.argv[1]); turns=[]
for i in range(1,7): turns.append(json.loads((root/f"turn-{i}-measurements.json").read_text()))
report={"attempt_id":"992b2904-8ddf-4788-8e1e-1aa0d076fb9a","decision":"REVIEW_REQUIRED","speaker_identity_verified":False,"syncnet_reference_calibration_only":True,"turns":turns}
(root/"summary.json").write_text(json.dumps(report,indent=2)+"\n")
for t in turns:
 print("TURN",t["turn"],"LEFT",t["candidate_faces"]["left"],"RIGHT",t["candidate_faces"]["right"])
print("FINAL_DECISION=REVIEW_REQUIRED")
PY
echo "REPORT=$RUN/summary.json"
