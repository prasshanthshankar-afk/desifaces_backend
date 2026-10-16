#!/usr/bin/env python3
"""Isolated DEV lip-sync evaluator bridge (no deployments, provider calls or mutations).

Use a locally installed, independently validated SyncNet-compatible evaluator.
It is intentionally NOT an automatic PASS mechanism: third-party offset estimates
require human review and approved calibration before use with the strict gate.

Input manifest JSON:
{
  "attempt_id": "...", "evaluator": {
    "command": ["/opt/av-eval/venv/bin/python", "/opt/av-eval/evaluate.py",
                "--video", "{video}", "--speaker-id", "{speaker}"],
    "model_file": "/opt/av-eval/checkpoints/syncnet.pt",
    "model_sha256": "64 lowercase hex digits", "preprocessing_id": "pinned-v1"
  },
  "turns": [{"turn":1, "speaker_id":"A", "video":"/private/path/turn1.mp4"}, ...]
}
The evaluated process must emit JSON on stdout with:
  offset_ms (signed), confidence (raw model score), valid_tracking (boolean).
The reported offset is an estimator result, NOT certified physical measurement.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SHA_RE = re.compile(r"^[0-9a-f]{64}$")

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def collect(manifest: dict) -> dict:
    if os.uname().nodename.split(".")[0] != "desifaces-dev":
        raise RuntimeError("DEV_HOST_REQUIRED")
    evaluator = manifest.get("evaluator") or {}
    model = Path(str(evaluator.get("model_file") or ""))
    expected = str(evaluator.get("model_sha256") or "")
    command = evaluator.get("command")
    preprocessing = str(evaluator.get("preprocessing_id") or "")
    if not SHA_RE.fullmatch(expected) or not model.is_file() or sha256(model) != expected:
        raise RuntimeError("PINNED_MODEL_CHECKSUM_REQUIRED")
    if not isinstance(command,list) or not command or any(not isinstance(x,str) for x in command):
        raise RuntimeError("EVALUATOR_COMMAND_REQUIRED")
    if not preprocessing:
        raise RuntimeError("PREPROCESSING_VERSION_REQUIRED")
    turns = manifest.get("turns")
    if not isinstance(turns,list) or [t.get("turn") for t in turns if isinstance(t,dict)] != list(range(1,7)):
        raise RuntimeError("EXACTLY_SIX_ORDERED_TURNS_REQUIRED")
    result = {"attempt_id": manifest.get("attempt_id"),
              "evaluator_model_sha256": expected,
              "preprocessing_id": preprocessing,
              "decision": "REVIEW_REQUIRED", "turns": []}
    for turn in turns:
        video = Path(str(turn.get("video") or ""))
        speaker = str(turn.get("speaker_id") or "")
        if not video.is_file() or not speaker:
            result["turns"].append({"turn":turn.get("turn"),"status":"INPUT_MISSING"})
            continue
        args = [x.replace("{video}",str(video)).replace("{speaker}",speaker) for x in command]
        try:
            # Isolated process only. No shell. Never log command or media URLs.
            p = subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                               text=True,timeout=180,check=False)
            if p.returncode:
                raise RuntimeError("EVALUATOR_FAILED")
            payload=json.loads(p.stdout)
            offset=payload.get("offset_ms")
            confidence=payload.get("confidence")
            if type(offset) not in (float,int) or type(confidence) not in (float,int):
                raise ValueError("MISSING_MEASUREMENTS")
            result["turns"].append({
                "turn":turn["turn"], "speaker_id":speaker,
                "estimated_signed_offset_ms":float(offset),
                "model_confidence_raw":float(confidence),
                "valid_tracking":payload.get("valid_tracking") is True,
                "status":"MEASURED_UNCALIBRATED",
            })
        except (ValueError,RuntimeError,subprocess.TimeoutExpired):
            result["turns"].append({"turn":turn["turn"],"speaker_id":speaker,
                                    "status":"INCONCLUSIVE"})
    # Deliberately never emit evaluator_validated=True or a passing verdict.
    # Calibration, human review, and integration gates are separate.
    return result

def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("manifest")
    parser.add_argument("--output", required=True)
    args=parser.parse_args()
    report=collect(json.loads(Path(args.manifest).read_text()))
    output=Path(args.output)
    if output.exists():
        raise RuntimeError("REFUSING_TO_OVERWRITE_EXISTING_EVIDENCE")
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2))
    print(json.dumps({"decision":report["decision"],"turns":len(report["turns"]),
                      "output":str(output)}))
    return 1

if __name__=="__main__":
    sys.exit(main())
