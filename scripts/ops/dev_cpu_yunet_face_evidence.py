#!/usr/bin/env python3
"""CPU-only DEV face tracking evidence using a user-supplied pinned YuNet ONNX model.

Does not download assets, change containers, determine speaker identity, or
claim audio-to-mouth synchronization. Output is REVIEW_REQUIRED by design.
Run through docker exec -i on an existing DEV extension container using
read-only mounts / paths, not by installing packages into it.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def analyze(video, model, expected_sha, every=5, limit=600):
    import cv2
    if digest(model) != expected_sha:
        raise ValueError("MODEL_SHA256_MISMATCH")
    if not hasattr(cv2, "FaceDetectorYN"):
        raise RuntimeError("OPENCV_FACEDETECTORYN_UNAVAILABLE")
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise ValueError("VIDEO_UNREADABLE")
    detector = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.7, 0.3, 5000)
    rows, frames, sampled = [], 0, 0
    try:
        while sampled < limit:
            ok, frame = cap.read()
            if not ok:
                break
            frames += 1
            if (frames - 1) % every:
                continue
            sampled += 1
            height, width = frame.shape[:2]
            detector.setInputSize((width, height))
            _, faces = detector.detect(frame)
            found = []
            if faces is not None:
                for f in faces:
                    # YuNet output: bounding box, 5 landmarks, confidence.
                    x,y,w,h = [float(v) for v in f[:4]]
                    if w <= 0 or h <= 0:
                        continue
                    mouth_l = (float(f[10]), float(f[11]))
                    mouth_r = (float(f[12]), float(f[13]))
                    # Mouth corner distance is NOT mouth aperture or viseme.
                    corner_distance = math.dist(mouth_l, mouth_r)
                    found.append({
                        "bbox_normalized": [round(x/width,4),round(y/height,4),
                                           round(w/width,4),round(h/height,4)],
                        "mouth_corner_distance_over_face_width": round(corner_distance/w,4),
                        "detection_score": round(float(f[14]),4),
                    })
            rows.append({"frame": frames-1,
                         "time_s": round(float(cap.get(cv2.CAP_PROP_POS_MSEC))/1000,3),
                         "detected_faces": found})
    finally:
        cap.release()
    return {"sampled_frames": sampled,"frames_read":frames,"frame_stride":every,
            "samples": rows, "status":"FACE_DETECTION_EVIDENCE_ONLY"}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--video",required=True)
    p.add_argument("--model",required=True)
    p.add_argument("--model-sha256",required=True)
    p.add_argument("--output",required=True)
    p.add_argument("--stride",type=int,default=5)
    p.add_argument("--max-samples",type=int,default=600)
    args=p.parse_args()
    import os
    if os.uname().nodename.split(".")[0] != "desifaces-dev":
        raise RuntimeError("DEV_HOST_REQUIRED")
    if args.stride < 1 or not 1 <= args.max_samples <= 1000:
        raise ValueError("BOUNDED_EVALUATION_REQUIRED")
    if len(args.model_sha256)!=64 or any(c not in "0123456789abcdef" for c in args.model_sha256):
        raise ValueError("VALID_SHA256_REQUIRED")
    out=Path(args.output)
    if out.exists():
        raise RuntimeError("EVIDENCE_OVERWRITE_REFUSED")
    result=analyze(Path(args.video),Path(args.model),args.model_sha256,
                   args.stride,args.max_samples)
    result.update({"decision":"REVIEW_REQUIRED","model_sha256":args.model_sha256,
                   "video_basename":Path(args.video).name,
                   "limitations":["YuNet mouth corner distance is not mouth opening.",
                                  "No speaker identity established.",
                                  "No audiovisual offset or viseme alignment measured."]})
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps({"decision":result["decision"],"sampled_frames":result["sampled_frames"],
                      "output":str(out)}))
    return 1

if __name__=="__main__":
    sys.exit(main())
