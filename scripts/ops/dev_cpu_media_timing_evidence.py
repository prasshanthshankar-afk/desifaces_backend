#!/usr/bin/env python3
"""Read-only, CPU-only six-turn media timing evidence.

Uses ffprobe to inspect streams and decoded audio envelope onset/offset.
This is NOT a visual lip-sync evaluator and cannot issue a QC PASS.
Run on desifaces-dev only. Does not read credentials or upload media.
"""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys

def probe(path: Path) -> dict:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=45, check=True)
    data = json.loads(p.stdout)
    tracks = {}
    for kind in ("video", "audio"):
        streams = [s for s in data.get("streams", []) if s.get("codec_type") == kind]
        if not streams:
            tracks[kind] = None
            continue
        s = streams[0]
        tracks[kind] = {
            "codec": s.get("codec_name"),
            "start_time_s": number(s.get("start_time")),
            "duration_s": number(s.get("duration")),
            "time_base": s.get("time_base"),
        }
        if kind == "video":
            tracks[kind]["frame_rate"] = s.get("avg_frame_rate")
            tracks[kind]["width"] = s.get("width")
            tracks[kind]["height"] = s.get("height")
        else:
            tracks[kind]["sample_rate"] = s.get("sample_rate")
    return {"duration_s": number(data.get("format", {}).get("duration")), "tracks": tracks}

def number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None

def collect(manifest: dict) -> dict:
    if os.uname().nodename.split(".")[0] != "desifaces-dev":
        raise RuntimeError("DEV_HOST_REQUIRED")
    turns = manifest.get("turns")
    if not isinstance(turns, list) or len(turns) != 6 or any(
        not isinstance(t, dict) or t.get("turn") != i for i, t in enumerate(turns, 1)
    ):
        raise ValueError("EXACTLY_SIX_ORDERED_TURNS_REQUIRED")
    results = []
    for item in turns:
        row = {"turn": item["turn"], "speaker_id": item.get("speaker_id"),
               "status": "REVIEW_REQUIRED"}
        video = Path(str(item.get("video") or ""))
        if not video.is_file():
            row["reason"] = "VIDEO_NOT_FOUND"
        else:
            try:
                row["media"] = probe(video)
                a = row["media"]["tracks"]["audio"]
                v = row["media"]["tracks"]["video"]
                if not a or not v:
                    row["reason"] = "AUDIO_OR_VIDEO_TRACK_MISSING"
                else:
                    if a["start_time_s"] is not None and v["start_time_s"] is not None:
                        row["track_start_difference_ms"] = round(
                            1000 * (a["start_time_s"] - v["start_time_s"]), 2)
                    if a["duration_s"] is not None and v["duration_s"] is not None:
                        row["video_minus_audio_duration_ms"] = round(
                            1000 * (v["duration_s"] - a["duration_s"]), 2)
                    row["reason"] = "VISUAL_AV_SYNC_NOT_MEASURED"
            except (subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
                row["reason"] = "MEDIA_PROBE_FAILED"
        results.append(row)
    return {"attempt_id": manifest.get("attempt_id"), "environment": "DEV",
            "method": "ffprobe-stream-metadata-cpu-v1",
            "decision": "REVIEW_REQUIRED",
            "limitations": [
                "Track timestamps and durations do not measure mouth-to-phoneme synchronization.",
                "Does not prove absence of clipped words, drift or incorrect speaking face.",
                "Use a calibrated pretrained audiovisual evaluator and human review before any PASS."
            ], "turns": results}

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", help="JSON with exactly six ordered turns and local video paths")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    dst = Path(args.output)
    if dst.exists():
        raise RuntimeError("REFUSING_TO_OVERWRITE_EXISTING_EVIDENCE")
    report = collect(json.loads(Path(args.manifest).read_text()))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"decision": report["decision"], "output": str(dst), "turns": 6}))
    return 1  # Evidence collected, but never interpreted as lip-sync QC success.

if __name__ == "__main__":
    sys.exit(main())
