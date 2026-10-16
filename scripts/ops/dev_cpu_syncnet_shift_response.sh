#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(hostname -s)" == "desifaces-dev" ]] || exit 2
BASE="$HOME/desifaces-qc-evidence/syncnet-reference-demo"
IMAGE=desifaces-syncnet-cpu-smoke:dev-20261016
docker image inspect "$IMAGE" >/dev/null
SRC=""
for D in "$BASE"/isolated.*/src; do
  if [[ -f "$D/data/syncnet_v2.model" && -f "$D/data/example.avi" ]]; then SRC="$D"; fi
done
[[ -n "$SRC" ]] || { echo "REFERENCE_FILES_MISSING"; exit 2; }
echo "961e8696f888fce4f3f3a6c3d5b3267cf5b343100b238e79b2659bff2c605442  $SRC/data/syncnet_v2.model" | sha256sum -c -
echo "1724121b3a50141cfc9e77c034f7de914024d015b92762ae12d3e1a7151b8621  $SRC/data/example.avi" | sha256sum -c -
RUN="$BASE/calibration-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 "$RUN"
echo "CALIBRATION_DIR=$RUN"
for MS in -240 -120 0 120 240; do
  if (( MS < 0 )); then LABEL="m$((-MS))"; else LABEL="p$MS"; fi
  if (( MS == 0 )); then cp "$SRC/data/example.avi" "$RUN/$LABEL.avi"; else
    OFFSET="$(python3 -c "print($MS/1000)")"
    docker run --rm --network none --cpus 1 --memory 512m \
      --user "$(id -u):$(id -g)" \
      --mount "type=bind,src=$SRC/data,dst=/source,readonly" \
      --mount "type=bind,src=$RUN,dst=/output" \
      --entrypoint ffmpeg "$IMAGE" -hide_banner -loglevel error -y \
      -i /source/example.avi -itsoffset "$OFFSET" -i /source/example.avi \
      -map 0:v:0 -map 1:a:0 -c:v mpeg4 -q:v 4 -c:a pcm_s16le \
      "/output/$LABEL.avi"
  fi
  mkdir -m 700 "$RUN/work-$LABEL"
  docker run --rm --network none --cpus 2 --memory 3g --pids-limit 160 \
    --security-opt no-new-privileges --cap-drop ALL \
    --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$SRC,dst=/eval,readonly" \
    --mount "type=bind,src=$RUN,dst=/evidence" \
    --entrypoint python "$IMAGE" /eval/demo_syncnet.py \
    --initial_model /eval/data/syncnet_v2.model \
    --videofile "/evidence/$LABEL.avi" \
    --tmp_dir "/evidence/work-$LABEL" --reference demo \
    --batch_size 4 --vshift 15 >"$RUN/$LABEL.stdout" 2>"$RUN/$LABEL.stderr"
  echo "INJECTED_SHIFT_MS=$MS"
  grep -E "AV offset:|Confidence:" "$RUN/$LABEL.stderr" | tail -n 2
done
python3 - "$RUN" <<'PY'
import json,pathlib,re,sys
root=pathlib.Path(sys.argv[1]); rows=[]
for ms in (-240,-120,0,120,240):
    label=("m"+str(-ms)) if ms<0 else ("p"+str(ms))
    log=(root/(label+".stderr")).read_text()
    o=re.findall(r"AV offset:\s*(-?\d+)",log)
    c=re.findall(r"Confidence:\s*(-?[\d.]+)",log)
    if not o or not c: raise ValueError("MISSING_SYNCNET_OUTPUT_"+label)
    rows.append({"requested_audio_shift_ms":ms,"offset_frames":int(o[-1]),"confidence_raw":float(c[-1])})
base=next(r["offset_frames"] for r in rows if r["requested_audio_shift_ms"]==0)
for r in rows: r["delta_from_baseline_ms"]=40*(r["offset_frames"]-base)
report={"status":"REVIEW_REQUIRED","reference_baseline_frames":base,
        "shift_response":rows,"independent_mux_shift_audit":False,
        "signed_offset_calibrated":False,"desifaces_lipsync_certified":False}
(root/"shift-response.json").write_text(json.dumps(report,indent=2)+"\n")
print(json.dumps(report,indent=2))
PY
echo "SHIFT_RESPONSE_EXECUTED=YES"
echo "CALIBRATION_CERTIFIED=NO"
echo "REPORT=$RUN/shift-response.json"
