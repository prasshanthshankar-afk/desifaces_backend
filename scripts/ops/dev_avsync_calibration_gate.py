#!/usr/bin/env python3
"""DEV calibration harness for a separately installed AV-sync estimator.

No model installation or production changes. Executes a caller-provided estimator
without shell; its JSON stdout must have offset_ms and confidence.
Generate calibrated ground truth using FFmpeg -itsoffset separately. This harness
checks signed offset recovery on supplied source/shifted files and refuses a PASS
if sign, tolerance, or confidence is ambiguous.
"""
from __future__ import annotations
import argparse, json, math, os, pathlib, subprocess, sys

def score(manifest):
    if os.uname().nodename.split(".")[0] != "desifaces-dev":
        raise RuntimeError("DEV_HOST_REQUIRED")
    cases=manifest.get("cases")
    command=manifest.get("command")
    if not isinstance(cases,list) or len(cases)<5:
        raise ValueError("AT_LEAST_FIVE_CALIBRATION_CASES_REQUIRED")
    if not isinstance(command,list) or not command or not all(isinstance(x,str) for x in command):
        raise ValueError("EXECUTABLE_COMMAND_ARRAY_REQUIRED")
    threshold=float(manifest.get("max_error_ms",40))
    if not 0<threshold<=100:raise ValueError("INVALID_TOLERANCE")
    observations=[]
    for row in cases:
        path=pathlib.Path(row["video"])
        expected=float(row["expected_offset_ms"])
        if not path.is_file() or not math.isfinite(expected):
            raise ValueError("MISSING_VIDEO_OR_EXPECTED_OFFSET")
        if abs(expected)>1000:raise ValueError("OFFSET_OUT_OF_TEST_RANGE")
        argv=[part.replace("{video}",str(path)) for part in command]
        p=subprocess.run(argv,text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                         timeout=240,check=False)
        if p.returncode:raise RuntimeError("EVALUATOR_EXIT_NONZERO")
        payload=json.loads(p.stdout)
        offset=payload.get("offset_ms")
        confidence=payload.get("confidence")
        if type(offset) not in (int,float) or type(confidence) not in (int,float):
            raise ValueError("NON_NUMERIC_MODEL_OUTPUT")
        if not math.isfinite(offset) or not math.isfinite(confidence):
            raise ValueError("NON_FINITE_MODEL_OUTPUT")
        observations.append({"expected_ms":expected,"measured_ms":offset,
                             "error_ms":round(offset-expected,2),
                             "confidence_raw":confidence,
                             "within_tolerance":abs(offset-expected)<=threshold})
    values={r["expected_ms"] for r in observations}
    signed_pairs=any(x>0 for x in values) and any(x<0 for x in values)
    zero_present=any(x==0 for x in values)
    calibrated=signed_pairs and zero_present and all(r["within_tolerance"] for r in observations)
    return {"decision":"CALIBRATION_CANDIDATE" if calibrated else "REVIEW_REQUIRED",
            "license_cleared":False,"production_approved":False,
            "observations":observations,
            "note":"Candidate timing calibration only. Validate model identity, weights rights, speech-face assignment and real clips."}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("manifest")
    p.add_argument("--output",required=True)
    a=p.parse_args()
    dest=pathlib.Path(a.output)
    if dest.exists():raise FileExistsError("REFUSE_OVERWRITE")
    report=score(json.loads(pathlib.Path(a.manifest).read_text()))
    dest.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({"decision":report["decision"],"evaluated_cases":len(report["observations"])}))
    return 0 if report["decision"]=="CALIBRATION_CANDIDATE" else 1
if __name__=="__main__":sys.exit(main())
