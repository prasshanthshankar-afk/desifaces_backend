#!/usr/bin/env python3
"""CPU SyncNet AV-offset adapter for an already cropped single-speaker MP4.

Input MUST be a correctly tracked 25 FPS, 224x224 face-crop video with embedded
16 kHz mono speech audio. Does NOT handle face selection, model download or
commercial rights. Run only in an isolated DEV CPU runtime. Prints JSON to stdout.
"""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import hashlib

def checksum(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for b in iter(lambda:f.read(1<<20),b""):h.update(b)
    return h.hexdigest()

def check_input(path):
    p=subprocess.run(["ffprobe","-v","error","-show_streams",
        "-of","json",str(path)],text=True,capture_output=True,timeout=30,check=True)
    streams=json.loads(p.stdout)["streams"]
    video=next((x for x in streams if x["codec_type"]=="video"),None)
    audio=next((x for x in streams if x["codec_type"]=="audio"),None)
    if not video or not audio:raise ValueError("AUDIO_AND_VIDEO_REQUIRED")
    rate=video.get("avg_frame_rate","0/1")
    num,den=map(int,rate.split("/"))
    if den==0 or abs(num/den-25)>0.01:raise ValueError("CROPPED_VIDEO_MUST_BE_25FPS")
    if (video.get("width"),video.get("height")) != (224,224):
        raise ValueError("CROPPED_VIDEO_MUST_BE_224x224")
    if int(audio.get("sample_rate",0))!=16000 or int(audio.get("channels",0))!=1:
        raise ValueError("AUDIO_MUST_BE_16KHZ_MONO")
    return True

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--video",required=True)
    p.add_argument("--model",required=True)
    p.add_argument("--sha256",required=True)
    p.add_argument("--syncnet-source",required=True)
    p.add_argument("--tmp-dir",required=True)
    p.add_argument("--vshift",type=int,default=15)
    p.add_argument("--batch-size",type=int,default=4)
    a=p.parse_args()
    if os.environ.get("DESIFACES_DEV_VERIFIED")!="1":
        raise RuntimeError("DEV_ATTESTATION_REQUIRED")
    if not 1<=a.vshift<=20 or not 1<=a.batch_size<=8:
        raise ValueError("BOUNDED_MODEL_ARGS_REQUIRED")
    if len(a.sha256)!=64 or checksum(a.model)!=a.sha256:
        raise ValueError("PINNED_MODEL_HASH_MISMATCH")
    check_input(Path(a.video))
    sys.path.insert(0,a.syncnet_source)
    from SyncNetInstance import SyncNetInstance
    class Args:
        tmp_dir=a.tmp_dir
        reference="single_speaker_eval"
        batch_size=a.batch_size
        vshift=a.vshift
    model=SyncNetInstance(device="cpu")
    model.loadParameters(a.model)
    offsets,conf,_=model.evaluate(Args(),videofile=a.video)
    frames=int(offsets.item())
    confidence=float(conf.item())
    if not math.isfinite(confidence):
        raise ValueError("INVALID_MODEL_CONFIDENCE")
    print(json.dumps({
        "offset_ms":frames*40,
        "confidence":confidence,
        "valid_tracking":False,
        "offset_frames":frames,
        "offset_sign":"MODEL_CONVENTION_UNCALIBRATED",
        "status":"UNVALIDATED_AV_ESTIMATE",
        "decision":"REVIEW_REQUIRED",
    }))
    return 1

if __name__=="__main__":sys.exit(main())
