from __future__ import annotations

import asyncio
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.services.audio_probe_service import AudioProbeService

JsonDict = Dict[str, Any]


def _as_dict(x: Any) -> JsonDict:
    return x if isinstance(x, dict) else {}

def _as_list(x: Any) -> List[Any]:
    return x if isinstance(x, list) else []

def _parse_aspect(aspect_ratio: str) -> Tuple[int, int]:
    s = (aspect_ratio or "16:9").strip()
    if s in ("9:16", "9/16"):
        return (1080, 1920)
    if s in ("1:1", "1/1"):
        return (1080, 1080)
    return (1920, 1080)

def _guess_ext(url: str) -> str:
    u = (url or "").split("?", 1)[0].lower()
    for ext in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".mp4"):
        if u.endswith(ext):
            return ext
    return ".mp3"

async def _download(url: str, dst: Path, *, timeout_s: int = 30, max_bytes: int = 250 * 1024 * 1024) -> None:
    # reuse orchestrator helper pattern but keep this file standalone (no urllib in event loop)
    import urllib.request

    dst.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "svc-music-montage"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        n = 0
        with open(dst, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                n += len(chunk)
                if max_bytes and n > max_bytes:
                    raise RuntimeError("download_too_large")
                f.write(chunk)

def _ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


@dataclass(frozen=True)
class MontageResult:
    local_path: Path
    duration_sec: float


class MontageComposerService:
    """
    v1 montage:
      - create per-shot mp4 segments from images using zoompan (Ken Burns)
      - concat segments
      - overlay audio full_mix
    """

    async def compose_from_images_and_audio(
        self,
        *,
        work_dir: Path,
        images: List[Tuple[Path, float]],  # (image_path, duration_sec)
        audio_url: str,
        fps: int,
        aspect_ratio: str,
    ) -> MontageResult:
        if not _ffmpeg_available():
            raise RuntimeError("ffmpeg_not_installed")

        w, h = _parse_aspect(aspect_ratio)
        fps = max(24, min(60, int(fps or 30)))

        work_dir.mkdir(parents=True, exist_ok=True)

        # download audio locally (ffmpeg https support varies)
        audio_path = work_dir / f"audio{_guess_ext(audio_url)}"
        await asyncio.to_thread(_download, audio_url, audio_path)

        # probe duration (best effort)
        dur_probe = AudioProbeService().probe(str(audio_path)) or {}
        audio_dur_sec = float(dur_probe.get("duration_sec") or 0) or 0.0

        seg_dir = work_dir / "segs"
        seg_dir.mkdir(parents=True, exist_ok=True)

        seg_paths: List[Path] = []
        for idx, (img_path, dur_s) in enumerate(images, start=1):
            dur_s = max(0.4, float(dur_s or 0.0))
            frames = max(1, int(math.ceil(dur_s * fps)))

            seg_path = seg_dir / f"seg_{idx:03d}.mp4"

            # Ken Burns: gentle zoom to 1.06 over the clip
            vf = (
                f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},"
                f"zoompan=z='if(eq(on,1),1.0,min(zoom+0.0008,1.06))':d={frames}:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)',"
                f"fps={fps},format=yuv420p"
            )

            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-loop",
                "1",
                "-t",
                f"{dur_s:.3f}",
                "-i",
                str(img_path),
                "-vf",
                vf,
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(seg_path),
            ]
            subprocess.run(cmd, check=True)
            seg_paths.append(seg_path)

        # concat segments
        seglist = work_dir / "seglist.txt"
        with open(seglist, "w", encoding="utf-8") as f:
            for p in seg_paths:
                f.write(f"file '{p.as_posix()}'\n")

        video_path = work_dir / "video_concat.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(seglist),
                "-c:v",
                "copy",
                str(video_path),
            ],
            check=True,
        )

        # mux audio
        out_path = work_dir / "music_video.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-i",
                str(audio_path),
                "-shortest",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(out_path),
            ],
            check=True,
        )

        # compute final duration (fallback to audio duration)
        final_probe = AudioProbeService().probe(str(out_path)) or {}
        out_dur = float(final_probe.get("duration_sec") or 0) or audio_dur_sec

        return MontageResult(local_path=out_path, duration_sec=float(out_dur or 0.0))