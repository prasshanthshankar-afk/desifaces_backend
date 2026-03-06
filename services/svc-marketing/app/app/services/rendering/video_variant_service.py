# services/svc-marketing/app/app/services/rendering/video_variant_service.py
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional, Literal

VariantKind = Literal["yt_short", "yt_long"]


@dataclass
class VariantResult:
    path: str


def _ffmpeg_exists() -> bool:
    return shutil.which("ffmpeg") is not None


def render_variant(input_mp4: str, output_mp4: str, variant: VariantKind) -> VariantResult:
    """
    Production-safe normalization:
      - yt_short: 1080x1920 (9:16), pad to fit
      - yt_long : 1920x1080 (16:9), pad to fit

    Ensures:
      - H.264 video + AAC audio
      - +faststart for streaming
    """
    if not _ffmpeg_exists():
        raise RuntimeError("ffmpeg not available in container")

    os.makedirs(os.path.dirname(output_mp4), exist_ok=True)

    if variant == "yt_short":
        W, H = 1080, 1920
    else:
        W, H = 1920, 1080

    vf = f"scale={W}:{H}:force_original_aspect_ratio=decrease," \
         f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1"

    cmd = [
        "ffmpeg", "-y",
        "-i", input_mp4,
        "-vf", vf,
        "-r", "30",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output_mp4,
    ]

    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if p.returncode != 0 or not os.path.exists(output_mp4):
        raise RuntimeError(f"ffmpeg render failed for variant={variant}")

    return VariantResult(path=output_mp4)