#!/usr/bin/env bash
# DEV-only waveform-verified calibration: fixed-length PCM audio shift, video unchanged.
set -Eeuo pipefail
[[ "$(hostname -s)" == "desifaces-dev" ]] || exit 2
BASE="$HOME/desifaces-qc-evidence/syncnet-reference-demo"
IMAGE=desifaces-syncnet-cpu-smoke:dev-20261016
SRC=""
for d in "$BASE"/isolated.*/src; do
  if [[ -f "$d/data/syncnet_v2.model" && -f "$d/data/example.avi" ]]; then SRC="$d"; fi
done
[[ -n "$SRC" ]] || { echo "REFERENCE_MISSING"; exit 2; }
docker image inspect "$IMAGE" >/dev/null
RUN="$BASE/waveform-calibration-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 "$RUN"
echo "OUTPUT_DIR=$RUN"
# Shared WAV reference; all variants use same video frames and sample count.
docker run --rm --network none --cpus 1 --memory 512m \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$SRC/data,dst=/src,readonly" \
  --mount "type=bind,src=$RUN,dst=/out" --entrypoint ffmpeg "$IMAGE" \
  -hide_banner -loglevel error -y -i /src/example.avi -vn \
  -ac 1 -ar 16000 -c:a pcm_s16le /out/original.wav
for ms in -240 -120 0 120 240; do
  if (( ms < 0 )); then label="m$((-ms))"; else label="p$ms"; fi
  if (( ms > 0 )); then
    # adelay samples uses S suffix (samples), so 16 samples per ms at 16 kHz.
    SAMPLES="$((ms*16))"
    FILTER="adelay=${SAMPLES}S,apad"
  elif (( ms < 0 )); then
    TRIM_SEC="$(python3 -c "print($((-ms))/1000)")"
    FILTER="atrim=start=${TRIM_SEC},asetpts=PTS-STARTPTS,apad"
  else FILTER="anull"; fi
  docker run --rm --network none --cpus 1 --memory 512m \
    --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$RUN,dst=/out" --entrypoint ffmpeg "$IMAGE" \
    -hide_banner -loglevel error -y -i /out/original.wav \
    -af "$FILTER" -t 30 -ac 1 -ar 16000 -c:a pcm_s16le "/out/$label.wav"
done
# Independently verify actual decoded waveform shifts, using correlation.
docker run --rm --network none --cpus 1 --memory 512m \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$RUN,dst=/out" --entrypoint python "$IMAGE" -c '
import wave,numpy as np,json
def samples(name):
    with wave.open("/out/"+name+".wav","rb") as w:
        assert w.getframerate()==16000 and w.getnchannels()==1
        return np.frombuffer(w.readframes(w.getnframes()),dtype="<i2").astype(np.float32)
source=samples("p0")
results={}
for ms in (-240,-120,0,120,240):
    name=("m"+str(-ms)) if ms<0 else ("p"+str(ms))
    y=samples(name)
    expected=ms*16
    # Compare a speech-rich window, shifted by the known number of samples.
    start=16000*2;length=16000*2
    x=source[start:start+length]
    idx=start+expected
    z=y[idx:idx+length]
    if len(z)!=len(x):raise RuntimeError("SHORT_AUDIO")
    corr=float(np.corrcoef(x,z)[0,1])
    if not np.isfinite(corr) or corr<0.995:
        raise RuntimeError("WAVEFORM_SHIFT_NOT_VERIFIED_"+name+"_CORR_"+str(corr))
    results[name]={"requested_shift_ms":ms,"waveform_correlation_at_expected_shift":round(corr,6)}
with open("/out/waveform-check.json","w") as f:json.dump(results,f,indent=2)
print("WAVEFORM_SHIFTS_VERIFIED=YES",json.dumps(results))
'
# Use unchanged original video stream, explicit WAV audio, no timestamp-offset tricks.
for ms in -240 -120 0 120 240; do
  if (( ms < 0 )); then label="m$((-ms))"; else label="p$ms"; fi
  docker run --rm --network none --cpus 1 --memory 512m \
    --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$SRC/data,dst=/src,readonly" \
    --mount "type=bind,src=$RUN,dst=/out" --entrypoint ffmpeg "$IMAGE" \
    -hide_banner -loglevel error -y -i /src/example.avi -i "/out/$label.wav" \
    -map 0:v:0 -map 1:a:0 -c:v mpeg4 -q:v 4 -c:a pcm_s16le \
    "/out/$label.avi"
  mkdir -m 700 "$RUN/work-$label"
  docker run --rm --network none --cpus 2 --memory 3g --pids-limit 160 \
    --security-opt no-new-privileges --cap-drop ALL \
    --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$SRC,dst=/eval,readonly" \
    --mount "type=bind,src=$RUN,dst=/out" \
    --entrypoint python "$IMAGE" /eval/demo_syncnet.py \
    --initial_model /eval/data/syncnet_v2.model \
    --videofile "/out/$label.avi" --tmp_dir "/out/work-$label" \
    --reference demo --batch_size 4 --vshift 15 \
    >"$RUN/$label.stdout" 2>"$RUN/$label.stderr"
  echo "WAVEFORM_SHIFT_MS=$ms"
  grep -E "AV offset:|Confidence:" "$RUN/$label.stderr" | tail -n 2
done
python3 - "$RUN" <<'PY'
import pathlib,json,re,sys
root=pathlib.Path(sys.argv[1]); rows=[]
for ms in (-240,-120,0,120,240):
    name=("m"+str(-ms)) if ms<0 else ("p"+str(ms))
    log=(root/(name+".stderr")).read_text()
    offset=re.findall(r"AV offset:\s*(-?\d+)",log)
    conf=re.findall(r"Confidence:\s*([\d.]+)",log)
    if not offset or not conf:raise RuntimeError("MISSING_RESULT_"+name)
    rows.append({"injected_ms":ms,"offset_frames":int(offset[-1]),"confidence":float(conf[-1])})
baseline=next(r["offset_frames"] for r in rows if r["injected_ms"]==0)
for row in rows:row["delta_from_baseline_ms"]=(row["offset_frames"]-baseline)*40
print(json.dumps({"waveforms_verified":True,"results":rows,"decision":"REVIEW_REQUIRED"},indent=2))
(root/"summary.json").write_text(json.dumps({"waveforms_verified":True,"results":rows,"decision":"REVIEW_REQUIRED"},indent=2))
PY
echo "CALIBRATION_EXPERIMENT_COMPLETE=YES"
echo "REPORT_DIR=$RUN"
