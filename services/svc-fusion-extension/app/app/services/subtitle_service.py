from __future__ import annotations

import html
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from azure.storage.blob import BlobServiceClient, ContentSettings

from app.config import settings
from app.services.sas_service import AzureBlobService

logger = logging.getLogger("svc_fusion_extension.subtitle_service")


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _format_vtt_time(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000.0)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _normalize_caption_text(text: str) -> str:
    value = re.sub(r"\s+", " ", _safe_text(text))
    return html.unescape(value).strip()


def _caption_text_from_row(row: Dict[str, Any]) -> str:
    script = row.get("script") if isinstance(row.get("script"), dict) else {}
    for value in (
        row.get("subtitle_text"),
        row.get("text_chunk"),
        row.get("script_text"),
        script.get("subtitle_text"),
        script.get("spoken_text"),
        script.get("voiceover_text"),
    ):
        text = _normalize_caption_text(_safe_text(value))
        if text:
            speaker = _safe_text(
                row.get("speaker_name")
                or row.get("speaker")
                or row.get("character_name")
                or script.get("speaker_name")
            )
            return f"{speaker}: {text}" if speaker else text
    return ""


def build_webvtt(
    rows: Sequence[Dict[str, Any]],
    *,
    stitch_mode: str = "concat",
    transition_seconds: float = 0.5,
) -> str:
    """
    Build a deterministic WebVTT track from the already-approved longform segment
    lineage. This intentionally uses the known script instead of retranscribing
    generated audio.

    For concat, cue boundaries are cumulative segment durations.
    For xfade, each next segment starts earlier by the transition overlap.
    """
    mode = _safe_text(stitch_mode).lower() or "concat"
    overlap = max(0.0, _safe_float(transition_seconds, 0.5)) if mode == "xfade" else 0.0

    cues: List[str] = ["WEBVTT", ""]
    cursor = 0.0
    cue_index = 1

    for raw in rows:
        row = dict(raw or {})
        text = _caption_text_from_row(row)
        duration = max(0.05, _safe_float(row.get("duration_sec"), 0.0))

        if not text:
            cursor += max(0.0, duration - overlap)
            continue

        start = cursor
        end = max(start + 0.05, start + duration)
        cues.extend(
            [
                str(cue_index),
                f"{_format_vtt_time(start)} --> {_format_vtt_time(end)}",
                text,
                "",
            ]
        )
        cue_index += 1
        cursor += max(0.0, duration - overlap)

    return "\n".join(cues).rstrip() + "\n"


def write_webvtt(
    rows: Sequence[Dict[str, Any]],
    output_path: str,
    *,
    stitch_mode: str = "concat",
    transition_seconds: float = 0.5,
) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = build_webvtt(
        rows,
        stitch_mode=stitch_mode,
        transition_seconds=transition_seconds,
    )
    path.write_text(body, encoding="utf-8")
    if path.stat().st_size <= 0:
        raise RuntimeError("subtitle track is empty")
    return str(path)


def upload_webvtt(
    local_path: str,
    *,
    storage_path: Optional[str] = None,
) -> Tuple[str, str]:
    path = Path(local_path)
    if not path.exists() or path.stat().st_size <= 0:
        raise RuntimeError(f"subtitle track missing or empty: {local_path}")

    blob_service = BlobServiceClient.from_connection_string(
        settings.AZURE_STORAGE_CONNECTION_STRING
    )
    container_name = settings.AZURE_VIDEO_OUTPUT_CONTAINER
    container = blob_service.get_container_client(container_name)

    prefix = getattr(settings, "AZURE_VIDEO_OUTPUT_PREFIX", None) or "longform"
    storage_path = storage_path or f"{prefix.rstrip('/')}/{uuid.uuid4()}.vtt"

    blob = container.get_blob_client(storage_path)
    with open(local_path, "rb") as handle:
        blob.upload_blob(
            handle,
            overwrite=True,
            content_settings=ContentSettings(
                content_type="text/vtt; charset=utf-8",
                content_disposition="inline",
            ),
        )

    sas = AzureBlobService(settings.AZURE_STORAGE_CONNECTION_STRING)
    signed_url = sas.sign_read_url(
        container_name,
        storage_path,
        getattr(settings, "FINAL_SAS_TTL_SECONDS", 86400),
    )
    logger.info("subtitle track uploaded storage_path=%s", storage_path)
    return storage_path, signed_url
