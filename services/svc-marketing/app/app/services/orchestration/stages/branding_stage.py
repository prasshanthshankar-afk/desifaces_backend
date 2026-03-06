# services/svc-marketing/app/app/services/orchestration/stages/branding_stage.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, Optional, Tuple, List
from uuid import UUID

import httpx

from app.config import settings
from app.domain.enums import AssetKind
from app.repos.marketing_assets_repo import MarketingAssetsRepo
from app.services.storage.blob_uploader import BlobUploader
from app.services.orchestration.errors import MarketingRunFailed
from app.services.orchestration.utils.config import cfg_bool, cfg_float, cfg_int, cfg_str
from app.services.orchestration.utils.azure_sas import maybe_add_azure_read_sas

logger = logging.getLogger("svc-marketing-branding")


def _asset_kind(name: str, fallback: str) -> str:
    e = getattr(AssetKind, name, None)
    try:
        return e.value if e is not None else fallback
    except Exception:
        return fallback


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


async def _download_url_to_file(url: str, out_path: str, timeout_s: int = 120) -> None:
    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                async for chunk in r.aiter_bytes():
                    if chunk:
                        f.write(chunk)


def _run_cmd(cmd: List[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"command failed rc={p.returncode}\ncmd={' '.join(cmd)}\nstdout={p.stdout}\nstderr={p.stderr}"
        )


def _ffprobe_video_wh_fps(path: str) -> Tuple[int, int, float]:
    if not _which("ffprobe"):
        raise RuntimeError("ffprobe not found (install ffmpeg/ffprobe in svc-marketing image)")
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate",
        "-of",
        "json",
        path,
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {p.stderr}")

    data = json.loads(p.stdout or "{}")
    s = (data.get("streams") or [{}])[0]
    w = int(s.get("width") or 0)
    h = int(s.get("height") or 0)
    rr = str(s.get("r_frame_rate") or "30/1")
    try:
        num, den = rr.split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    except Exception:
        fps = 30.0

    if w <= 0 or h <= 0:
        raise RuntimeError(f"ffprobe returned invalid size w={w} h={h}")
    return w, h, fps


def _ffprobe_has_audio(path: str) -> bool:
    if not _which("ffprobe"):
        return False
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "json", path]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        return False
    try:
        data = json.loads(p.stdout or "{}")
        return bool(data.get("streams"))
    except Exception:
        return False


def _ensure_audio_stream(in_mp4: str, out_mp4: str) -> str:
    if _ffprobe_has_audio(in_mp4):
        if in_mp4 != out_mp4:
            shutil.copyfile(in_mp4, out_mp4)
        return out_mp4

    _run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            in_mp4,
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-shortest",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            out_mp4,
        ]
    )
    return out_mp4


def _render_endcard_mp4(
    *,
    logo_path: str,
    brand_text: str,
    w: int,
    h: int,
    fps: float,
    seconds: float,
    out_mp4: str,
    work_dir: str,
) -> str:
    os.makedirs(work_dir, exist_ok=True)

    textfile = os.path.join(work_dir, "brand_text.txt")
    with open(textfile, "w", encoding="utf-8") as f:
        f.write((brand_text or "desifaces.ai").strip())

    logo_w = max(160, int(w * 0.22))
    font_size = max(28, int(h * 0.045))
    logo_y = int(h * 0.18)
    text_y = int(h * 0.78)

    _run_cmd(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-t",
            f"{seconds}",
            "-i",
            f"color=c=black:s={w}x{h}:r={fps}",
            "-loop",
            "1",
            "-t",
            f"{seconds}",
            "-i",
            logo_path,
            "-f",
            "lavfi",
            "-t",
            f"{seconds}",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex",
            (
                f"[1:v]scale={logo_w}:-1[lg];"
                f"[0:v][lg]overlay=x=(W-w)/2:y={logo_y}:format=auto,"
                f"drawtext=textfile='{textfile}':x=(w-text_w)/2:y={text_y}:fontsize={font_size}:fontcolor=white"
                f"[v]"
            ),
            "-map",
            "[v]",
            "-map",
            "2:a",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            out_mp4,
        ]
    )
    return out_mp4


def _brand_mp4_with_endcard(
    *,
    in_mp4: str,
    logo_path: str,
    brand_text: str,
    seconds: float,
    out_mp4: str,
    work_dir: str,
) -> str:
    if not _which("ffmpeg"):
        raise RuntimeError("ffmpeg not found (install ffmpeg in svc-marketing image)")

    seconds = float(seconds or 0.0)
    if seconds <= 0.0:
        seconds = 2.0
    seconds = max(0.5, min(6.0, seconds))

    w, h, fps = _ffprobe_video_wh_fps(in_mp4)

    normalized = os.path.join(work_dir, "input_with_audio.mp4")
    _ensure_audio_stream(in_mp4, normalized)

    endcard = os.path.join(work_dir, "endcard.mp4")
    _render_endcard_mp4(
        logo_path=logo_path,
        brand_text=brand_text,
        w=w,
        h=h,
        fps=fps,
        seconds=seconds,
        out_mp4=endcard,
        work_dir=work_dir,
    )

    _run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            normalized,
            "-i",
            endcard,
            "-filter_complex",
            "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            out_mp4,
        ]
    )
    return out_mp4


def _pick_reel_url(reel_url: Optional[str], kwargs: Dict[str, Any]) -> str:
    """
    Many upstream stages name the video URL differently. We normalize here.
    """
    if isinstance(reel_url, str) and reel_url:
        return reel_url

    # common direct keys
    for k in (
        "reel_url",
        "reel_mp4_url",
        "reel",
        "video_url",
        "mp4_url",
        "final_url",
        "output_url",
        "url",
    ):
        v = kwargs.get(k)
        if isinstance(v, str) and v:
            return v

    # sometimes a dict is passed (e.g. stage output)
    out = kwargs.get("output")
    if isinstance(out, dict):
        for k in (
            "reel_url",
            "reel_mp4_url",
            "video_url",
            "final_url",
            "output_url",
            "url",
        ):
            v = out.get(k)
            if isinstance(v, str) and v:
                return v

    return ""


class BrandingStage:
    def __init__(self, assets: MarketingAssetsRepo, uploader: BlobUploader):
        self.assets = assets
        self.uploader = uploader

    async def run(
        self,
        *,
        run_id: UUID,
        reel_url: Optional[str] = None,
        fmt: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        enable = cfg_bool("MARKETING_BRAND_ENABLE", False)
        strict = cfg_bool("MARKETING_BRAND_STRICT", False)

        # normalize reel_url from kwargs if caller didn't pass it
        reel_url_norm = _pick_reel_url(reel_url, kwargs)

        if not enable:
            return reel_url_norm or (reel_url or "")

        if not (reel_url_norm and reel_url_norm.startswith("http")):
            msg = "branding skipped: missing reel_url (caller did not pass reel_url and no fallback keys found)"
            if strict:
                raise MarketingRunFailed("BRANDING_MISSING_REEL_URL", msg)
            logger.warning("run=%s %s keys=%s", str(run_id), msg, sorted(list(kwargs.keys()))[:30])
            return reel_url_norm or (reel_url or "")

        # accept fmt but only mp4 supported
        if fmt:
            f = str(fmt).strip().lower().lstrip(".")
            if f and f != "mp4":
                logger.warning("run=%s branding fmt=%s not supported; using mp4", str(run_id), f)

        logo_url = cfg_str(
            "MARKETING_BRAND_LOGO_URL",
            "https://desifacesstore.blob.core.windows.net/desifaces-media/logo/desifaces-logo.png",
        ).strip()
        brand_text = cfg_str("MARKETING_BRAND_TEXT", "desifaces.ai").strip()
        seconds = float(cfg_float("MARKETING_BRAND_ENDCARD_SECONDS", 2.0))

        if not _which("ffmpeg") or not _which("ffprobe"):
            msg = "ffmpeg/ffprobe not found in svc-marketing image"
            if strict:
                raise MarketingRunFailed("BRANDING_FFMPEG_MISSING", msg)
            logger.warning("run=%s branding skipped: %s", str(run_id), msg)
            return reel_url_norm

        work_dir = os.path.join(settings.OUTPUT_DIR, str(run_id), "brand_tmp")
        os.makedirs(work_dir, exist_ok=True)

        in_mp4 = os.path.join(work_dir, "input.mp4")
        logo_path = os.path.join(work_dir, "logo.png")
        out_mp4 = os.path.join(work_dir, "reel_branded.mp4")

        sas_hours = cfg_int("MARKETING_BLOB_SAS_HOURS", 24)

        if logo_url:
            logo_url = maybe_add_azure_read_sas(logo_url, expiry_hours=sas_hours)

        try:
            await _download_url_to_file(reel_url_norm, in_mp4, timeout_s=cfg_int("MARKETING_BRAND_DL_TIMEOUT_S", 240))
        except Exception as e:
            msg = f"Failed to download reel mp4: {e}"
            if strict:
                raise MarketingRunFailed("BRANDING_REEL_FETCH_FAILED", msg)
            logger.warning("run=%s branding skipped: %s", str(run_id), msg)
            return reel_url_norm

        try:
            await _download_url_to_file(logo_url, logo_path, timeout_s=cfg_int("MARKETING_BRAND_LOGO_DL_TIMEOUT_S", 120))
        except Exception as e:
            msg = f"Failed to download brand logo url={logo_url}: {e}"
            if strict:
                raise MarketingRunFailed("BRANDING_LOGO_FETCH_FAILED", msg)
            logger.warning("run=%s branding skipped: %s", str(run_id), msg)
            return reel_url_norm

        try:
            _brand_mp4_with_endcard(
                in_mp4=in_mp4,
                logo_path=logo_path,
                brand_text=brand_text,
                seconds=seconds,
                out_mp4=out_mp4,
                work_dir=work_dir,
            )
        except Exception as e:
            msg = f"Failed to append endcard: {e}"
            if strict:
                raise MarketingRunFailed("BRANDING_RENDER_FAILED", msg)
            logger.warning("run=%s branding skipped: %s", str(run_id), msg)
            return reel_url_norm

        try:
            up = await asyncio.to_thread(
                self.uploader.upload_file,
                out_mp4,
                f"{run_id}/reel_branded.mp4",
                "video/mp4",
            )
        except Exception as e:
            msg = f"Upload branded mp4 failed: {e}"
            if strict:
                raise MarketingRunFailed("BRANDING_UPLOAD_FAILED", msg)
            logger.warning("run=%s branding skipped: %s", str(run_id), msg)
            return reel_url_norm

        branded_url = getattr(up, "url", None) if up is not None else None
        if isinstance(up, dict) and not branded_url:
            branded_url = up.get("url")

        try:
            await self.assets.add_asset(
                run_id,
                _asset_kind("reel_branded_mp4", "reel_branded_mp4"),
                str(branded_url or ""),
                "video/mp4",
                None,
                None,
                None,
                {"source": "svc-marketing", "kind": "endcard_brand"},
            )
        except Exception:
            pass

        return maybe_add_azure_read_sas(str(branded_url or ""), expiry_hours=sas_hours) or reel_url_norm