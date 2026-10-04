#!/usr/bin/env python3
"""Compatibility entry point for the former Kling shared-scene certification.

Group-photo conversation video generation is frozen to OmniHuman 1.5. Keep this
legacy script path executable so older DEV automation fails neither silently nor
by referring to the retired provider contract.
"""
from pathlib import Path
import runpy

root = Path(__file__).resolve().parents[1]
runpy.run_path(
    str(root / "scripts/test-shared-scene-omnihuman-cinematic.py"),
    run_name="__main__",
)
