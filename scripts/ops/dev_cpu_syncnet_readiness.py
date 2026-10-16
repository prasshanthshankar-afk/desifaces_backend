#!/usr/bin/env python3
"""Read-only CPU SyncNet host readiness inventory. Never downloads or installs anything.

Safe on a verified desifaces-dev host. A result of READY_FOR_STAGING does not
mean a pretrained evaluator is present, calibrated, or production-licensed.
"""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

def run(argv):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=12, check=False)
        return p.stdout.strip()[:300] if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None

def main():
    if os.uname().nodename.split(".")[0] != "desifaces-dev":
        print(json.dumps({"decision": "BLOCKED", "reason": "DEV_HOST_REQUIRED"}))
        return 2
    usage = shutil.disk_usage("/")
    mem_kib = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                mem_kib = int(line.split()[1])
    except (OSError, ValueError):
        pass
    result = {
        "environment": "DEV",
        "decision": "REVIEW_REQUIRED",
        "gpu_required": False,
        "free_root_gib": round(usage.free / 1024**3, 2),
        "available_ram_gib": round(mem_kib / 1024**2, 2) if mem_kib is not None else None,
        "python": sys.version.split()[0],
        "ffmpeg": run(["ffmpeg", "-version"]),
        "ffprobe": run(["ffprobe", "-version"]),
        "git": run(["git", "--version"]),
        "cpu_model_modules": {
            x: importlib.util.find_spec(x) is not None
            for x in ("torch", "cv2", "numpy", "scipy")
        },
        "checkpoint_present": "NOT_ASSESSED",
        "model_license_cleared": False,
        "visual_offset_measured": False,
        "production_approved": False,
        "next_action": "STAGE_PINNED_CPU_EVALUATOR_IN_ISOLATED_ENV_AFTER_STORAGE_AND_LICENSE_REVIEW",
    }
    if result["free_root_gib"] < 5:
        result["next_action"] = "INSUFFICIENT_DISK_HEADROOM_AVOID_DEPENDENCY_INSTALL"
    if result["available_ram_gib"] is not None and result["available_ram_gib"] < 2:
        result["next_action"] = "INSUFFICIENT_MEMORY_AVOID_CPU_MODEL_INSTALL"
    print(json.dumps(result, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
