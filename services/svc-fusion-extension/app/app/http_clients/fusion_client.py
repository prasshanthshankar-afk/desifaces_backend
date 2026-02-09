from __future__ import annotations

from typing import Any, Dict, Optional
from uuid import UUID

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings


def _pick_video_url(job_view: Dict[str, Any]) -> Optional[str]:
    artifacts = job_view.get("artifacts") or []
    if not artifacts:
        return None

    for a in artifacts:
        if not isinstance(a, dict):
            continue
        if (a.get("kind") or "").lower() == "video" and a.get("url"):
            return a["url"]

    for a in artifacts:
        if not isinstance(a, dict):
            continue
        ct = (a.get("content_type") or "").lower()
        if ct.startswith("video/") and a.get("url"):
            return a["url"]

    for a in artifacts:
        if not isinstance(a, dict):
            continue
        url = a.get("url")
        if isinstance(url, str) and url.lower().endswith(".mp4"):
            return url

    for a in artifacts:
        if isinstance(a, dict) and a.get("url"):
            return a["url"]

    return None


def _normalize_auth_value(token_or_header: str) -> str:
    """
    Accept raw token OR full Authorization header value and return full 'Bearer <...>'.
    """
    t = (token_or_header or "").strip()
    if not t:
        return ""
    if t.lower().startswith("bearer "):
        return t
    return f"Bearer {t}"


def _is_uuid(v: str) -> bool:
    try:
        UUID(str(v))
        return True
    except Exception:
        return False


def _auth_headers(
    token_or_header: str,
    *,
    actor_user_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Dict[str, str]:
    """
    token_or_header can be:
      - user JWT (raw), OR 'Bearer <jwt>'
      - service secret (raw), OR 'Bearer <secret>'

    For svc-to-svc calls, svc-fusion uses X-Actor-User-Id for attribution/audit.
    We ONLY send X-Actor-User-Id if it's a valid UUID string.
    """
    auth = _normalize_auth_value(token_or_header)
    if not auth:
        return {}

    headers: Dict[str, str] = {"Authorization": auth}

    if actor_user_id and _is_uuid(actor_user_id):
        headers["X-Actor-User-Id"] = str(actor_user_id)

    # Optional but useful for auditing
    headers["X-Actor-Source"] = "svc-fusion-extension"

    # Optional: prevent duplicates if retries happen (server must support it; harmless if ignored)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    return headers


def _client(timeout_s: float) -> httpx.AsyncClient:
    # Safer defaults under load
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
    timeout = httpx.Timeout(timeout_s, connect=10.0)
    return httpx.AsyncClient(timeout=timeout, limits=limits)


def _raise_for_status_with_body(r: httpx.Response) -> None:
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = r.text
        except Exception:
            body = "<unreadable body>"
        raise httpx.HTTPStatusError(
            f"{e}. Response body: {body[:4000]}",
            request=e.request,
            response=e.response,
        ) from None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def create_fusion_job(
    token_or_header: str,
    *,
    actor_user_id: Optional[str] = None,
    face_artifact_id: Optional[str] = None,
    face_image_url: Optional[str] = None,
    audio_url: Optional[str] = None,
    audio_artifact_id: Optional[str] = None,
    aspect_ratio: str = "9:16",
    duration_sec: Optional[int] = None,
    provider: str = "heygen_av4",
    consent_external_provider_ok: bool = True,
    tags: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Calls svc-fusion POST /jobs (FusionJobCreate).
    """
    url = settings.SVC_FUSION_BASE_URL.rstrip("/") + settings.SVC_FUSION_CREATE_PATH  # /jobs

    if not (face_artifact_id or face_image_url):
        raise ValueError("Either face_artifact_id or face_image_url is required for svc-fusion")
    if not (audio_url or audio_artifact_id):
        raise ValueError("Either audio_url or audio_artifact_id is required for svc-fusion (voice_audio)")

    voice_audio: Dict[str, Any] = {"type": "audio"}
    if audio_url:
        voice_audio["audio_url"] = audio_url
    elif audio_artifact_id:
        voice_audio["audio_artifact_id"] = audio_artifact_id

    payload: Dict[str, Any] = {
        "provider": provider,
        "consent": {"external_provider_ok": bool(consent_external_provider_ok)},
        "tags": tags or {},
        "video": {"aspect_ratio": aspect_ratio},
        "voice_mode": "audio",
        "voice_audio": voice_audio,
    }

    if face_artifact_id:
        payload["face_artifact_id"] = face_artifact_id
    if face_image_url:
        payload["face_image_url"] = face_image_url
    if duration_sec is not None:
        payload["video"]["duration_sec"] = int(duration_sec)

    headers = _auth_headers(token_or_header, actor_user_id=actor_user_id, idempotency_key=idempotency_key)

    async with _client(timeout_s=90.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        _raise_for_status_with_body(r)
        return r.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def get_fusion_job(
    token_or_header: str,
    job_id: str,
    *,
    actor_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Calls svc-fusion GET /jobs/{job_id} -> FusionJobView
    """
    url = settings.SVC_FUSION_BASE_URL.rstrip("/") + settings.SVC_FUSION_STATUS_PATH.format(job_id=job_id)
    headers = _auth_headers(token_or_header, actor_user_id=actor_user_id)

    async with _client(timeout_s=45.0) as client:
        r = await client.get(url, headers=headers)
        _raise_for_status_with_body(r)
        return r.json()


async def get_fusion_video_url_if_done(
    token_or_header: str,
    job_id: str,
    *,
    actor_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Returns:
      { status, video_url?, error_message?, raw }
    """
    st = await get_fusion_job(token_or_header, job_id, actor_user_id=actor_user_id)
    status = (st.get("status") or "").lower()

    if status in ("succeeded", "success", "done"):
        video_url = _pick_video_url(st)
        return {"status": "succeeded", "video_url": video_url, "raw": st}

    if status in ("failed", "error"):
        return {"status": "failed", "error_message": st.get("error_message") or str(st), "raw": st}

    return {"status": status, "raw": st}