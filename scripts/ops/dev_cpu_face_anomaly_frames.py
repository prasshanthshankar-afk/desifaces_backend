#!/usr/bin/env python3
"""Pinpoint anomalous YuNet frames in retained DEV evidence, no media or secrets.

Spatial heuristics are diagnostic only; no persistent identity or lip sync PASS.
"""
import argparse
import json
import math
from pathlib import Path

def inspect(samples):
    previous = None
    findings = []
    for row in samples:
        faces = sorted(row.get("detected_faces", []),
            key=lambda f: float(f["bbox_normalized"][0])+
            float(f["bbox_normalized"][2])/2)
        if len(faces)<2:
            findings.append({"frame":row.get("frame"),"time_s":row.get("time_s"),"reason":"FEWER_THAN_TWO_FACES"})
            previous=None
            continue
        pair=[faces[0], faces[-1]]
        centers=[]
        for f in pair:
            x,y,w,h=map(float,f["bbox_normalized"])
            centers.append((x+w/2,y+h/2))
        reasons=[]
        if min(float(f["detection_score"]) for f in pair)<0.75:
            reasons.append("LOW_CONFIDENCE")
        if previous is not None and max(math.dist(centers[0],previous[0]),
                                        math.dist(centers[1],previous[1]))>0.20:
            reasons.append("LARGE_POSITION_CHANGE")
        if reasons:
            findings.append({"frame":row.get("frame"),"time_s":row.get("time_s"),
                             "reason":reasons})
        previous=centers
    return findings

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("dir")
    parser.add_argument("--output",default="tracking-anomaly-frames.json")
    args=parser.parse_args()
    root=Path(args.dir)
    results={}
    for turn in range(1,7):
        data=json.loads((root/f"turn-{turn}.json").read_text())
        results[str(turn)]=inspect(data["samples"])
    output={"status":"REVIEW_REQUIRED","method":"YuNet frame-level spatial anomaly localization",
            "lip_sync_measured":False,"turns":results}
    dest=root/args.output
    if dest.exists(): raise FileExistsError("EVIDENCE_OVERWRITE_REFUSED")
    dest.write_text(json.dumps(output,indent=2)+"\n")
    for turn,rows in results.items():
        print(f"TURN={turn} ANOMALIES={len(rows)}")
        for row in rows:
            print(f"  FRAME={row['frame']} TIME_S={row['time_s']} REASONS={row['reason']}")
    print("AV_OFFSET_MEASURED=NO")
if __name__=="__main__":main()
