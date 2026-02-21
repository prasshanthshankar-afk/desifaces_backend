from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from app.config import settings
from app.repos.music_jobs_repo import MusicJobsRepo
from app.repos.steps_repo import StepsRepo
from app.services.azure_storage_service import AzureStorageService
from app.services.music_orchestrator_common import (
    _as_dict,
    _clamp_int,
    _download_http_to_file,
    _guess_ext_from_url,
)
from app.services.music_orchestrator_audio import pick_audio_url_for_probe
from app.services.music_orchestrator_studio_jobs import persist_studio_payload_best_effort

JsonDict = Dict[str, Any]

# Cache NVENC probe (per-process)
_NVENC_USABLE: Optional[bool] = None


# -----------------------------
# Env/Settings knobs
# -----------------------------
def _get_int(name: str, default: int) -> int:
    v = getattr(settings, name, None)
    if v is None:
        v = os.getenv(name)
    try:
        return int(float(v)) if v is not None else int(default)
    except Exception:
        return int(default)


def _get_bool(name: str, default: bool) -> bool:
    v = getattr(settings, name, None)
    if v is None:
        v = os.getenv(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _get_str(name: str, default: str) -> str:
    v = getattr(settings, name, None)
    if v is None:
        v = os.getenv(name)
    s = str(v).strip() if v is not None else ""
    return s or default


# -----------------------------
# Blob naming helpers
# -----------------------------
def _montage_blob_filename(*, job_id: UUID, run_id: str, filename: str) -> str:
    """
    Ensure every montage output is unique per job+run to avoid stale/older-run URLs.
    We keep everything under: montage/{job_id}/{run_id}/...
    """
    fn = (filename or "").lstrip("/")
    if fn.startswith("montage/"):
        fn = fn[len("montage/") :]
    fn = fn or "output.mp4"
    return f"montage/{job_id}/{run_id}/{fn}"


# -----------------------------
# Deterministic motion seed
# -----------------------------
def _stable_unit_float(*parts: str) -> float:
    """
    Deterministic float in [0,1) derived from sha256(parts...).
    """
    s = "|".join([p for p in parts if p is not None])
    h = hashlib.sha256(s.encode("utf-8")).digest()
    n = int.from_bytes(h[:8], "big", signed=False)
    return (n % (2**53)) / float(2**53)  # stable double-friendly range


# -----------------------------
# FFmpeg helpers
# -----------------------------
def _ffmpeg_timeout_s() -> int:
    return _get_int("MUSIC_FFMPEG_TIMEOUT_SECS", 60 * 20)


def _ffmpeg_run(cmd: List[str], *, timeout_s: Optional[int] = None) -> None:
    import logging

    logger = logging.getLogger(__name__)
    logger.info("ffmpeg cmd=%s", " ".join(cmd))

    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=int(timeout_s or _ffmpeg_timeout_s()),
        )
    except subprocess.TimeoutExpired as e:
        tail = ""
        if getattr(e, "stdout", None):
            tail = str(e.stdout)[-4000:]
        raise RuntimeError(f"ffmpeg_timeout out={tail}") from e

    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg_failed rc={p.returncode} out={p.stdout[-4000:]}")


def _ffprobe_duration_s(path: Path) -> Optional[float]:
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
        if p.returncode != 0:
            return None
        s = (p.stdout or "").strip()
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def _ffmpeg_has_nvenc() -> bool:
    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
        )
        if p.returncode != 0:
            return False
        out = (p.stdout or "").lower()
        return "h264_nvenc" in out
    except Exception:
        return False


def _has_nvidia_device() -> bool:
    candidates = [
        "/dev/nvidia0",
        "/dev/nvidiactl",
        "/proc/driver/nvidia/version",
    ]
    try:
        return any(Path(p).exists() for p in candidates)
    except Exception:
        return False


def _ffmpeg_nvenc_usable() -> bool:
    global _NVENC_USABLE
    if _NVENC_USABLE is not None:
        return _NVENC_USABLE

    if not _ffmpeg_has_nvenc():
        _NVENC_USABLE = False
        return _NVENC_USABLE

    if not _has_nvidia_device():
        _NVENC_USABLE = False
        return _NVENC_USABLE

    try:
        p = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "nullsrc=size=64x64:rate=1",
                "-t",
                "0.2",
                "-c:v",
                "h264_nvenc",
                "-f",
                "null",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
        out = (p.stdout or "").lower()
        if p.returncode == 0:
            _NVENC_USABLE = True
        else:
            if "libcuda" in out or "operation not permitted" in out or "could not open encoder" in out:
                _NVENC_USABLE = False
            else:
                _NVENC_USABLE = False
    except Exception:
        _NVENC_USABLE = False

    return _NVENC_USABLE


def _video_encoder_args(*, enable_nvenc: bool, x264_preset: str, crf: int) -> List[str]:
    if enable_nvenc and _ffmpeg_nvenc_usable():
        cq = _clamp_int(_get_int("MUSIC_MONTAGE_NVENC_CQ", 23), 10, 35)
        bitrate = _get_str("MUSIC_MONTAGE_NVENC_BITRATE", "6M")
        preset = _get_str("MUSIC_MONTAGE_NVENC_PRESET", "p4")
        return ["-c:v", "h264_nvenc", "-preset", preset, "-rc", "vbr", "-cq", str(cq), "-b:v", bitrate]
    return ["-c:v", "libx264", "-preset", x264_preset, "-crf", str(crf)]


def _vf_fit_pad(w: int, h: int) -> str:
    return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"


# -----------------------------
# Download & render (parallel)
# -----------------------------
async def _download_to_file_async(url: str, path: Path, *, timeout_s: int, max_bytes: int) -> None:
    await asyncio.to_thread(_download_http_to_file, url, path, timeout_s=timeout_s, max_bytes=max_bytes)


async def _render_image_segment_async(
    *,
    img_path: Path,
    seg_path: Path,
    w: int,
    h: int,
    fps: int,
    frames: int,
    enable_nvenc: bool,
    x264_preset: str,
    crf: int,
    motion_seed: float,
) -> None:
    """
    Render an image as a short video segment with dynamic motion.
    Uses zoompan + gentle drift; motion parameters vary deterministically per clip.
    """
    # Base knobs (global)
    z_step_base = float(_get_int("MUSIC_MONTAGE_ZOOMPAN_STEP_X1E6", 500) / 1_000_000.0)
    z_max_base = float(_get_int("MUSIC_MONTAGE_ZOOMPAN_MAX_X100", 104) / 100.0)

    # Per-segment variation (deterministic)
    z_step = z_step_base * (0.65 + 0.90 * float(motion_seed))          # ~0.65x..1.55x
    z_max = z_max_base + (float(motion_seed) - 0.5) * 0.06             # +/- 0.03

    # Drift amplitude & speed
    amp = 0.22 + 0.18 * float(motion_seed)  # 0.22..0.40
    sx = 18.0 + 20.0 * float(motion_seed)   # 18..38
    sy = 22.0 + 18.0 * float(motion_seed)   # 22..40

    # Phase offsets (avoid identical motion between clips)
    phx = 3.14159 * (0.30 + 1.40 * float(motion_seed))
    phy = 3.14159 * (0.20 + 1.60 * float(motion_seed))

    # We drift around center; clamp to [0,1] before multiplying by (iw-iw/zoom).
    x_expr = f"(iw-iw/zoom)*max(0,min(1,0.5+{amp:.3f}*sin(on/{sx:.3f}+{phx:.3f})))"
    y_expr = f"(ih-ih/zoom)*max(0,min(1,0.5+{amp:.3f}*cos(on/{sy:.3f}+{phy:.3f})))"

    vf = (
        f"{_vf_fit_pad(w, h)},"
        f"zoompan=z='if(eq(on,1),1.0,min(zoom+{z_step:.6f},{z_max:.3f}))':"
        f"x='{x_expr}':y='{y_expr}':d={int(frames)}:fps={int(fps)},"
        "format=yuv420p"
    )

    enc = _video_encoder_args(enable_nvenc=enable_nvenc, x264_preset=x264_preset, crf=crf)

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(img_path),
        "-vf",
        vf,
        "-frames:v",
        str(int(frames)),
        "-r",
        str(int(fps)),
        *enc,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(seg_path),
    ]
    await asyncio.to_thread(_ffmpeg_run, cmd)


async def _render_video_segment_async(
    *,
    video_path: Path,
    seg_path: Path,
    w: int,
    h: int,
    fps: int,
    dur_sec: float,
    start_sec: float,
    loop: bool,
    enable_nvenc: bool,
    x264_preset: str,
    crf: int,
) -> None:
    """
    Trim a segment from a performer video, fit+pad (no crop), no audio.
    If loop=True we use -stream_loop -1 so segments never end early.
    """
    enc = _video_encoder_args(enable_nvenc=enable_nvenc, x264_preset=x264_preset, crf=crf)
    vf = f"{_vf_fit_pad(w, h)},fps={int(fps)},format=yuv420p"

    if loop:
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-stream_loop",
            "-1",
            "-i",
            str(video_path),
            "-ss",
            f"{max(0.0, float(start_sec)):.3f}",
            "-t",
            f"{max(0.1, float(dur_sec)):.3f}",
            "-vf",
            vf,
            "-an",
            "-r",
            str(int(fps)),
            *enc,
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(seg_path),
        ]
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, float(start_sec)):.3f}",
            "-t",
            f"{max(0.1, float(dur_sec)):.3f}",
            "-i",
            str(video_path),
            "-vf",
            vf,
            "-an",
            "-r",
            str(int(fps)),
            *enc,
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(seg_path),
        ]
    await asyncio.to_thread(_ffmpeg_run, cmd)


async def _transcode_async(cmd: List[str]) -> None:
    await asyncio.to_thread(_ffmpeg_run, cmd)


async def _render_montage_local_parallel(
    *,
    job_id: UUID,
    manifest: JsonDict,
    broll_by_clip: Dict[str, str],
    audio_url: str,
    out_dir: Path,
    performer_url: Optional[str],
    scene_no_face: bool,
) -> Tuple[Dict[str, Path], Dict[str, Any]]:
    clips = manifest.get("clips")
    if not isinstance(clips, list) or not clips:
        raise RuntimeError("montage_missing_clips")

    out_dir.mkdir(parents=True, exist_ok=True)

    # knobs
    download_parallel = _clamp_int(_get_int("MUSIC_MONTAGE_DOWNLOAD_PARALLEL", 8), 1, 32)
    render_parallel = _clamp_int(_get_int("MUSIC_MONTAGE_RENDER_PARALLEL", 3), 1, 12)
    enable_exports = _get_bool("MUSIC_MONTAGE_ENABLE_EXPORTS", True)
    enable_nvenc = _get_bool("MUSIC_MONTAGE_ENABLE_NVENC", True)
    x264_preset = _get_str("MUSIC_MONTAGE_X264_PRESET", "veryfast")
    crf = _clamp_int(_get_int("MUSIC_MONTAGE_CRF", 23), 16, 35)

    # performer intercut knobs (default TRUE if performer_url exists; can disable via env)
    enable_performer_intercut = _get_bool("MUSIC_MONTAGE_ENABLE_PERFORMER_INTERCUT", True)
    performer_every_n = _clamp_int(_get_int("MUSIC_MONTAGE_PERFORMER_EVERY_N", 3), 1, 50)
    performer_offset = _clamp_int(_get_int("MUSIC_MONTAGE_PERFORMER_OFFSET", 1), 0, 20)
    performer_max_ratio = float(_get_int("MUSIC_MONTAGE_PERFORMER_MAX_RATIO_X100", 40)) / 100.0  # default 0.40
    performer_min_sec = float(_get_int("MUSIC_MONTAGE_PERFORMER_MIN_SEC_X10", 12)) / 10.0        # default 1.2

    performer_enabled = bool(enable_performer_intercut and performer_url and not scene_no_face)

    # output format
    w, h = 1920, 1080
    target = _as_dict(manifest.get("target"))
    try:
        fps = int(target.get("fps") or 30)
    except Exception:
        fps = 30
    fps = _clamp_int(fps, 24, 60)

    # 1) Download audio
    audio_path = out_dir / f"audio{_guess_ext_from_url(audio_url)}"
    await _download_to_file_async(audio_url, audio_path, timeout_s=60, max_bytes=250 * 1024 * 1024)

    # 1b) Download performer (optional)
    performer_path: Optional[Path] = None
    performer_dur_s: Optional[float] = None
    if performer_enabled and performer_url:
        performer_path = out_dir / f"performer{_guess_ext_from_url(performer_url) or '.mp4'}"
        await _download_to_file_async(str(performer_url), performer_path, timeout_s=90, max_bytes=600 * 1024 * 1024)
        performer_dur_s = _ffprobe_duration_s(performer_path)

    # 2) Build render plan (clip order preserved)
    plan: List[Dict[str, Any]] = []
    for i, clip in enumerate(clips):
        if not isinstance(clip, dict):
            continue
        clip_id = str(clip.get("clip_id") or "").strip()
        if not clip_id:
            continue

        img_url = broll_by_clip.get(clip_id)
        if not img_url:
            continue

        dur = float(clip.get("duration_sec") or 0) or max(
            0.6,
            float(clip.get("end_sec") or 0) - float(clip.get("start_sec") or 0),
        )
        dur = max(0.6, min(10.0, float(dur)))

        # decide performer usage for this clip
        use_perf = False
        if performer_enabled and performer_path is not None:
            if dur >= performer_min_sec:
                use_perf = (((i + performer_offset) % performer_every_n) == 0)

        frames = int(round(dur * fps))
        frames = max(1, int(frames))

        start_sec = 0.0
        try:
            start_sec = float(clip.get("start_sec") or 0.0)
        except Exception:
            start_sec = 0.0

        motion_seed = _stable_unit_float(str(job_id), clip_id, str(i))

        plan.append(
            {
                "i": i,
                "clip_id": clip_id,
                "img_url": str(img_url),
                "dur": float(dur),
                "frames": int(frames),
                "start_sec": float(start_sec),
                "use_performer": bool(use_perf),
                "motion_seed": float(motion_seed),
            }
        )

    if not plan:
        raise RuntimeError("montage_no_segments_rendered_plan_empty")

    # Cap performer ratio (so we still have b-roll)
    if performer_enabled:
        desired = [p for p in plan if p.get("use_performer")]
        max_perf = int(max(1, int(round(len(plan) * max(0.05, min(0.9, performer_max_ratio))))))
        if len(desired) > max_perf:
            k = max(1, int(round(len(desired) / max_perf)))
            keep = set()
            for idx, p in enumerate(desired):
                if idx % k == 0 and len(keep) < max_perf:
                    keep.add(int(p["i"]))
            for p in plan:
                if p.get("use_performer") and int(p["i"]) not in keep:
                    p["use_performer"] = False

    # 3) Download all images concurrently (bounded)
    dl_sem = asyncio.Semaphore(download_parallel)

    async def _dl_one(item: Dict[str, Any]) -> Dict[str, Any]:
        async with dl_sem:
            img_path = out_dir / f"img_{int(item['i']):03d}.jpg"
            await _download_to_file_async(item["img_url"], img_path, timeout_s=60, max_bytes=50 * 1024 * 1024)
            item["img_path"] = img_path
            return item

    dl_tasks = [asyncio.create_task(_dl_one(dict(x))) for x in plan]
    dl_results = await asyncio.gather(*dl_tasks, return_exceptions=True)

    ok_plan: List[Dict[str, Any]] = []
    errors: List[str] = []
    for r in dl_results:
        if isinstance(r, Exception):
            errors.append(str(r))
            continue
        if isinstance(r, dict) and r.get("img_path"):
            ok_plan.append(r)

    if not ok_plan:
        raise RuntimeError(f"montage_all_image_downloads_failed errors={errors[:3]}")

    ok_plan.sort(key=lambda x: int(x["i"]))

    # 4) Render segments concurrently (bounded)
    r_sem = asyncio.Semaphore(render_parallel)

    async def _render_one(item: Dict[str, Any]) -> Path:
        async with r_sem:
            seg = out_dir / f"seg_{int(item['i']):03d}.mp4"
            if performer_enabled and performer_path is not None and bool(item.get("use_performer")):
                dur_sec = float(item["dur"])
                start = float(item.get("start_sec") or 0.0)

                loop = False
                if performer_dur_s and performer_dur_s > 0.5:
                    if performer_dur_s < dur_sec + 0.05:
                        loop = True
                        start = 0.0
                    else:
                        max_start = max(0.0, performer_dur_s - dur_sec - 0.05)
                        if max_start > 0.0:
                            start = float(start % max_start)
                        else:
                            start = 0.0
                else:
                    loop = True
                    start = 0.0

                await _render_video_segment_async(
                    video_path=performer_path,
                    seg_path=seg,
                    w=w,
                    h=h,
                    fps=fps,
                    dur_sec=dur_sec,
                    start_sec=start,
                    loop=loop,
                    enable_nvenc=enable_nvenc,
                    x264_preset=x264_preset,
                    crf=crf,
                )
            else:
                await _render_image_segment_async(
                    img_path=Path(item["img_path"]),
                    seg_path=seg,
                    w=w,
                    h=h,
                    fps=fps,
                    frames=int(item["frames"]),
                    enable_nvenc=enable_nvenc,
                    x264_preset=x264_preset,
                    crf=crf,
                    motion_seed=float(item.get("motion_seed") or 0.123),
                )
            return seg

    render_tasks = [asyncio.create_task(_render_one(x)) for x in ok_plan]
    render_results = await asyncio.gather(*render_tasks, return_exceptions=True)

    seg_paths: List[Path] = []
    for r in render_results:
        if isinstance(r, Exception):
            errors.append(str(r))
            continue
        seg_paths.append(Path(r))

    seg_paths = sorted(seg_paths)
    if not seg_paths:
        raise RuntimeError(f"montage_no_segments_rendered errors={errors[:3]}")

    # 5) Concat video (-c copy)
    concat_list = out_dir / "concat_list.txt"
    concat_list.write_text("".join([f"file '{p.as_posix()}'\n" for p in seg_paths]), encoding="utf-8")

    video_only = out_dir / "video_only.mp4"
    await asyncio.to_thread(
        _ffmpeg_run,
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
            str(concat_list),
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            str(video_only),
        ],
    )

    # 6) Mux audio
    final_1080p = out_dir / "final_16x9_1080p.mp4"
    await asyncio.to_thread(
        _ffmpeg_run,
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_only),
            "-i",
            str(audio_path),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(final_1080p),
        ],
    )

    # 7) Preview + exports
    enc2 = _video_encoder_args(
        enable_nvenc=enable_nvenc,
        x264_preset=_get_str("MUSIC_MONTAGE_X264_PRESET_2", "veryfast"),
        crf=_clamp_int(_get_int("MUSIC_MONTAGE_CRF_2", 22), 16, 35),
    )

    preview_720p = out_dir / "preview_720p.mp4"
    cmd_preview = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(final_1080p),
        "-vf",
        _vf_fit_pad(1280, 720),
        *enc2,
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(preview_720p),
    ]

    tasks2 = [asyncio.create_task(_transcode_async(cmd_preview))]

    export_9x16 = out_dir / "export_9x16_1080x1920.mp4"
    export_1x1 = out_dir / "export_1x1_1080x1080.mp4"

    if enable_exports:
        cmd_9x16 = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(final_1080p),
            "-vf",
            _vf_fit_pad(1080, 1920),
            *enc2,
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(export_9x16),
        ]
        cmd_1x1 = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(final_1080p),
            "-vf",
            _vf_fit_pad(1080, 1080),
            *enc2,
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(export_1x1),
        ]
        tasks2.append(asyncio.create_task(_transcode_async(cmd_9x16)))
        tasks2.append(asyncio.create_task(_transcode_async(cmd_1x1)))

    trans_res = await asyncio.gather(*tasks2, return_exceptions=True)
    for r in trans_res:
        if isinstance(r, Exception):
            errors.append(str(r))

    out_paths: Dict[str, Path] = {
        "preview": preview_720p,
        "final": final_1080p,
    }
    if enable_exports:
        out_paths["export_9x16"] = export_9x16
        out_paths["export_1x1"] = export_1x1

    perf = {
        "download_parallel": download_parallel,
        "render_parallel": render_parallel,
        "enable_exports": enable_exports,
        "enable_nvenc_env": enable_nvenc,
        "nvenc_usable": _ffmpeg_nvenc_usable(),
        "nvenc_used": bool(enable_nvenc and _ffmpeg_nvenc_usable()),
        "performer_enabled": performer_enabled,
        "performer_used": bool(performer_enabled and performer_path is not None and any(p.get("use_performer") for p in ok_plan)),
        "performer_every_n": performer_every_n,
        "performer_offset": performer_offset,
        "performer_max_ratio": performer_max_ratio,
        "performer_min_sec": performer_min_sec,
        "performer_duration_s": performer_dur_s,
        "segments": len(seg_paths),
        "errors_count": len(errors),
    }

    return out_paths, perf


# -----------------------------
# Media asset insert + uploads
# -----------------------------
async def _create_media_asset_best_effort(
    *,
    pool,
    user_id: UUID,
    storage_ref: str,
    content_type: str,
    duration_ms: Optional[int],
    meta_json: Optional[JsonDict] = None,
) -> Optional[UUID]:
    cols = set()
    try:
        rows = await pool.fetch(
            """
            select column_name
            from information_schema.columns
            where table_schema='public' and table_name='media_assets'
            """
        )
        cols = {str(r["column_name"]) for r in rows}
    except Exception:
        cols = set()

    if not {"id", "user_id", "storage_ref"}.issubset(cols):
        return None

    asset_id = uuid4()

    meta = dict(meta_json or {})
    try:
        c, p = AzureStorageService.parse_blob_url(storage_ref)
        if c and not meta.get("container"):
            meta["container"] = c
        if p and not meta.get("storage_path"):
            meta["storage_path"] = p
    except Exception:
        pass

    insert_cols: List[str] = ["id", "user_id", "storage_ref"]
    params: List[Any] = [asset_id, user_id, storage_ref]
    values: List[str] = ["$1", "$2", "$3"]

    def add(col: str, val: Any) -> None:
        if col in cols:
            insert_cols.append(col)
            params.append(val)
            values.append(f"${len(params)}")

    add("content_type", content_type)
    if duration_ms is not None:
        add("duration_ms", int(duration_ms))
    add("meta_json", meta if meta else {})

    try:
        await pool.execute(
            f"""
            insert into public.media_assets({", ".join(insert_cols)})
            values({", ".join(values)})
            on conflict (id) do nothing
            """,
            *params,
        )
        return asset_id
    except Exception:
        return None


async def _upload_music_output_video(
    *,
    storage: AzureStorageService,
    user_id: UUID,
    project_id: UUID,
    job_id: UUID,
    local_path: str | Path,
    blob_filename: str,
    content_type: str = "video/mp4",
) -> JsonDict:
    res = await storage.upload_music_output_file(
        user_id=str(user_id),
        project_id=str(project_id),
        job_id=str(job_id),
        local_path=local_path,
        content_type=content_type,
        blob_filename=str(blob_filename),
    )
    return {
        "sas_url": res.sas_url,
        "container": res.container,
        "storage_path": res.storage_path,
        "bytes": res.bytes,
        "sha256": res.sha256,
    }


# -----------------------------
# Public entrypoint
# -----------------------------
async def render_montage_and_upload(
    *,
    steps: StepsRepo,
    jobs: MusicJobsRepo,
    pool,
    job_id: UUID,
    user_id: UUID,
    project_id: UUID,
    input_json: JsonDict,
) -> Tuple[JsonDict, Optional[UUID], Optional[UUID]]:
    computed = _as_dict(input_json.get("computed"))
    manifest = _as_dict(computed.get("clip_manifest"))
    broll = _as_dict(computed.get("broll"))
    by_clip = broll.get("by_clip")
    if not isinstance(by_clip, dict) or not by_clip:
        raise RuntimeError("montage_missing_broll_by_clip")

    audio_url = pick_audio_url_for_probe(input_json)
    if not audio_url:
        raise RuntimeError("montage_missing_audio_url")

    performer_url = str(computed.get("performer_video_url") or "").strip() or None
    scene_no_face = bool(computed.get("scene_no_face"))

    download_parallel = _clamp_int(_get_int("MUSIC_MONTAGE_DOWNLOAD_PARALLEL", 8), 1, 32)
    render_parallel = _clamp_int(_get_int("MUSIC_MONTAGE_RENDER_PARALLEL", 3), 1, 12)
    enable_exports = _get_bool("MUSIC_MONTAGE_ENABLE_EXPORTS", True)
    enable_nvenc = _get_bool("MUSIC_MONTAGE_ENABLE_NVENC", True)

    nvenc_usable = _ffmpeg_nvenc_usable()
    nvenc_used = bool(enable_nvenc and nvenc_usable)

    # IMPORTANT: unique run id per montage render to avoid stale/older-run URLs
    run_id = uuid4().hex[:12]
    computed["montage_run_id"] = run_id
    input_json["computed"] = computed

    # Persist run_id early (best-effort) for debuggability.
    try:
        await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
        await persist_studio_payload_best_effort(job_id=job_id, payload_json=input_json)
    except Exception:
        pass

    try:
        await steps.upsert_step(
            job_id=job_id,
            step_code="render_montage",
            status="running",
            meta_json={
                "run_id": run_id,
                "audio_url": True,
                "download_parallel": download_parallel,
                "render_parallel": render_parallel,
                "enable_exports": enable_exports,
                "enable_nvenc_env": enable_nvenc,
                "nvenc_usable": nvenc_usable,
                "nvenc_used": nvenc_used,
                "has_performer_url": bool(performer_url),
                "scene_no_face": scene_no_face,
            },
        )
    except Exception:
        pass

    storage = AzureStorageService.for_output()

    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix=f"df_montage_{job_id}_") as td:
        out_dir = Path(td)

        paths, perf = await _render_montage_local_parallel(
            job_id=job_id,
            manifest=manifest,
            broll_by_clip=by_clip,
            audio_url=str(audio_url),
            out_dir=out_dir,
            performer_url=performer_url,
            scene_no_face=scene_no_face,
        )

        # upload tasks keyed by name (avoid list-order bugs)
        upload_tasks: Dict[str, asyncio.Task] = {
            "preview": asyncio.create_task(
                _upload_music_output_video(
                    storage=storage,
                    user_id=user_id,
                    project_id=project_id,
                    job_id=job_id,
                    local_path=str(paths["preview"]),
                    blob_filename=_montage_blob_filename(job_id=job_id, run_id=run_id, filename="preview_720p.mp4"),
                )
            ),
            "final": asyncio.create_task(
                _upload_music_output_video(
                    storage=storage,
                    user_id=user_id,
                    project_id=project_id,
                    job_id=job_id,
                    local_path=str(paths["final"]),
                    blob_filename=_montage_blob_filename(job_id=job_id, run_id=run_id, filename="final_16x9_1080p.mp4"),
                )
            ),
        }

        if "export_9x16" in paths:
            upload_tasks["export_9x16"] = asyncio.create_task(
                _upload_music_output_video(
                    storage=storage,
                    user_id=user_id,
                    project_id=project_id,
                    job_id=job_id,
                    local_path=str(paths["export_9x16"]),
                    blob_filename=_montage_blob_filename(job_id=job_id, run_id=run_id, filename="export_9x16_1080x1920.mp4"),
                )
            )

        if "export_1x1" in paths:
            upload_tasks["export_1x1"] = asyncio.create_task(
                _upload_music_output_video(
                    storage=storage,
                    user_id=user_id,
                    project_id=project_id,
                    job_id=job_id,
                    local_path=str(paths["export_1x1"]),
                    blob_filename=_montage_blob_filename(job_id=job_id, run_id=run_id, filename="export_1x1_1080x1080.mp4"),
                )
            )

        results = await asyncio.gather(*upload_tasks.values(), return_exceptions=True)

        by_name: Dict[str, Any] = {}
        for name, res in zip(upload_tasks.keys(), results):
            by_name[name] = res

        up_preview = by_name.get("preview")
        up_final = by_name.get("final")
        up_9x16 = by_name.get("export_9x16")
        up_1x1 = by_name.get("export_1x1")

        if not isinstance(up_preview, dict) or not isinstance(up_final, dict):
            raise RuntimeError("montage_upload_failed_preview_or_final")

    preview_url = str(up_preview["sas_url"])
    final_url = str(up_final["sas_url"])

    exports_list: List[JsonDict] = [
        {
            "name": "16:9",
            "w": 1920,
            "h": 1080,
            "url": final_url,
            "container": up_final["container"],
            "storage_path": up_final["storage_path"],
        }
    ]
    if isinstance(up_9x16, dict):
        exports_list.append(
            {
                "name": "9:16",
                "w": 1080,
                "h": 1920,
                "url": str(up_9x16["sas_url"]),
                "container": up_9x16["container"],
                "storage_path": up_9x16["storage_path"],
            }
        )
    if isinstance(up_1x1, dict):
        exports_list.append(
            {
                "name": "1:1",
                "w": 1080,
                "h": 1080,
                "url": str(up_1x1["sas_url"]),
                "container": up_1x1["container"],
                "storage_path": up_1x1["storage_path"],
            }
        )

    out = {
        "run_id": run_id,
        "preview_url": preview_url,
        "final_url": final_url,
        "exports": exports_list,
        "source": "ffmpeg_montage_v3_parallel_fitpad_plus_performer_intercut",
        "perf": {
            **perf,
            "enable_exports": _get_bool("MUSIC_MONTAGE_ENABLE_EXPORTS", True),
            "enable_nvenc_env": _get_bool("MUSIC_MONTAGE_ENABLE_NVENC", True),
            "elapsed_s": round(time.time() - t0, 3),
        },
    }

    computed = _as_dict(input_json.get("computed"))
    computed["video_outputs"] = out
    computed["preview_video_url"] = preview_url
    computed["final_video_url"] = final_url
    computed["montage_performer_used"] = bool(_as_dict(out.get("perf")).get("performer_used"))

    # UI-facing canonical fields (avoid clients accidentally showing old performer_url)
    computed["display_video_url"] = final_url
    computed["display_video_source"] = "montage"

    input_json["computed"] = computed
    await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
    await persist_studio_payload_best_effort(job_id=job_id, payload_json=input_json)

    # duration
    dur_ms = None
    ap = _as_dict(computed.get("audio_probe"))
    if isinstance(ap.get("duration_ms"), (int, float)):
        try:
            dur_ms = int(float(ap["duration_ms"]))
        except Exception:
            dur_ms = None

    preview_asset_id = await _create_media_asset_best_effort(
        pool=pool,
        user_id=user_id,
        storage_ref=str(preview_url),
        content_type="video/mp4",
        duration_ms=dur_ms,
        meta_json={
            "kind": "music_video_preview",
            "job_id": str(job_id),
            "montage_run_id": run_id,
            "container": up_preview["container"],
            "storage_path": up_preview["storage_path"],
        },
    )
    final_asset_id = await _create_media_asset_best_effort(
        pool=pool,
        user_id=user_id,
        storage_ref=str(final_url),
        content_type="video/mp4",
        duration_ms=dur_ms,
        meta_json={
            "kind": "music_video_final",
            "job_id": str(job_id),
            "montage_run_id": run_id,
            "container": up_final["container"],
            "storage_path": up_final["storage_path"],
        },
    )

    try:
        await steps.upsert_step(
            job_id=job_id,
            step_code="render_montage",
            status="succeeded",
            meta_json={
                "run_id": run_id,
                "has_preview": bool(preview_url),
                "has_final": bool(final_url),
                "exports": len(exports_list),
                "elapsed_s": round(time.time() - t0, 3),
                "performer_used": bool(_as_dict(out.get("perf")).get("performer_used")),
            },
        )
    except Exception:
        pass

    return out, preview_asset_id, final_asset_id