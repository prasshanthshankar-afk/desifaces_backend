
from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings


def _pick_video_url(job_view: Dict[str, Any]) -> Optional[str]:
    direct = job_view.get("primary_video_url")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    light = job_view.get("light_status") or {}
    if isinstance(light, dict):
        direct = light.get("primary_video_url")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

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
    auth = _normalize_auth_value(token_or_header)
    if not auth:
        return {}

    headers: Dict[str, str] = {"Authorization": auth}

    if actor_user_id and _is_uuid(actor_user_id):
        headers["X-Actor-User-Id"] = str(actor_user_id)

    headers["X-Actor-Source"] = "svc-fusion-extension"

    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    return headers


def _client(timeout_s: float) -> httpx.AsyncClient:
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
    provider: str = "omnihuman_v15",
    provider_options: Optional[Dict[str, Any]] = None,
    reference_image_urls: Optional[List[str]] = None,
    reference_image_artifact_ids: Optional[List[str]] = None,
    consent_external_provider_ok: bool = True,
    tags: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Calls svc-fusion POST /jobs (FusionJobCreate).
    Supports silent non-heygen motion providers via provider_options/reference images.
    """
    url = settings.SVC_FUSION_BASE_URL.rstrip("/") + settings.SVC_FUSION_CREATE_PATH

    if not (face_artifact_id or face_image_url or reference_image_urls or reference_image_artifact_ids):
        raise ValueError("Either face selector or reference image(s) are required for svc-fusion")

    provider_name = str(provider or "omnihuman_v15").strip().lower() or "omnihuman_v15"
    provider_options = dict(provider_options or {})
    reference_image_urls = [str(x).strip() for x in (reference_image_urls or []) if str(x or "").strip()]
    reference_image_artifact_ids = [str(x).strip() for x in (reference_image_artifact_ids or []) if str(x or "").strip()]

    voice_audio: Optional[Dict[str, Any]] = None
    if audio_url or audio_artifact_id:
        voice_audio = {"type": "audio"}
        if audio_url:
            voice_audio["audio_url"] = audio_url
        elif audio_artifact_id:
            voice_audio["audio_artifact_id"] = audio_artifact_id

    if provider_name in {"heygen_av4", "omnihuman_v15", "omnihuman"} and not voice_audio:
        raise ValueError(f"{provider_name} requires audio_url or audio_artifact_id")

    payload: Dict[str, Any] = {
        "provider": provider_name,
        "consent": {"external_provider_ok": bool(consent_external_provider_ok)},
        "tags": tags or {},
        "video": {"aspect_ratio": aspect_ratio},
        "provider_options": provider_options,
        "reference_image_urls": reference_image_urls,
        "reference_image_artifact_ids": reference_image_artifact_ids,
    }
    if voice_audio:
        payload["voice_mode"] = "audio"
        payload["voice_audio"] = voice_audio
    if face_artifact_id:
        payload["face_artifact_id"] = face_artifact_id
    if face_image_url:
        payload["face_image_url"] = face_image_url
    if duration_sec is not None:
        payload["video"]["duration_sec"] = int(duration_sec)

    headers = _auth_headers(token_or_header, actor_user_id=actor_user_id, idempotency_key=idempotency_key)
    async with _client(timeout_s=120.0) as client:
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
    Prefer svc-fusion light status for polling paths. Fall back to the full job view.
    """
    base = settings.SVC_FUSION_BASE_URL.rstrip("/")
    light_path = getattr(settings, "SVC_FUSION_STATUS_LIGHT_PATH", None) or f"/jobs/{job_id}/status-light"
    url = base + str(light_path).format(job_id=job_id)
    headers = _auth_headers(token_or_header, actor_user_id=actor_user_id)

    async with _client(timeout_s=45.0) as client:
        r = await client.get(url, headers=headers)
        if r.status_code in {404, 405}:
            full_url = base + settings.SVC_FUSION_STATUS_PATH.format(job_id=job_id)
            r = await client.get(full_url, headers=headers)
        _raise_for_status_with_body(r)
        return r.json()


async def get_fusion_video_url_if_done(
    token_or_header: str,
    job_id: str,
    *,
    actor_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    st = await get_fusion_job(token_or_header, job_id, actor_user_id=actor_user_id)
    status = (st.get("status") or "").lower()

    if status in ("succeeded", "success", "done"):
        video_url = _pick_video_url(st)
        return {"status": "succeeded", "video_url": video_url, "raw": st}

    if status in ("failed", "error"):
        return {"status": "failed", "error_message": st.get("error_message") or str(st), "raw": st}

    return {"status": status, "raw": st}


# ---------------------------------------------------------------------------
# Compatibility wrappers for older orchestrator code
# ---------------------------------------------------------------------------

def _looks_like_uuid(value: str) -> bool:
    try:
        UUID(str(value))
        return True
    except Exception:
        return False


async def create_video_segment(
    token_or_header: str,
    image_ref: str,
    audio_url: str,
    idempotency_key: str,
    output_profile: Optional[str] = None,
    *,
    actor_user_id: Optional[str] = None,
    aspect_ratio: str = "9:16",
) -> Dict[str, Any]:
    """
    Back-compat wrapper for older callers.
    `output_profile` is currently ignored by svc-fusion and preserved only for signature compatibility.
    """
    image_ref = str(image_ref)
    kwargs: Dict[str, Any] = {
        "token_or_header": token_or_header,
        "audio_url": audio_url,
        "idempotency_key": idempotency_key,
        "actor_user_id": actor_user_id,
        "aspect_ratio": aspect_ratio,
    }
    if _looks_like_uuid(image_ref):
        kwargs["face_artifact_id"] = image_ref
    else:
        kwargs["face_image_url"] = image_ref

    created = await create_fusion_job(**kwargs)
    return {
        "job_id": created.get("job_id") or created.get("id"),
        "raw": created,
    }


async def get_video_job(
    token_or_header: str,
    job_id: str,
    *,
    actor_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Back-compat wrapper for older callers. Adds common aliases.
    """
    st = await get_fusion_job(token_or_header, job_id, actor_user_id=actor_user_id)
    video_url = _pick_video_url(st)
    out = dict(st)
    if video_url and not out.get("video_url"):
        out["video_url"] = video_url
    return out
