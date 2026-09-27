#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from uuid import UUID

from azure.storage.blob import BlobServiceClient

from app.services.sas_service import AzureBlobService


WORKFLOW_ID = UUID("2ef2b35b-f515-47da-964a-9c35669863fd")
STAGE_RUN_ID = UUID("69b968f3-793e-433e-bdd9-03ec2afa43e8")


def clean(v) -> str:
    return str(v or "").strip()


def parse_rate(value: str) -> float:
    raw=clean(value)
    if not raw:
        return 0.0
    if "/" in raw:
        a,b=raw.split("/",1)
        try:
            d=float(b)
            return float(a)/d if d else 0.0
        except Exception:
            return 0.0
    try:
        return float(raw)
    except Exception:
        return 0.0


def probe(path: Path) -> dict:
    p=subprocess.run([
        "ffprobe","-v","error","-select_streams","v:0",
        "-show_entries","stream=width,height,avg_frame_rate,r_frame_rate,nb_frames:format=duration",
        "-of","json",str(path)
    ],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if p.returncode != 0:
        raise RuntimeError("FFPROBE_FAILED:"+(p.stderr or "")[-1200:])
    data=json.loads(p.stdout or "{}")
    streams=list(data.get("streams") or [])
    if not streams:
        raise RuntimeError("VIDEO_STREAM_MISSING")
    s=streams[0]
    fps=parse_rate(str(s.get("avg_frame_rate") or "")) or parse_rate(str(s.get("r_frame_rate") or ""))
    duration=float((data.get("format") or {}).get("duration") or 0)
    return {
        "width":int(s.get("width") or 0),
        "height":int(s.get("height") or 0),
        "fps":round(fps,6),
        "duration":round(duration,3),
        "nb_frames":s.get("nb_frames"),
    }


def main() -> None:
    conn=clean(os.getenv("AZURE_STORAGE_CONNECTION_STRING"))
    container=clean(os.getenv("AZURE_VIDEO_OUTPUT_CONTAINER") or "video-output")
    if not conn:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING_MISSING")

    prefix=f"v3/qa/shared-scene-performance/{WORKFLOW_ID}/{STAGE_RUN_ID}/"
    bsc=BlobServiceClient.from_connection_string(conn)
    cc=bsc.get_container_client(container)
    candidates=[
        b for b in cc.list_blobs(name_starts_with=prefix)
        if str(b.name).endswith("/performance-motion.mp4")
    ]
    if not candidates:
        raise RuntimeError("NO_PERFORMANCE_MOTION_BLOB_FOUND")
    candidates.sort(key=lambda b: b.last_modified, reverse=True)
    selected=candidates[0]

    signer=AzureBlobService(conn)
    review_url=signer.sign_read_url(container, selected.name, 15*24*3600)

    with tempfile.TemporaryDirectory(prefix="df_motion_recover_") as td:
        path=Path(td)/"performance-motion.mp4"
        path.write_bytes(cc.download_blob(selected.name).readall())
        info=probe(path)

    print("============================================================")
    print("NEXT3_PERFORMANCE_MOTION_RECOVERY=PASS")
    print(f"blob={selected.name}")
    print(f"last_modified={selected.last_modified.isoformat()}")
    print(f"video_info={json.dumps(info, separators=(',',':'))}")
    print(f"MOTION_BASE_URL={review_url}")
    print("provider_generation=NONE")
    print("database_write=NONE")
    print("production_touch=NONE")
    print("============================================================")


if __name__ == "__main__":
    main()
