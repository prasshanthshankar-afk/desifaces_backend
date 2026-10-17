#!/usr/bin/env python3
"""DEV read-only AV timeline audit for six child MP4s and final stitch.
Runs inside df-svc-fusion-extension with Azure Blob credentials already present.
No uploads, DB writes, generation, repair, or URLs in output.
"""
import asyncio, json, os, subprocess, tempfile
from pathlib import Path
from urllib.parse import urlparse, unquote
import asyncpg
from azure.storage.blob import BlobServiceClient

ATTEMPT="992b2904-8ddf-4788-8e1e-1aa0d076fb9a"
LIMIT=100*1024*1024
def num(s):
    try: return float(s)
    except (TypeError,ValueError): return None

def probe(path):
    p=subprocess.run(["ffprobe","-v","error","-show_format","-show_streams",
                      "-of","json",str(path)],capture_output=True,text=True,timeout=60,check=True)
    d=json.loads(p.stdout)
    streams={}
    for typ in ("audio","video"):
        selected=next((v for v in d["streams"] if v["codec_type"]==typ),None)
        if not selected:
            streams[typ]=None
        else:
            streams[typ]={"start_ms":num(selected.get("start_time")),
                          "duration_ms":num(selected.get("duration")),
                          "codec":selected.get("codec_name"),
                          "sample_rate":selected.get("sample_rate"),
                          "frame_rate":selected.get("avg_frame_rate")}
            for key in ("start_ms","duration_ms"):
                if streams[typ][key] is not None:streams[typ][key]=round(1000*streams[typ][key],2)
    return {"container_duration_ms":round(1000*float(d["format"]["duration"]),2),
            "streams":streams}

async def refs():
    conn=await asyncpg.connect(os.environ["DATABASE_URL"],server_settings={"default_transaction_read_only":"on"},timeout=10)
    try:
        raw=await conn.fetchval("SELECT metadata_json FROM public.v3_studio_stage_attempts WHERE attempt_id=$1::uuid",ATTEMPT)
        meta=json.loads(raw) if isinstance(raw,str) else raw
        if not isinstance(meta,dict) or len(meta.get("children",[]))!=6:raise RuntimeError("EXPECTED_SIX_CHILDREN")
        return [x.get("video_url") for x in meta["children"]],meta.get("stitched_video_url")
    finally:await conn.close()

def download(service,url,path):
    if not isinstance(url,str):raise RuntimeError("URL_MISSING")
    parsed=urlparse(url)
    if parsed.scheme!="https" or parsed.hostname!=urlparse(service.url).hostname:
        raise RuntimeError("STORAGE_ACCOUNT_MISMATCH")
    parts=parsed.path.lstrip("/").split("/",1)
    if len(parts)!=2:raise RuntimeError("INVALID_BLOB_PATH")
    b=service.get_blob_client(unquote(parts[0]),unquote(parts[1]))
    props=b.get_blob_properties()
    if not 0<props.size<=LIMIT:raise RuntimeError("BLOB_SIZE_OUT_OF_BOUNDS")
    total=0
    with open(path,"wb") as out:
        for chunk in b.download_blob(max_concurrency=1).chunks():
            total+=len(chunk)
            if total>LIMIT:raise RuntimeError("BLOB_SIZE_LIMIT")
            out.write(chunk)
    if total!=props.size:raise RuntimeError("INCOMPLETE_DOWNLOAD")
    return {"bytes":total,"media":probe(path)}

def main():
    if os.environ.get("QC_DEV_HOST_VERIFIED")!="1":raise RuntimeError("DEV_HOST_VERIFICATION_REQUIRED")
    service=BlobServiceClient.from_connection_string(os.environ["AZURE_STORAGE_CONNECTION_STRING"])
    children,final=asyncio.run(refs())
    report={"attempt_id":ATTEMPT,"result":"REVIEW_REQUIRED","source":"original child blobs",
            "normalization_and_post_stitch_not_verified":True,"segments":[]}
    cumulative=0.0
    with tempfile.TemporaryDirectory(prefix="df_dev_timeline_") as td:
        for i,url in enumerate(children,1):
            p=Path(td)/"child.mp4"
            entry=download(service,url,p)
            entry["turn"]=i
            entry["expected_stitch_start_ms"]=round(cumulative,2)
            cumulative+=entry["media"]["container_duration_ms"]
            report["segments"].append(entry)
            p.unlink()
        report["sum_child_container_durations_ms"]=round(cumulative,2)
        if final:
            try:
                entry=download(service,final,Path(td)/"stitched.mp4")
                report["stitched"]=entry
                report["final_minus_sum_source_ms"]=round(entry["media"]["container_duration_ms"]-cumulative,2)
            except Exception as e:
                report["final_read_status"]=type(e).__name__
        else:report["final_read_status"]="FINAL_URL_MISSING"
    report["interpretation"]="Container timing only. Source-to-normalized intermediate packets, approved audio, and visual phoneme alignment still require separate tests."
    print(json.dumps(report,indent=2))
if __name__=="__main__":main()
