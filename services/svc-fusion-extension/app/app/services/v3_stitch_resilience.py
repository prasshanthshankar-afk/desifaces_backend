from __future__ import annotations

import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

from app.services.stitch_service import download_to_local, stitch_videos

logger = logging.getLogger("svc_fusion_extension.v3_stitch_resilience")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name, str(default))).strip())
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(str(os.getenv(name, str(default))).strip())
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _download_timeout_seconds() -> int:
    # The original generic stitch helper used 120 seconds. Provider video downloads
    # can legitimately take longer for a 28-turn scene, especially while the remote
    # object store is serving several MP4s concurrently.
    return _env_int(
        "V3_SCENE_STITCH_DOWNLOAD_TIMEOUT_SECONDS",
        300,
        minimum=30,
        maximum=900,
    )


def _download_attempts() -> int:
    return _env_int(
        "V3_SCENE_STITCH_DOWNLOAD_ATTEMPTS",
        3,
        minimum=1,
        maximum=5,
    )


def _download_concurrency(segment_count: int) -> int:
    configured = _env_int(
        "V3_SCENE_STITCH_DOWNLOAD_CONCURRENCY",
        8,
        minimum=1,
        maximum=16,
    )
    return max(1, min(segment_count, configured))


def _retry_backoff_seconds() -> float:
    return _env_float(
        "V3_SCENE_STITCH_DOWNLOAD_RETRY_BACKOFF_SECONDS",
        2.0,
        minimum=0.0,
        maximum=15.0,
    )


def _url_host(url: str) -> str:
    try:
        return str(urlsplit(url).hostname or "unknown-host")[:200]
    except Exception:
        return "unknown-host"


def _unlink_partial(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        logger.exception("unable to remove partial V3 stitch segment path=%s", path)


def _download_segment(index: int, url: str, output_path: str) -> str:
    timeout_seconds = _download_timeout_seconds()
    attempts = _download_attempts()
    backoff = _retry_backoff_seconds()
    host = _url_host(url)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        _unlink_partial(output_path)
        started = time.monotonic()
        try:
            result = download_to_local(
                url,
                output_path,
                timeout_seconds=timeout_seconds,
            )
            logger.info(
                "v3_stitch_segment_download_ok index=%s host=%s attempt=%s elapsed_ms=%s bytes=%s",
                index,
                host,
                attempt,
                int((time.monotonic() - started) * 1000),
                Path(result).stat().st_size,
            )
            return result
        except Exception as exc:
            last_error = exc
            _unlink_partial(output_path)
            logger.warning(
                "v3_stitch_segment_download_failed index=%s host=%s attempt=%s/%s elapsed_ms=%s error_type=%s error=%s",
                index,
                host,
                attempt,
                attempts,
                int((time.monotonic() - started) * 1000),
                type(exc).__name__,
                str(exc)[:500],
            )
            if attempt < attempts and backoff > 0:
                time.sleep(backoff * attempt)

    raise RuntimeError(
        "v3_scene_segment_download_exhausted:"
        f"index={index}:host={host}:attempts={attempts}:"
        f"error_type={type(last_error).__name__ if last_error else 'unknown'}:"
        f"error={str(last_error)[:500] if last_error else 'unknown'}"
    ) from last_error


def resilient_stitch_video_urls(segment_urls: Sequence[str], out_mp4: str) -> None:
    """Download ordered provider segments robustly, then run canonical stitching.

    This function is intentionally V3-specific. It preserves the canonical FFmpeg
    stitch implementation while replacing the fragile one-shot provider download
    step with bounded per-segment retries. A successful segment is kept in the local
    stitch workspace when a different segment needs a retry, so one transient remote
    read timeout does not force all 28 videos to be downloaded again.
    """

    urls = [str(value or "").strip() for value in segment_urls if str(value or "").strip()]
    if not urls:
        raise ValueError("segment_urls must not be empty")
    if len(urls) != len(segment_urls):
        raise ValueError("segment_urls must not contain empty entries")

    workers = _download_concurrency(len(urls))
    started = time.monotonic()
    logger.info(
        "v3_stitch_download_batch_start segments=%s workers=%s timeout_seconds=%s attempts=%s",
        len(urls),
        workers,
        _download_timeout_seconds(),
        _download_attempts(),
    )

    with tempfile.TemporaryDirectory(prefix="df_v3_scene_stitch_dl_") as td:
        local_files = [
            os.path.join(td, f"segment_{index:04d}.mp4")
            for index in range(len(urls))
        ]

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_download_segment, index, url, local_files[index]): index
                for index, url in enumerate(urls)
            }
            for future in as_completed(futures):
                # Surface the first exhausted segment immediately. The executor
                # context waits for already-running downloads to finish safely.
                future.result()

        for path in local_files:
            if not Path(path).is_file() or Path(path).stat().st_size <= 0:
                raise RuntimeError(f"v3_scene_segment_missing_after_download:{Path(path).name}")

        download_elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "v3_stitch_download_batch_ok segments=%s elapsed_ms=%s",
            len(local_files),
            download_elapsed_ms,
        )

        stitch_started = time.monotonic()
        stitch_videos(local_files, out_mp4)
        logger.info(
            "v3_stitch_ffmpeg_ok segments=%s stitch_ms=%s total_ms=%s",
            len(local_files),
            int((time.monotonic() - stitch_started) * 1000),
            int((time.monotonic() - started) * 1000),
        )


__all__ = ["resilient_stitch_video_urls"]
