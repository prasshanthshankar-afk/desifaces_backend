from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Tuple

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings


def _pick_audio_variant(status_json: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
    """
    svc-audio JobStatusResponse contains:
      variants: [{ audio_url, artifact_id, content_type, bytes }, ...]
    """
    variants = status_json.get("variants") or []
    if not variants or not isinstance(variants[0], dict):
        return None, None, None, None

    v0 = variants[0]
    return (
        v0.get("audio_url"),
        v0.get("artifact_id"),
        v0.get("content_type"),
        v0.get("bytes"),
    )


def _looks_like_audio_url(url: str) -> bool:
    u = (url or "").lower()
    return u.startswith("http") and (".mp3" in u or ".wav" in u or "audio-output" in u)


def _normalize_auth_value(token_or_header: str) -> str:
    """
    Accept raw token OR full Authorization header value and return full 'Bearer <...>' string.
    """
    t = (token_or_header or "").strip()
    if not t:
        return ""
    if t.lower().startswith("bearer "):
        return t
    return f"Bearer {t}"


def _auth_headers(token_or_header: str, *, actor_user_id: Optional[str] = None) -> Dict[str, str]:
    """
    token_or_header can be:
      - raw user JWT (normal user flow), OR
      - raw service secret (Option A), OR
      - full 'Bearer <...>' value

    For service calls that write to DB, svc-audio requires X-Actor-User-Id (UUID).
    """
    auth = _normalize_auth_value(token_or_header)
    if not auth:
        return {}

    headers: Dict[str, str] = {"Authorization": auth}

    if actor_user_id:
        headers["X-Actor-User-Id"] = str(actor_user_id)

    # Optional but useful for auditing
    headers["X-Actor-Source"] = "svc-fusion-extension"
    return headers


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def create_tts_job(
    token_or_header: str,
    text: str,
    voice_cfg: Dict[str, Any],
    *,
    actor_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Calls svc-audio POST /api/audio/tts
    Request: TTSCreateRequest
    Response: JobCreatedResponse { job_id, status }
    """
    url = settings.SVC_AUDIO_BASE_URL.rstrip("/") + settings.SVC_AUDIO_TTS_PATH  # /api/audio/tts

    payload: Dict[str, Any] = {
        "text": text,
        "target_locale": voice_cfg.get("locale") or voice_cfg.get("target_locale") or "en-US",
        "translate": bool(voice_cfg.get("translate", False)),
        "voice": voice_cfg.get("voice_id") or voice_cfg.get("voice"),
        "style": voice_cfg.get("style"),
        "style_degree": voice_cfg.get("style_degree"),
        "rate": voice_cfg.get("speaking_rate") or voice_cfg.get("rate"),
        "pitch": voice_cfg.get("pitch"),
        "volume": voice_cfg.get("volume"),
        "context": voice_cfg.get("context"),
        "output_format": voice_cfg.get("output_format", "mp3"),
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    headers = _auth_headers(token_or_header, actor_user_id=actor_user_id)

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def get_tts_status(
    token_or_header: str,
    job_id: str,
    *,
    actor_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    url = settings.SVC_AUDIO_BASE_URL.rstrip("/") + settings.SVC_AUDIO_STATUS_PATH.format(job_id=job_id)
    headers = _auth_headers(token_or_header, actor_user_id=actor_user_id)

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        return r.json()


async def create_tts_audio_blocking(
    token_or_header: str,
    text: str,
    voice_cfg: Dict[str, Any],
    *,
    actor_user_id: Optional[str] = None,
    poll_seconds: float = 1.5,
    timeout_seconds: int = 240,
) -> Dict[str, Any]:
    """
    Convenience wrapper for longform: create job + poll until succeeded/failed.
    """
    created = await create_tts_job(token_or_header, text, voice_cfg, actor_user_id=actor_user_id)
    job_id = created["job_id"]

    loop = asyncio.get_running_loop()
    deadline = loop.time() + float(timeout_seconds)

    while True:
        st = await get_tts_status(token_or_header, job_id, actor_user_id=actor_user_id)
        status = (st.get("status") or "").lower()

        if status in ("succeeded", "success", "done"):
            audio_url, audio_artifact_id, content_type, nbytes = _pick_audio_variant(st)
            if not audio_url:
                raise RuntimeError(f"svc-audio job {job_id} succeeded but variants[0].audio_url missing")

            if not _looks_like_audio_url(audio_url):
                raise RuntimeError(f"svc-audio job {job_id} returned unexpected audio_url: {audio_url}")

            return {
                "job_id": job_id,
                "status": "succeeded",
                "audio_url": audio_url,
                "audio_artifact_id": audio_artifact_id,
                "content_type": content_type,
                "bytes": nbytes,
                "raw": st,
            }

        if status in ("failed", "error"):
            raise RuntimeError(f"svc-audio job {job_id} failed: {st.get('error_message') or st}")

        if loop.time() > deadline:
            raise TimeoutError(f"svc-audio job {job_id} timed out")

        await asyncio.sleep(poll_seconds)