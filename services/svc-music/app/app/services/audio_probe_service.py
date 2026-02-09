import json
import subprocess

class AudioProbeService:
    def duration_ms(self, local_path: str) -> int | None:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", local_path]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            return None
        data = json.loads(p.stdout)
        dur = float(data["format"]["duration"])
        return int(dur * 1000)