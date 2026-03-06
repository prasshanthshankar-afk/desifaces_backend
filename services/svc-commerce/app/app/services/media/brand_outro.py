from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx


@dataclass
class BrandOutroConfig:
    enable: bool
    logo_url: str
    brand_text: str = "desifaces.ai"
    endcard_seconds: float = 2.0


def _run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"command failed rc={p.returncode}\ncmd={' '.join(cmd)}\nstdout={p.stdout}\nstderr={p.stderr}")


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _download_to(url: str, out_path: Path, max_bytes: int = 20_000_000) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")

    with httpx.Client(follow_redirects=True, timeout=30.0) as client:
        r = client.get(url)
        r.raise_for_status()
        content = r.content
        if len(content) > max_bytes:
            raise RuntimeError(f"logo too large: {len(content)} bytes > {max_bytes}")
        tmp.write_bytes(content)

    tmp.replace(out_path)


def _safe_ext_from_url(url: str, default: str = ".png") -> str:
    try:
        p = urlparse(url).path.lower()
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            if p.endswith(ext):
                return ext
    except Exception:
        pass
    return default


def _ffprobe_wh_fps(in_mp4: Path) -> Tuple[int, int, float]:
    if not _which("ffprobe"):
        raise RuntimeError("ffprobe not found; install ffmpeg in svc-marketing image")

    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "json",
        str(in_mp4),
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe failed\n{p.stderr}")

    data = json.loads(p.stdout)
    s = (data.get("streams") or [{}])[0]
    w = int(s.get("width") or 0)
    h = int(s.get("height") or 0)
    rr = str(s.get("r_frame_rate") or "30/1")
    num, den = rr.split("/")
    fps = float(num) / float(den) if float(den) != 0 else 30.0
    if w <= 0 or h <= 0:
        raise RuntimeError(f"ffprobe returned invalid size w={w} h={h}")
    return w, h, fps


def _ffprobe_has_audio(in_mp4: Path) -> bool:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "json", str(in_mp4)]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        return False
    try:
        data = json.loads(p.stdout)
        return bool(data.get("streams"))
    except Exception:
        return False


def _ensure_audio_stream(in_mp4: Path, out_mp4: Path) -> Path:
    # If input has audio, just copy.
    if _ffprobe_has_audio(in_mp4):
        if in_mp4 != out_mp4:
            shutil.copyfile(in_mp4, out_mp4)
        return out_mp4

    # Add silent audio (keep video bitstream copy).
    _run([
        "ffmpeg", "-y",
        "-i", str(in_mp4),
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-shortest",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        str(out_mp4),
    ])
    return out_mp4


def _render_endcard(logo_path: Path, w: int, h: int, fps: float, seconds: float, brand_text: str, out_mp4: Path) -> Path:
    # Conservative sizing for vertical/horizontal.
    logo_w = max(160, int(w * 0.22))
    font_size = max(28, int(h * 0.045))

    # Place logo upper-ish, text lower-ish.
    logo_y = int(h * 0.18)
    text_y = int(h * 0.78)

    # Note: drawtext uses fontconfig; install fonts-dejavu-core in Docker for reliability.
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-t", f"{seconds}", "-i", f"color=c=black:s={w}x{h}:r={fps}",
        "-loop", "1", "-t", f"{seconds}", "-i", str(logo_path),
        "-f", "lavfi", "-t", f"{seconds}", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex",
        (
            f"[1:v]scale={logo_w}:-1[lg];"
            f"[0:v][lg]overlay=x=(W-w)/2:y={logo_y}:format=auto,"
            f"drawtext=text='{brand_text}':x=(w-text_w)/2:y={text_y}:fontsize={font_size}:fontcolor=white"
            f"[v]"
        ),
        "-map", "[v]",
        "-map", "2:a",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "128k",
        str(out_mp4),
    ])
    return out_mp4


def brand_with_endcard(in_mp4: Path, cfg: BrandOutroConfig, work_dir: Path) -> Path:
    """
    Returns path to branded mp4 (original + end-card).
    """
    if not cfg.enable:
        return in_mp4

    if not _which("ffmpeg"):
        raise RuntimeError("ffmpeg not found; install ffmpeg in svc-marketing image")

    work_dir.mkdir(parents=True, exist_ok=True)

    # Cache logo locally by URL.
    ext = _safe_ext_from_url(cfg.logo_url, ".png")
    logo_path = work_dir / f"brand_logo{ext}"
    if not logo_path.exists():
        _download_to(cfg.logo_url, logo_path)

    w, h, fps = _ffprobe_wh_fps(in_mp4)

    # Ensure audio exists so concat always works.
    normalized = work_dir / "input_with_audio.mp4"
    _ensure_audio_stream(in_mp4, normalized)

    endcard_mp4 = work_dir / "endcard.mp4"
    _render_endcard(logo_path, w, h, fps, cfg.endcard_seconds, cfg.brand_text, endcard_mp4)

    branded = work_dir / "branded.mp4"
    _run([
        "ffmpeg", "-y",
        "-i", str(normalized),
        "-i", str(endcard_mp4),
        "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]",
        "-map", "[v]",
        "-map", "[a]",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "128k",
        str(branded),
    ])
    return branded