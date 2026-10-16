#!/usr/bin/env python3
"""DEV-only six-turn spatial face-track QC over existing YuNet evidence.

No images, audio, inference, identity matching or production changes.
Left/right are per-frame spatial positions, NOT person identifiers.
This is not audiovisual lip-sync verification.
"""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path

def summarize(evidence: dict, turn: int) -> dict:
    samples = evidence.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError(f"TURN_{turn}_SAMPLES_MISSING")
    total, valid, low_conf, crossings, large_motion, collisions = len(samples), 0, 0, 0, 0, 0
    prev = None
    for row in samples:
        faces = row.get("detected_faces", [])
        if not isinstance(faces, list) or len(faces) < 2:
            prev = None
            continue
        ordered = sorted(faces, key=lambda face: float(face["bbox_normalized"][0]) +
                         float(face["bbox_normalized"][2])/2)
        left, right = ordered[0], ordered[-1]
        def center(face):
            x,y,w,h = map(float, face["bbox_normalized"])
            return (x+w/2, y+h/2)
        a,b = center(left),center(right)
        if b[0]-a[0] < 0.10:
            collisions += 1
        if min(float(left["detection_score"]),float(right["detection_score"])) < 0.75:
            low_conf += 1
        if prev is not None:
            jump = max(math.dist(a,prev[0]),math.dist(b,prev[1]))
            if jump > 0.20:
                large_motion += 1
            # If crossed matches are distinctly closer than same-side matches,
            # left/right spatial correspondence may switch between detections.
            normal=math.dist(a,prev[0])+math.dist(b,prev[1])
            swapped=math.dist(a,prev[1])+math.dist(b,prev[0])
            if swapped + 0.05 < normal:
                crossings += 1
        prev=(a,b)
        valid += 1
    reasons=[]
    if valid/total < 0.95: reasons.append("INSUFFICIENT_TWO_FACE_COVERAGE")
    if low_conf: reasons.append("LOW_CONFIDENCE_DETECTIONS")
    if collisions: reasons.append("FACES_SPATIALLY_AMBIGUOUS")
    if crossings: reasons.append("POSSIBLE_TRACK_ID_SWITCH")
    if large_motion: reasons.append("LARGE_POSITION_CHANGE")
    return {"turn":turn,"samples":total,"frames_with_two_faces":valid,
            "coverage_pct":round(100*valid/total,2),
            "low_confidence_frames":low_conf,
            "spatially_ambiguous_frames":collisions,
            "possible_identity_switches":crossings,
            "large_position_changes":large_motion,
            "spatial_tracking_status":"REVIEW_REQUIRED" if reasons else "CANDIDATE_STABLE",
            "reasons":reasons}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("evidence_directory")
    parser.add_argument("--output",default="spatial-tracking-summary.json")
    args=parser.parse_args()
    root=Path(args.evidence_directory)
    base=json.loads((root/"summary.json").read_text())
    if len(base.get("turns",[])) != 6: raise ValueError("SIX_TURNS_REQUIRED")
    turns=[]
    for n in range(1,7):
        evidence=json.loads((root/f"turn-{n}.json").read_text())
        if evidence.get("decision") != "REVIEW_REQUIRED":
            raise ValueError("UNEXPECTED_UPSTREAM_EVIDENCE_DECISION")
        turns.append(summarize(evidence,n))
    report={"attempt_id":base.get("attempt_id"),
            "method":"YuNet spatial left/right heuristic v1",
            "tracks":turns,
            "speakers_verified":False,"mouth_visemes_measured":False,
            "audiovisual_offset_ms":None,"lip_sync_decision":"REVIEW_REQUIRED",
            "limitations":[
              "Spatial left/right is not persistent identity recognition.",
              "YuNet mouth-corner width is not mouth opening or viseme evidence.",
              "Requires human mapping and pretrained calibrated AV estimator."
            ]}
    dst=root/args.output
    if dst.exists():raise FileExistsError("REFUSE_OVERWRITE")
    dst.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({"turns":len(turns),"candidate_stable":sum(x["spatial_tracking_status"]=="CANDIDATE_STABLE" for x in turns),
                      "lip_sync_decision":"REVIEW_REQUIRED","output":str(dst)},indent=2))
if __name__=="__main__":main()
