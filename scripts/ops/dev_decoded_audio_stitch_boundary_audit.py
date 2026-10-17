#!/usr/bin/env python3
"""Read-only DEV comparison of decoded audio from six original clips and final stitch."""
import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse, unquote
import asyncpg
import numpy as np
from azure.storage.blob import BlobServiceClient

ATTEMPT = "992b2904-8ddf-4788-8e1e-1aa0d076fb9a"
RATE = 8000
MAX_BYTES = 100 * 1024 * 1024
VIDEO_MS = [10760,16040,9640,10920,9640,8200]

async def references():
    con = await asyncpg.connect(os.environ["DATABASE_URL"],
        server_settings={"default_transaction_read_only":"on"},timeout=10)
    try:
        raw = await con.fetchval(
            "SELECT metadata_json FROM public.v3_studio_stage_attempts WHERE attempt_id=$1::uuid", ATTEMPT)
        m = json.loads(raw) if isinstance(raw,str) else raw
        if not isinstance(m,dict) or len(m.get("children",[])) != 6:
            raise RuntimeError("INVALID_SIX_TURN_LINEAGE")
        return [c["video_url"] for c in m["children"]], m["stitched_video_url"]
    finally:
        await con.close()

def download(service, url, path):
    p = urlparse(url)
    if p.scheme != "https" or p.hostname != urlparse(service.url).hostname:
        raise RuntimeError("STORAGE_ACCOUNT_MISMATCH")
    parts = p.path.lstrip("/").split("/",1)
    if len(parts) != 2: raise RuntimeError("BLOB_PATH_MISSING")
    blob = service.get_blob_client(unquote(parts[0]),unquote(parts[1]))
    size = blob.get_blob_properties().size
    if not 0 < size <= MAX_BYTES: raise RuntimeError("BLOB_SIZE_OUT_OF_RANGE")
    received=0
    with open(path,"wb") as f:
        for chunk in blob.download_blob(max_concurrency=1).chunks():
            received+=len(chunk)
            if received > MAX_BYTES: raise RuntimeError("BLOB_LIMIT")
            f.write(chunk)
    if received != size: raise RuntimeError("INCOMPLETE_DOWNLOAD")

def audio(path):
    result=subprocess.run(["ffmpeg","-nostdin","-v","error","-i",str(path),"-vn",
         "-ac","1","-ar",str(RATE),"-f","f32le","pipe:1"],
         stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120,check=True)
    return np.frombuffer(result.stdout,dtype="<f4").copy()

def align(source, output, start_seconds):
    # Search start-offset around video timeline, not summed source audio durations.
    # 80-ms hop / energy envelope; 5-ms shift grid via sample time.
    hop=80
    def rms(x):
        x=x[:len(x)//hop*hop].reshape(-1,hop)
        return np.sqrt(np.mean(x*x,axis=1)+1e-12)
    a=rms(source)
    b=rms(output)
    window=a[15:min(len(a)-15,265)]
    if len(window)<80: return {"status":"SOURCE_TOO_SHORT"}
    window=window-window.mean()
    best=(-2.0,None)
    nominal=round(start_seconds*100)
    for shift in range(-50,51):
        index=nominal+shift+15
        if index<0 or index+len(window)>len(b):continue
        v=b[index:index+len(window)]
        v=v-v.mean()
        denom=float(np.linalg.norm(v)*np.linalg.norm(window))
        corr=float(np.dot(v,window)/denom) if denom>1e-10 else 0
        if corr>best[0]:best=(corr,shift)
    if best[1] is None:return {"status":"UNMEASURABLE"}
    return {"status":"MEASURED_AUDIO_ENVELOPE",
            "peak_correlation":round(best[0],4),
            "audio_shift_vs_video_boundary_ms":best[1]*10,
            "boundary_of_search":abs(best[1])==50}

def main():
    if os.environ.get("QC_DEV_HOST_VERIFIED")!="1":raise RuntimeError("DEV_ONLY")
    svc=BlobServiceClient.from_connection_string(os.environ["AZURE_STORAGE_CONNECTION_STRING"])
    children, stitched=asyncio.run(references())
    with tempfile.TemporaryDirectory(prefix="df_qc_boundaries_") as td:
        path=Path(td)/"clip.mp4"
        sources=[]
        for url in children:
            download(svc,url,path)
            sources.append(audio(path))
            path.unlink()
        download(svc,stitched,path)
        final=audio(path)
    starts=np.cumsum([0]+VIDEO_MS[:-1])/1000
    turns=[]
    for i,source in enumerate(sources):
        turns.append({"turn":i+1,"video_nominal_start_ms":round(float(starts[i]*1000),2),
                      "source_decoded_audio_ms":round(len(source)/RATE*1000,2),
                      **align(source,final,float(starts[i]))})
    joins=[]
    for i in range(5):
        boundary_ms=int((starts[i+1])*1000)
        # Five seam windows: downstream audio correlation is the primary diagnostic.
        joins.append({"between_turns":[i+1,i+2],
                      "nominal_video_boundary_ms":boundary_ms,
                      "following_audio_shift_ms":turns[i+1].get("audio_shift_vs_video_boundary_ms"),
                      "preceding_audio_shift_ms":turns[i].get("audio_shift_vs_video_boundary_ms"),
                      "following_match_correlation":turns[i+1].get("peak_correlation")})
    report={"attempt_id":ATTEMPT,"status":"REVIEW_REQUIRED","method":"10ms decoded RMS envelope ±500ms around original video boundaries",
         "final_decoded_audio_ms":round(len(final)/RATE*1000,2),
         "turns":turns,"joins":joins,
         "limitations":["Metadata-derived video boundaries are approximate after 30fps normalization.",
         "A matched waveform envelope is not a phoneme-level or visual lip-sync certification.",
         "Clipping and word completeness need listening/phoneme review."]}
    print(json.dumps(report,indent=2))

if __name__=="__main__":main()
