from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, Optional

JsonDict = Dict[str, Any]


class AudioProbeService:
    """
    v1 contract:
      - probe(local_path) -> dict with duration_sec/duration_ms (+ optional bpm/beats_per_bar)
      - duration_ms(local_path) -> int|None
    Notes:
      - BPM detection from raw audio is non-trivial without DSP libs.
      - v1: parse metadata tags if present (TBPM / time_signature); otherwise leave bpm unset.
    """

    def probe(self, local_path: str, *, timeout_s: int = 12) -> Optional[JsonDict]:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:format_tags=TBPM,time_signature",
            "-of",
            "json",
            local_path,
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        except Exception:
            return None

        if p.returncode != 0 or not (p.stdout or "").strip():
            return None

        try:
            data = json.loads(p.stdout)
            fmt = data.get("format") or {}
            dur_s = float((fmt.get("duration") or 0) or 0)
            if dur_s <= 0:
                return None

            tags = fmt.get("tags") or {}
            bpm = None
            try:
                tbpm = tags.get("TBPM")
                if tbpm is not None:
                    bpm_f = float(tbpm)
                    if bpm_f > 0:
                        bpm = bpm_f
            except Exception:
                bpm = None

            beats_per_bar = 4
            # time_signature like "4/4" or "3/4"
            try:
                ts = tags.get("time_signature")
                if isinstance(ts, str) and "/" in ts:
                    num = int(ts.split("/", 1)[0].strip())
                    if num > 0:
                        beats_per_bar = num
            except Exception:
                beats_per_bar = 4

            return {
                "version": 1,
                "duration_sec": float(dur_s),
                "duration_ms": int(round(dur_s * 1000.0)),
                "bpm": bpm,  # may be None
                "beats_per_bar": int(beats_per_bar),
                "source": "ffprobe",
                "tags": {"TBPM": tags.get("TBPM"), "time_signature": tags.get("time_signature")},
            }
        except Exception:
            return None

    def duration_ms(self, local_path: str, *, timeout_s: int = 12) -> Optional[int]:
        r = self.probe(local_path, timeout_s=timeout_s)
        if not r:
            return None
        try:
            return int(r.get("duration_ms")) if r.get("duration_ms") is not None else None
        except Exception:
            return None