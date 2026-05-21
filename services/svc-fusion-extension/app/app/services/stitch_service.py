from __future__ import annotations

import logging
import math
import os
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.request import Request, urlopen

from azure.storage.blob import BlobServiceClient, ContentSettings

from app.config import settings
from app.services.sas_service import AzureBlobService

logger = logging.getLogger("svc_fusion_extension.stitch_service")


def _tail_text(value: str, limit: int = 1200) -> str:
    s = str(value or "")
    return s if len(s) <= limit else s[-limit:]


def _preview_url(url: Optional[str], keep: int = 96) -> Optional[str]:
    if not url:
        return None
    s = str(url).strip()
    if len(s) <= keep:
        return s
    return s[:keep] + "..."




def _as_dict_loose(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return {}
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def _ffmpeg_preset() -> str:
    return str(os.getenv("LONGFORM_FFMPEG_PRESET", "superfast")).strip() or "superfast"


def _ffmpeg_crf() -> str:
    return str(os.getenv("LONGFORM_FFMPEG_CRF", "22")).strip() or "22"


def _ffmpeg_threads() -> str:
    return str(os.getenv("LONGFORM_FFMPEG_THREADS", "2")).strip() or "2"


def _segment_edge_fade_seconds() -> float:
    return max(0.0, _env_float("LONGFORM_SEGMENT_EDGE_FADE_SECONDS", 0.12))


def _stitch_mode() -> str:
    return str(os.getenv("LONGFORM_STITCH_MODE", "xfade")).strip().lower() or "xfade"


def _presenter_composition_mode() -> str:
    return str(os.getenv("LONGFORM_PRESENTER_COMPOSITION_MODE", "presenter_blend")).strip().lower() or "presenter_blend"


def _presenter_blend_opacity() -> float:
    return min(0.35, max(0.04, _env_float("LONGFORM_PRESENTER_BLEND_OPACITY", 0.14)))


def _stitch_concurrency() -> int:
    return max(1, _env_int("LONGFORM_STITCH_CONCURRENCY", 3))


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def _run(cmd: Sequence[str], *, timeout_seconds: Optional[int] = None) -> None:
    cmd_list = list(cmd)
    started = time.monotonic()
    logger.debug("subprocess start cmd=%s timeout_seconds=%s", " ".join(cmd_list), timeout_seconds)
    try:
        p = subprocess.run(
            cmd_list,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        logger.exception(
            "subprocess timeout elapsed=%.2fs cmd=%s stdout_tail=%s stderr_tail=%s",
            time.monotonic() - started,
            " ".join(cmd_list),
            _tail_text(exc.stdout or ""),
            _tail_text(exc.stderr or ""),
        )
        raise

    elapsed = time.monotonic() - started
    if p.returncode != 0:
        logger.error(
            "subprocess failed rc=%s elapsed=%.2fs cmd=%s stdout_tail=%s stderr_tail=%s",
            p.returncode,
            elapsed,
            " ".join(cmd_list),
            _tail_text(p.stdout),
            _tail_text(p.stderr),
        )
        raise RuntimeError(
            "Command failed:\n"
            f"{' '.join(cmd_list)}\n\n"
            f"STDOUT:\n{p.stdout}\n\n"
            f"STDERR:\n{p.stderr}"
        )
    logger.info("subprocess ok elapsed=%.2fs cmd=%s", elapsed, " ".join(cmd_list))


def _probe_has_audio(input_path: str) -> bool:
    p = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            input_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if p.returncode != 0:
        raise RuntimeError(
            "ffprobe failed:\n"
            f"{input_path}\n\n"
            f"STDERR:\n{p.stderr}"
        )
    return bool((p.stdout or "").strip())


def _probe_duration_seconds(input_path: str) -> Optional[float]:
    p = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            input_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if p.returncode != 0:
        return None
    try:
        return float((p.stdout or "").strip())
    except Exception:
        return None


def probe_duration_seconds(input_path: str) -> Optional[float]:
    return _probe_duration_seconds(input_path)


def _ensure_parent_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _require_nonempty_file(path: str) -> None:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Missing file: {path}")
    if p.stat().st_size <= 0:
        raise RuntimeError(f"Empty file: {path}")


def _concat_escape(path: str) -> str:
    s = str(Path(path).resolve())
    s = s.replace("\\", "/")
    s = s.replace("'", "'\\''")
    return f"'{s}'"


def _write_concat_list(segment_files: List[str], list_path: str) -> None:
    with open(list_path, "w", encoding="utf-8") as f:
        for fp in segment_files:
            _require_nonempty_file(fp)
            f.write(f"file {_concat_escape(fp)}\n")


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _aspect_dimensions(aspect_ratio: Optional[str]) -> Tuple[int, int]:
    ar = (aspect_ratio or "16:9").strip()
    if ar == "9:16":
        return 1080, 1920
    if ar in {"1:1", "square"}:
        return 1080, 1080
    if ar in {"4:5", "portrait_4_5"}:
        return 1080, 1350
    return 1920, 1080




def _fit_pad_filter(width: int, height: int, *, bg: str = "black") -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:{bg},setsar=1"
    )

def _ffmpeg_escape_text(text: str) -> str:
    s = str(text or "")
    s = s.replace("\\", r"\\")
    s = s.replace(":", r"\:")
    s = s.replace("'", r"\'")
    s = s.replace("%", r"\%")
    s = s.replace("[", r"\[")
    s = s.replace("]", r"\]")
    s = s.replace(",", r"\,")
    return s


def _seconds(value: Optional[float], *, default: float = 3.0, minimum: float = 0.8) -> float:
    try:
        v = float(value or default)
    except Exception:
        v = default
    return max(minimum, v)


def _transition_style() -> str:
    style = str(
        getattr(settings, "LONGFORM_SEGMENT_TRANSITION_STYLE", None)
        or os.getenv("LONGFORM_SEGMENT_TRANSITION_STYLE", "fade")
    ).strip().lower()
    return style or "fade"


def _transition_audio_curve() -> str:
    curve = str(
        getattr(settings, "LONGFORM_SEGMENT_AUDIO_XFADE_CURVE", None)
        or os.getenv("LONGFORM_SEGMENT_AUDIO_XFADE_CURVE", "qsin")
    ).strip().lower()
    return curve or "qsin"


def _safe_transition_duration(
    requested_sec: float,
    left_duration_sec: Optional[float],
    right_duration_sec: Optional[float],
) -> float:
    requested = _seconds(requested_sec, default=0.50, minimum=0.18)

    limits = [requested, 0.75]
    for value in (left_duration_sec, right_duration_sec):
        try:
            if value is not None and float(value) > 0:
                limits.append(max(0.18, float(value) * 0.22))
        except Exception:
            logger.exception("stitch_videos xfade duration calculation failed")
            pass

    transition = min(limits)
    return max(0.18, transition)


def download_to_local(url: str, output_path: str, *, timeout_seconds: int = 120) -> str:
    if not url:
        raise ValueError("url must not be empty")
    logger.info("download_to_local start url=%s output_path=%s timeout_seconds=%s", _preview_url(url), output_path, timeout_seconds)
    _ensure_parent_dir(output_path)
    req = Request(url, headers={"User-Agent": "desifaces-fusion-extension/1.0"})
    with urlopen(req, timeout=timeout_seconds) as resp, open(output_path, "wb") as out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    _require_nonempty_file(output_path)
    logger.info("download_to_local ok url=%s output_path=%s bytes=%s", _preview_url(url), output_path, Path(output_path).stat().st_size)
    return output_path


def download_many_to_local(
    downloads: Sequence[Tuple[str, str]],
    *,
    timeout_seconds: int = 120,
    max_workers: Optional[int] = None,
) -> List[str]:
    items = [(url, path) for url, path in downloads if str(url or "").strip() and str(path or "").strip()]
    if not items:
        return []

    workers = max(1, min(len(items), int(max_workers or _stitch_concurrency())))
    logger.info("download_many_to_local start count=%s workers=%s", len(items), workers)

    def _one(item: Tuple[str, str]) -> str:
        url, out_path = item
        return download_to_local(url, out_path, timeout_seconds=timeout_seconds)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        outputs = list(ex.map(_one, items))

    logger.info("download_many_to_local ok count=%s", len(outputs))
    return outputs


def stitch_video_urls(
    segment_urls: List[str],
    out_mp4: str,
    *,
    timeout_seconds: int = 120,
) -> None:
    urls = [str(u or "").strip() for u in (segment_urls or []) if str(u or "").strip()]
    if not urls:
        raise ValueError("segment_urls must not be empty")

    with tempfile.TemporaryDirectory(prefix="df_fusionext_stitch_dl_") as td:
        local_files = [os.path.join(td, f"segment_{i:04d}.bin") for i in range(len(urls))]
        download_many_to_local(list(zip(urls, local_files)), timeout_seconds=timeout_seconds)
        stitch_videos(local_files, out_mp4)


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def normalize_segment_mp4(input_path: str, output_path: str) -> None:
    _require_nonempty_file(input_path)
    _ensure_parent_dir(output_path)

    has_audio = _probe_has_audio(input_path)
    edge_fade = _segment_edge_fade_seconds()
    duration = _probe_duration_seconds(input_path) or 0.0
    vf_parts = ["fps=30", "format=yuv420p"]
    af_parts: List[str] = []

    if edge_fade > 0.0 and duration > (edge_fade * 2.2):
        fade_out_start = max(0.0, duration - edge_fade)
        vf_parts.append(f"fade=t=in:st=0:d={edge_fade:.3f}")
        vf_parts.append(f"fade=t=out:st={fade_out_start:.3f}:d={edge_fade:.3f}")
        if has_audio:
            af_parts.append(f"afade=t=in:st=0:d={edge_fade:.3f}")
            af_parts.append(f"afade=t=out:st={fade_out_start:.3f}:d={edge_fade:.3f}")

    cmd = [
        "ffmpeg",
        "-y",
        "-threads", _ffmpeg_threads(),
        "-threads", _ffmpeg_threads(),
        "-i", input_path,
    ]
    if not has_audio:
        cmd.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-shortest"])

    if vf_parts:
        cmd.extend(["-vf", ",".join(vf_parts)])
    if af_parts:
        cmd.extend(["-af", ",".join(af_parts)])

    cmd.extend([
        "-c:v", "libx264",
        "-preset", _ffmpeg_preset(),
        "-crf", _ffmpeg_crf(),
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-movflags", "+faststart",
        output_path,
    ])

    _run(cmd)
    _require_nonempty_file(output_path)


# ---------------------------------------------------------------------------
# Internal renderers: cards + motion clips + montage
# ---------------------------------------------------------------------------

def render_text_card(
    out_mp4: str,
    *,
    duration_sec: float,
    aspect_ratio: Optional[str] = None,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    body: Optional[str] = None,
    footer: Optional[str] = None,
) -> None:
    width, height = _aspect_dimensions(aspect_ratio)
    duration = _seconds(duration_sec, default=3.0, minimum=1.0)

    filter_parts: List[str] = [f"color=c=black:s={width}x{height}:r=30:d={duration}"]
    vf_parts = ["format=yuv420p"]

    if title:
        vf_parts.append(
            "drawtext="
            f"text='{_ffmpeg_escape_text(title)}':"
            f"x=(w-text_w)/2:y=h*0.22:"
            "fontcolor=white:fontsize=h*0.072:line_spacing=12:"
            "box=1:boxcolor=black@0.35:boxborderw=22"
        )

    if subtitle:
        vf_parts.append(
            "drawtext="
            f"text='{_ffmpeg_escape_text(subtitle)}':"
            f"x=(w-text_w)/2:y=h*0.36:"
            "fontcolor=white:fontsize=h*0.040:line_spacing=10:"
            "box=1:boxcolor=black@0.28:boxborderw=16"
        )

    if body:
        vf_parts.append(
            "drawtext="
            f"text='{_ffmpeg_escape_text(body)}':"
            f"x=w*0.10:y=h*0.56:"
            "fontcolor=white:fontsize=h*0.034:line_spacing=10:"
            "box=1:boxcolor=black@0.25:boxborderw=18"
        )

    if footer:
        vf_parts.append(
            "drawtext="
            f"text='{_ffmpeg_escape_text(footer)}':"
            f"x=(w-text_w)/2:y=h*0.84:"
            "fontcolor=white:fontsize=h*0.028:"
            "box=1:boxcolor=black@0.28:boxborderw=12"
        )

    filtergraph = ",".join(vf_parts)
    _ensure_parent_dir(out_mp4)
    _run([
        "ffmpeg",
        "-y",
        "-threads", _ffmpeg_threads(),
        "-f", "lavfi",
        "-i", filter_parts[0],
        "-f", "lavfi",
        "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-shortest",
        "-vf", filtergraph,
        "-c:v", "libx264",
        "-preset", _ffmpeg_preset(),
        "-crf", _ffmpeg_crf(),
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        out_mp4,
    ])
    _require_nonempty_file(out_mp4)


def render_image_motion_segment(
    image_path: str,
    out_mp4: str,
    *,
    duration_sec: float,
    aspect_ratio: Optional[str] = None,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    footer: Optional[str] = None,
) -> None:
    _require_nonempty_file(image_path)
    _ensure_parent_dir(out_mp4)

    width, height = _aspect_dimensions(aspect_ratio)
    duration = _seconds(duration_sec, default=2.5, minimum=1.0)
    frames = max(1, int(math.ceil(duration * 30.0)))

    vf_parts = [
        (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
            f"zoompan=z='min(zoom+0.0006,1.06)':"
            f"x='max(0,min(iw-iw/zoom,(iw-iw/zoom)/2))':"
            f"y='max(0,min(ih-ih/zoom,(ih-ih/zoom)/2))':"
            f"d={frames}:s={width}x{height}:fps=30"
        ),
        "format=yuv420p",
        "fade=t=in:st=0:d=0.25",
        f"fade=t=out:st={max(0.0, duration - 0.25):.3f}:d=0.25",
    ]

    if title:
        vf_parts.append(
            "drawtext="
            f"text='{_ffmpeg_escape_text(title)}':"
            f"x=w*0.06:y=h*0.10:"
            "fontcolor=white:fontsize=h*0.050:"
            "box=1:boxcolor=black@0.32:boxborderw=18"
        )
    if subtitle:
        vf_parts.append(
            "drawtext="
            f"text='{_ffmpeg_escape_text(subtitle)}':"
            f"x=w*0.06:y=h*0.18:"
            "fontcolor=white:fontsize=h*0.032:"
            "box=1:boxcolor=black@0.25:boxborderw=14"
        )
    if footer:
        vf_parts.append(
            "drawtext="
            f"text='{_ffmpeg_escape_text(footer)}':"
            f"x=w*0.06:y=h*0.88:"
            "fontcolor=white:fontsize=h*0.026:"
            "box=1:boxcolor=black@0.22:boxborderw=12"
        )

    _run([
        "ffmpeg",
        "-y",
        "-threads", _ffmpeg_threads(),
        "-loop", "1",
        "-i", image_path,
        "-f", "lavfi",
        "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-shortest",
        "-t", f"{duration:.3f}",
        "-vf", ",".join(vf_parts),
        "-c:v", "libx264",
        "-preset", _ffmpeg_preset(),
        "-crf", _ffmpeg_crf(),
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        out_mp4,
    ])
    _require_nonempty_file(out_mp4)


def render_video_motion_segment(
    video_url: str,
    out_mp4: str,
    *,
    duration_sec: float,
    aspect_ratio: Optional[str] = None,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    footer: Optional[str] = None,
) -> None:
    if not video_url:
        raise ValueError("video_url must not be empty")

    _ensure_parent_dir(out_mp4)
    width, height = _aspect_dimensions(aspect_ratio)
    duration = _seconds(duration_sec, default=2.5, minimum=1.0)

    with tempfile.TemporaryDirectory(prefix="df_longform_video_") as td:
        src_video = os.path.join(td, "source_video.bin")
        norm_video = os.path.join(td, "source_video_norm.mp4")

        download_to_local(video_url, src_video)
        normalize_segment_mp4(src_video, norm_video)

        vf_parts = [
            _fit_pad_filter(width, height),
            "format=yuv420p",
            "fade=t=in:st=0:d=0.20",
            f"fade=t=out:st={max(0.0, duration - 0.20):.3f}:d=0.20",
        ]

        if title:
            vf_parts.append(
                "drawtext="
                f"text='{_ffmpeg_escape_text(title)}':"
                f"x=w*0.06:y=h*0.10:"
                "fontcolor=white:fontsize=h*0.050:"
                "box=1:boxcolor=black@0.32:boxborderw=18"
            )
        if subtitle:
            vf_parts.append(
                "drawtext="
                f"text='{_ffmpeg_escape_text(subtitle)}':"
                f"x=w*0.06:y=h*0.18:"
                "fontcolor=white:fontsize=h*0.032:"
                "box=1:boxcolor=black@0.25:boxborderw=14"
            )
        if footer:
            vf_parts.append(
                "drawtext="
                f"text='{_ffmpeg_escape_text(footer)}':"
                f"x=w*0.06:y=h*0.88:"
                "fontcolor=white:fontsize=h*0.026:"
                "box=1:boxcolor=black@0.22:boxborderw=12"
            )

        _run([
            "ffmpeg",
            "-y",
            "-stream_loop", "-1",
            "-i", norm_video,
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-t", f"{duration:.3f}",
            "-vf", ",".join(vf_parts),
            "-c:v", "libx264",
            "-preset", _ffmpeg_preset(),
            "-crf", _ffmpeg_crf(),
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "48000",
            "-movflags", "+faststart",
            out_mp4,
        ])
        _require_nonempty_file(out_mp4)


def render_mixed_montage_segment(
    media_items: List[Dict[str, Any]],
    out_mp4: str,
    *,
    duration_sec: float,
    aspect_ratio: Optional[str] = None,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    footer: Optional[str] = None,
) -> None:
    items: List[Dict[str, str]] = []
    for item in media_items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        kind = str(item.get("kind") or "image").strip().lower()
        items.append({
            "kind": "video" if kind == "video" else "image",
            "url": url,
        })

    if not items:
        raise ValueError("media_items must not be empty")

    total_duration = _seconds(duration_sec, default=4.0, minimum=1.2)

    with tempfile.TemporaryDirectory(prefix="df_longform_mixed_montage_") as td:
        local_clips: List[str] = []
        per_clip_duration = max(1.15, total_duration / max(1, len(items)))

        for idx, item in enumerate(items):
            clip_path = os.path.join(td, f"clip_{idx:03d}.mp4")
            clip_title = title if idx == 0 else None
            clip_subtitle = subtitle if idx == 0 else None

            if item["kind"] == "video":
                render_video_motion_segment(
                    item["url"],
                    clip_path,
                    duration_sec=per_clip_duration,
                    aspect_ratio=aspect_ratio,
                    title=clip_title,
                    subtitle=clip_subtitle,
                    footer=footer,
                )
            else:
                img_path = os.path.join(td, f"img_{idx:03d}.bin")
                download_to_local(item["url"], img_path)
                render_image_motion_segment(
                    img_path,
                    clip_path,
                    duration_sec=per_clip_duration,
                    aspect_ratio=aspect_ratio,
                    title=clip_title,
                    subtitle=clip_subtitle,
                    footer=footer,
                )

            local_clips.append(clip_path)

        stitch_videos(local_clips, out_mp4)
        _require_nonempty_file(out_mp4)


def _xfade_pair(
    left_mp4: str,
    right_mp4: str,
    out_mp4: str,
    *,
    transition_duration_sec: float,
) -> None:
    _require_nonempty_file(left_mp4)
    _require_nonempty_file(right_mp4)
    _ensure_parent_dir(out_mp4)

    left_duration = _probe_duration_seconds(left_mp4)
    right_duration = _probe_duration_seconds(right_mp4)
    if left_duration is None:
        raise RuntimeError(f"Unable to probe duration for {left_mp4}")
    if right_duration is None:
        raise RuntimeError(f"Unable to probe duration for {right_mp4}")

    xfade_duration = _safe_transition_duration(
        transition_duration_sec,
        left_duration,
        right_duration,
    )
    offset = max(0.0, float(left_duration) - xfade_duration)
    transition_style = _transition_style()
    audio_curve = _transition_audio_curve()

    _run([
        "ffmpeg",
        "-y",
        "-threads", _ffmpeg_threads(),
        "-i", left_mp4,
        "-i", right_mp4,
        "-filter_complex",
        (
            f"[0:v][1:v]xfade=transition={transition_style}:duration={xfade_duration:.3f}:offset={offset:.3f}[v];"
            f"[0:a][1:a]acrossfade=d={xfade_duration:.3f}:c1={audio_curve}:c2={audio_curve}[a]"
        ),
        "-map", "[v]",
        "-map", "[a]",
        "-c:v", "libx264",
        "-preset", _ffmpeg_preset(),
        "-crf", _ffmpeg_crf(),
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-movflags", "+faststart",
        out_mp4,
    ])
    _require_nonempty_file(out_mp4)


def render_montage_segment(
    image_urls: List[str],
    out_mp4: str,
    *,
    duration_sec: float,
    aspect_ratio: Optional[str] = None,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    footer: Optional[str] = None,
) -> None:
    urls = [u for u in image_urls if str(u or "").strip()]
    if not urls:
        raise ValueError("image_urls must not be empty")

    render_mixed_montage_segment(
        [{"kind": "image", "url": u} for u in urls],
        out_mp4,
        duration_sec=duration_sec,
        aspect_ratio=aspect_ratio,
        title=title,
        subtitle=subtitle,
        footer=footer,
    )


def attach_audio_track(
    video_path: str,
    audio_path: str,
    out_mp4: str,
) -> None:
    _require_nonempty_file(video_path)
    _require_nonempty_file(audio_path)
    _ensure_parent_dir(out_mp4)

    _run([
        "ffmpeg",
        "-y",
        "-threads", _ffmpeg_threads(),
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-shortest",
        "-movflags", "+faststart",
        out_mp4,
    ])
    _require_nonempty_file(out_mp4)




def compose_presenter_with_motion_background(
    presenter_video_url: str,
    background_video_url: str,
    out_mp4: str,
    *,
    aspect_ratio: Optional[str] = None,
    presenter_scale: float = 0.34,
    margin_px: int = 36,
) -> None:
    logger.info(
        "compose_presenter_with_motion_background start presenter_video_url=%s background_video_url=%s out_mp4=%s aspect_ratio=%s composition_mode=%s",
        _preview_url(presenter_video_url),
        _preview_url(background_video_url),
        out_mp4,
        aspect_ratio,
        _presenter_composition_mode(),
    )
    if not presenter_video_url:
        raise ValueError("presenter_video_url must not be empty")

    _ensure_parent_dir(out_mp4)
    width, height = _aspect_dimensions(aspect_ratio)
    mode = _presenter_composition_mode()

    with tempfile.TemporaryDirectory(prefix="df_presenter_bg_") as td:
        fg_src = os.path.join(td, "presenter_src.bin")
        fg_norm = os.path.join(td, "presenter_norm.mp4")

        if mode in {"presenter_fullscreen", "presenter_only"} or not background_video_url:
            download_to_local(presenter_video_url, fg_src)
            normalize_segment_mp4(fg_src, fg_norm)
            _run([
                "ffmpeg", "-y",
                "-threads", _ffmpeg_threads(),
                "-i", fg_norm,
                "-vf", f"{_fit_pad_filter(width, height)},format=yuv420p",
                "-map", "0:v:0",
                "-map", "0:a:0",
                "-c:v", "libx264",
                "-preset", _ffmpeg_preset(),
                "-crf", _ffmpeg_crf(),
                "-pix_fmt", "yuv420p",
                "-r", "30",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "48000",
                "-movflags", "+faststart",
                out_mp4,
            ])
            _require_nonempty_file(out_mp4)
            logger.info("compose_presenter_with_motion_background ok fullscreen-only out_mp4=%s bytes=%s", out_mp4, Path(out_mp4).stat().st_size)
            return

        bg_src = os.path.join(td, "background_src.bin")
        bg_norm = os.path.join(td, "background_norm.mp4")
        download_many_to_local(
            [
                (presenter_video_url, fg_src),
                (background_video_url, bg_src),
            ]
        )
        with ThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(lambda pair: normalize_segment_mp4(*pair), [(fg_src, fg_norm), (bg_src, bg_norm)]))

        if mode == "pip":
            presenter_scale = max(0.20, min(0.50, float(presenter_scale or 0.34)))
            presenter_w = int(width * presenter_scale)
            overlay_x = max(0, width - presenter_w - int(margin_px))
            overlay_y = max(0, height - int(height * 0.42) - int(margin_px))

            filter_complex = (
                f"[0:v]{_fit_pad_filter(width, height)}[bg];"
                f"[1:v]scale={presenter_w}:-2,setsar=1,format=rgba,colorchannelmixer=aa=1[fg];"
                f"[bg]drawbox=x={overlay_x-8}:y={overlay_y-8}:w={presenter_w+16}:h={int(height*0.42)+16}:"
                f"color=black@0.28:t=fill[bgbox];"
                f"[bgbox][fg]overlay=x={overlay_x}:y={overlay_y}:format=auto[v]"
            )
        else:
            opacity = _presenter_blend_opacity()
            filter_complex = (
                f"[0:v]{_fit_pad_filter(width, height)},"
                f"boxblur=10:2,eq=brightness=-0.04:saturation=1.06[bg];"
                f"[1:v]{_fit_pad_filter(width, height)},format=yuv420p[fg];"
                f"[fg][bg]blend=all_mode='normal':all_opacity={opacity:.3f}[v]"
            )

        _run([
            "ffmpeg", "-y",
            "-threads", _ffmpeg_threads(),
            "-i", bg_norm,
            "-i", fg_norm,
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-map", "1:a:0",
            "-c:v", "libx264",
            "-preset", _ffmpeg_preset(),
            "-crf", _ffmpeg_crf(),
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "48000",
            "-shortest",
            "-movflags", "+faststart",
            out_mp4,
        ])
        _require_nonempty_file(out_mp4)
        logger.info("compose_presenter_with_motion_background ok out_mp4=%s bytes=%s", out_mp4, Path(out_mp4).stat().st_size)

# ---------------------------------------------------------------------------
# Stitching / composition
# ---------------------------------------------------------------------------

def stitch_videos(segment_files: List[str], out_mp4: str, *, stitch_mode_override: Optional[str] = None) -> None:
    effective_mode = (str(stitch_mode_override or "").strip().lower() or _stitch_mode())
    logger.info("stitch_videos start out_mp4=%s segment_count=%s mode=%s", out_mp4, len(segment_files or []), effective_mode)
    if not segment_files:
        raise ValueError("segment_files must not be empty")

    _ensure_parent_dir(out_mp4)

    transition_duration = float(
        getattr(settings, "LONGFORM_SEGMENT_TRANSITION_SECONDS", None)
        or os.getenv("LONGFORM_SEGMENT_TRANSITION_SECONDS", "0.70")
    )

    with tempfile.TemporaryDirectory(prefix="df_fusionext_stitch_") as td:
        normalized_files: List[str] = [os.path.join(td, f"norm_{i:04d}.mp4") for i in range(len(segment_files))]

        def _norm_one(item):
            _, src, norm = item
            normalize_segment_mp4(src, norm)

        with ThreadPoolExecutor(max_workers=_stitch_concurrency()) as ex:
            list(ex.map(_norm_one, [(i, src, normalized_files[i]) for i, src in enumerate(segment_files)]))

        logger.info("stitch_videos normalized segment_count=%s", len(normalized_files))
        if len(normalized_files) == 1:
            _run([
                "ffmpeg",
                "-y",
                "-threads", _ffmpeg_threads(),
                "-i", normalized_files[0],
                "-c", "copy",
                "-movflags", "+faststart",
                out_mp4,
            ])
            _require_nonempty_file(out_mp4)
            logger.info("stitch_videos single ok out_mp4=%s bytes=%s", out_mp4, Path(out_mp4).stat().st_size)
            return

        mode = effective_mode
        if mode in {"xfade", "fade"}:
            try:
                running = normalized_files[0]
                for idx in range(1, len(normalized_files)):
                    next_input = normalized_files[idx]
                    xfade_out = os.path.join(td, f"xfade_{idx:04d}.mp4")
                    _xfade_pair(
                        running,
                        next_input,
                        xfade_out,
                        transition_duration_sec=transition_duration,
                    )
                    running = xfade_out

                _run([
                    "ffmpeg",
                    "-y",
                    "-threads", _ffmpeg_threads(),
                    "-i", running,
                    "-c", "copy",
                    "-movflags", "+faststart",
                    out_mp4,
                ])
                _require_nonempty_file(out_mp4)
                logger.info("stitch_videos xfade ok out_mp4=%s bytes=%s", out_mp4, Path(out_mp4).stat().st_size)
                return
            except Exception:
                logger.exception("stitch_videos xfade failed out_mp4=%s", out_mp4)

        concat_list = os.path.join(td, "concat.txt")
        _write_concat_list(normalized_files, concat_list)

        try:
            _run([
                "ffmpeg",
                "-y",
                "-threads", _ffmpeg_threads(),
                "-f", "concat",
                "-safe", "0",
                "-i", concat_list,
                "-c", "copy",
                "-movflags", "+faststart",
                out_mp4,
            ])
            _require_nonempty_file(out_mp4)
            logger.info("stitch_videos concat-copy ok out_mp4=%s bytes=%s", out_mp4, Path(out_mp4).stat().st_size)
            return
        except Exception:
            logger.exception("stitch_videos concat-copy failed out_mp4=%s", out_mp4)

        _run([
            "ffmpeg",
            "-y",
            "-threads", _ffmpeg_threads(),
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list,
            "-c:v", "libx264",
            "-preset", _ffmpeg_preset(),
            "-crf", _ffmpeg_crf(),
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "48000",
            "-movflags", "+faststart",
            out_mp4,
        ])
        _require_nonempty_file(out_mp4)
        logger.info("stitch_videos final ok out_mp4=%s bytes=%s", out_mp4, Path(out_mp4).stat().st_size)


def compose_timeline(
    segment_files: List[str],
    out_mp4: str,
    *,
    job_id: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    overlay_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    stitch_videos(segment_files, out_mp4, stitch_mode_override=_as_dict_loose(overlay_meta).get("stitch_mode"))

    duration_sec = _probe_duration_seconds(out_mp4)
    return {
        "status": "succeeded",
        "job_id": job_id,
        "final_local_path": out_mp4,
        "segment_count": len(segment_files),
        "aspect_ratio": aspect_ratio,
        "duration_sec": duration_sec,
        "overlay_meta": overlay_meta or {},
    }


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def upload_final_mp4(
    local_path: str,
    *,
    storage_path: Optional[str] = None,
) -> Tuple[str, str]:
    logger.info("upload_final_mp4 start local_path=%s storage_path=%s", local_path, storage_path)
    _require_nonempty_file(local_path)

    blob_service = BlobServiceClient.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)
    container_name = settings.AZURE_VIDEO_OUTPUT_CONTAINER
    container = blob_service.get_container_client(container_name)

    prefix = getattr(settings, "AZURE_VIDEO_OUTPUT_PREFIX", None) or "longform"
    storage_path = storage_path or f"{prefix.rstrip('/')}/{uuid.uuid4()}.mp4"

    blob = container.get_blob_client(storage_path)
    with open(local_path, "rb") as f:
        blob.upload_blob(
            f,
            overwrite=True,
            content_settings=ContentSettings(content_type="video/mp4"),
        )

    sas = AzureBlobService(settings.AZURE_STORAGE_CONNECTION_STRING)
    signed_url = sas.sign_read_url(
        container_name,
        storage_path,
        getattr(settings, "FINAL_SAS_TTL_SECONDS", 86400),
    )
    logger.info("upload_final_mp4 ok storage_path=%s signed_url=%s", storage_path, _preview_url(signed_url))
    return storage_path, signed_url
