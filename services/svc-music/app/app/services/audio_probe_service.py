import json
import subprocess
from typing import Optional


class AudioProbeService:
    def duration_ms(self, local_path: str, *, timeout_s: int = 12) -> Optional[int]:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
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
            dur_s = float(data["format"]["duration"])
            if dur_s <= 0:
                return None
            return int(dur_s * 1000)
        except Exception:
            return None