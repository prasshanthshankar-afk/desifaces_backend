#!/usr/bin/env python3
"""Make a tracked single-face SyncNet input from a local two-person MP4.

CPU-only preprocessing. Caller MUST specify LEFT/RIGHT after human verification.
Uses YuNet every output frame, fails on uncertain frames rather than silently
switching speakers. The output is 224x224 at 25fps and audio 16k mono.
No lip-sync verdict. Never operate on original media in-place.
"""
from __future__ import annotations
import argparse, hashlib, os, subprocess, json
from pathlib import Path
import cv2

def sha(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for part in iter(lambda:f.read(1024*1024),b""):h.update(part)
    return h.hexdigest()

def main():
    a=argparse.ArgumentParser()
    a.add_argument("--video",required=True)
    a.add_argument("--model",required=True)
    a.add_argument("--model-sha256",required=True)
    a.add_argument("--speaker-side",choices=("left","right"),required=True)
    a.add_argument("--output",required=True)
    a.add_argument("--max-seconds",type=float,default=20)
    args=a.parse_args()
    if os.environ.get("DESIFACES_DEV_VERIFIED")!="1":raise RuntimeError("DEV_ATTESTATION_REQUIRED")
    if not 0<args.max_seconds<=30:raise ValueError("INVALID_MAX_DURATION")
    if sha(args.model)!=args.model_sha256:raise ValueError("MODEL_CHECKSUM_MISMATCH")
    if Path(args.output).exists():raise FileExistsError("OUTPUT_EXISTS")
    cap=cv2.VideoCapture(args.video)
    if not cap.isOpened():raise RuntimeError("UNREADABLE_VIDEO")
    fps=cap.get(cv2.CAP_PROP_FPS)
    if fps<=0:raise RuntimeError("INVALID_INPUT_FPS")
    duration=cap.get(cv2.CAP_PROP_FRAME_COUNT)/fps
    if duration>args.max_seconds:raise RuntimeError("VIDEO_TOO_LONG")
    detector=cv2.FaceDetectorYN.create(args.model,"",(320,320),0.75,0.3,5000)
    tmp=Path(args.output).with_suffix(".silent.avi")
    writer=cv2.VideoWriter(str(tmp),cv2.VideoWriter_fourcc(*"MJPG"),25,(224,224))
    if not writer.isOpened():raise RuntimeError("VIDEO_WRITER_FAILED")
    dropped=0; last=None; count=0
    try:
        out_count=max(0,int(duration*25))
        for n in range(out_count):
            cap.set(cv2.CAP_PROP_POS_MSEC,1000*n/25)
            ok,frame=cap.read()
            if not ok:raise RuntimeError("FRAME_READ_FAILED")
            h,w=frame.shape[:2]
            small=cv2.resize(frame,(320,320))
            _,faces=detector.detect(small)
            if faces is None or len(faces)<2:raise RuntimeError(f"FACE_TRACK_MISSING_FRAME_{n}")
            faces=sorted(faces,key=lambda z:float(z[0]+z[2]*0.5))
            f=faces[0] if args.speaker_side=="left" else faces[-1]
            if float(f[14])<0.75:raise RuntimeError(f"FACE_CONFIDENCE_LOW_FRAME_{n}")
            x,y,bw,bh=map(float,f[:4])
            cx=(x+bw/2)*w/320;cy=(y+bh/2)*h/320
            width=max(bw*w/320,bh*h/320)*1.55
            if last is not None and abs(cx-last)>w*0.20:
                raise RuntimeError(f"FACE_POSITION_JUMP_FRAME_{n}")
            last=cx
            size=int(width)
            if size<25:raise RuntimeError("FACE_TOO_SMALL")
            x0=max(0,int(cx-size/2));x1=min(w,int(cx+size/2))
            y0=max(0,int(cy-size/2));y1=min(h,int(cy+size/2))
            crop=frame[y0:y1,x0:x1]
            if crop.size==0:raise RuntimeError("EMPTY_FACE_CROP")
            writer.write(cv2.resize(crop,(224,224)))
            count+=1
    finally:
        cap.release();writer.release()
    try:
        subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y",
          "-i",str(tmp),"-i",args.video,"-map","0:v:0","-map","1:a:0",
          "-af","aresample=16000","-ac","1","-ar","16000",
          "-c:v","mpeg4","-q:v","4","-c:a","aac","-shortest",args.output],
          check=True,timeout=180)
    finally:
        tmp.unlink(missing_ok=True)
    print(json.dumps({"status":"CROPPED_AV_PREPARED","frames":count,
                      "speaker_side":args.speaker_side,
                      "speaker_identity_confirmed":False,
                      "lip_sync_verified":False,"output":args.output}))
if __name__=="__main__":main()
