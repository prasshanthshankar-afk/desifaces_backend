from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

from tenacity import RetryError

from app.config import settings
from app.db import get_db_pool
from app.http_clients.audio_client import create_tts_audio_blocking
from app.http_clients.fusion_client import create_fusion_job, get_fusion_job, _pick_video_url
from app.repos.longform_segments_repo import LongformSegmentsRepo
from app.services.sas_service import parse_blob_path_from_sas_url

logger = logging.getLogger("svc_fusion_extension.longform_worker")
segs_repo = LongformSegmentsRepo()


# -----------------------------
# Helpers: auth + retry + json
# -----------------------------
def _normalize_bearer(token_or_header: str) -> str:
    """
    Accept raw token OR full 'Bearer <...>' and return 'Bearer <...>'.
    """
    t = (token_or_header or "").strip()
    if not t:
        return ""
    if t.lower().startswith("bearer "):
        return t
    return f"Bearer {t}"


def _resolve_auth_token(seg_row: Dict[str, Any]) -> str:
    """
    1) Prefer DB-provided per-job auth_token (if present)
    2) Fallback to service-to-service bearer from settings/env
    """
    tok = _normalize_bearer(seg_row.get("auth_token") or "")
    if tok:
        return tok

    svc_tok = ""
    if hasattr(settings, "SVC_TO_SVC_BEARER"):
        svc_tok = getattr(settings, "SVC_TO_SVC_BEARER") or ""

    svc_tok = svc_tok or os.getenv("SVC_TO_SVC_BEARER", "") or os.getenv("SVC_FUSION_EXTENSION_BEARER", "")
    return _normalize_bearer(svc_tok)


def _unwrap_retry_error(e: Exception) -> Exception:
    """
    Tenacity wraps the real exception as RetryError; unwrap so we store the real root cause.
    """
    if isinstance(e, RetryError):
        try:
            last = e.last_attempt.exception()
            return last or e
        except Exception:
            return e
    return e


def _safe_errmsg(e: Exception) -> str:
    root = _unwrap_retry_error(e)
    return f"{type(root).__name__}: {root}"


def _exc_info_tuple(e: Exception) -> Tuple[type, BaseException, Any]:
    """
    logging expects exc_info as (exc_type, exc, tb).
    """
    root = _unwrap_retry_error(e)
    return (type(root), root, root.__traceback__)


def _as_dict(val: Any, *, field: str) -> Dict[str, Any]:
    """
    Normalize DB json/jsonb fields that might come back as:
      - dict
      - None
      - str containing JSON
    """
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
        except Exception as ex:
            raise RuntimeError(f"{field}_invalid_json: {ex}") from ex
        return parsed if isinstance(parsed, dict) else {}
    raise RuntimeError(f"{field}_wrong_type: {type(val).__name__}")


# -----------------------------
# Helpers: voice gender + voice
# -----------------------------
def _normalize_gender(val: Any) -> Optional[str]:
    s = ("" if val is None else str(val)).strip().lower()
    if not s:
        return None
    if s in ("m", "male", "man", "boy"):
        return "male"
    if s in ("f", "female", "woman", "girl"):
        return "female"
    return None


def _default_voice_for(gender: str, locale: str) -> str:
    """
    Default voice chooser.
    Override via env/settings:
      - DEFAULT_TTS_VOICE_FEMALE
      - DEFAULT_TTS_VOICE_MALE
    """
    loc = (locale or "en-US").strip() or "en-US"

    female = getattr(settings, "DEFAULT_TTS_VOICE_FEMALE", None) or os.getenv("DEFAULT_TTS_VOICE_FEMALE", "")
    male = getattr(settings, "DEFAULT_TTS_VOICE_MALE", None) or os.getenv("DEFAULT_TTS_VOICE_MALE", "")

    if (gender or "").lower() == "male":
        return male.strip() or (f"{loc}-GuyNeural" if loc.lower().startswith("en-") else "en-US-GuyNeural")
    return female.strip() or (f"{loc}-JennyNeural" if loc.lower().startswith("en-") else "en-US-JennyNeural")


def _resolve_gender_mode_and_manual(seg: Dict[str, Any], voice_cfg: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """
    Read policy from:
      - longform_jobs columns returned via fetch_next_segments:
          voice_gender_mode, voice_gender
      - OR from voice_cfg (back-compat): voice_gender_mode/voice_gender/gender
    """
    mode = seg.get("voice_gender_mode") or voice_cfg.get("voice_gender_mode") or "auto"
    mode = str(mode).strip().lower()
    if mode not in ("auto", "manual"):
        mode = "auto"

    manual = seg.get("voice_gender") or voice_cfg.get("voice_gender") or voice_cfg.get("gender")
    return mode, _normalize_gender(manual)


def _infer_gender_from_seg(seg: Dict[str, Any]) -> Optional[str]:
    """
    Infer from face_meta_json (joined from media_assets.meta_json in fetch_next_segments).
    Expected: meta_json.gender = "male"|"female"
    """
    meta = _as_dict(seg.get("face_meta_json"), field="face_meta_json")

    g = _normalize_gender(meta.get("gender"))
    if g:
        return g

    for k in ("sex", "voice_gender", "gender_hint"):
        g = _normalize_gender(meta.get(k))
        if g:
            return g

    return None


def _apply_voice_selection(seg: Dict[str, Any], voice_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    If caller already provided voice/voice_id -> respect it.
    Else pick voice based on:
      - manual mode (voice_gender) OR
      - auto mode inferred from face_meta_json OR
      - default female
    """
    voice_cfg = dict(voice_cfg or {})

    # Respect explicit voice selection
    if voice_cfg.get("voice") or voice_cfg.get("voice_id"):
        return voice_cfg

    mode, manual_gender = _resolve_gender_mode_and_manual(seg, voice_cfg)

    if mode == "manual":
        if not manual_gender:
            raise RuntimeError("voice_gender_missing_for_manual_mode")
        resolved_gender = manual_gender
    else:
        resolved_gender = _infer_gender_from_seg(seg) or "female"

    locale = voice_cfg.get("locale") or voice_cfg.get("target_locale") or "en-US"
    voice_cfg["voice"] = _default_voice_for(resolved_gender, str(locale))
    voice_cfg["voice_gender_resolved"] = resolved_gender
    return voice_cfg


# -----------------------------
# Fusion polling
# -----------------------------
async def _poll_fusion_until_done(
    token_or_header: str,
    job_id: str,
    *,
    actor_user_id: Optional[str],
    timeout_seconds: int,
) -> Dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + float(timeout_seconds)
    while True:
        st = await get_fusion_job(token_or_header, job_id, actor_user_id=actor_user_id)
        status = (st.get("status") or "").lower()

        if status in ("succeeded", "success", "done"):
            return st
        if status in ("failed", "error"):
            raise RuntimeError(st.get("error_message") or str(st))
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"svc-fusion job {job_id} timed out")

        await asyncio.sleep(settings.FUSION_POLL_SECONDS)


# -----------------------------
# Worker loop
# -----------------------------
async def worker_loop() -> None:
    if not settings.WORKER_ENABLED:
        logger.info("WORKER_ENABLED=false; exiting worker loop.")
        return

    pool = await get_db_pool()
    logger.info(
        "longform_worker started (batch=%s poll=%.2fs max_inflight=%s)",
        settings.WORKER_BATCH_SIZE,
        float(settings.WORKER_POLL_SECONDS),
        settings.MAX_INFLIGHT_SEGMENTS_PER_JOB,
    )

    while True:
        async with pool.acquire() as conn:
            segs = await segs_repo.fetch_next_segments(
                conn,
                settings.WORKER_BATCH_SIZE,
                settings.MAX_INFLIGHT_SEGMENTS_PER_JOB,
            )

        if not segs:
            await asyncio.sleep(settings.WORKER_POLL_SECONDS)
            continue

        for s in segs:
            seg = dict(s)

            seg_id = str(seg["id"])
            longform_job_id = str(seg["job_id"])
            user_id = str(seg["user_id"])

            face_artifact_id = str(seg["face_artifact_id"]) if seg.get("face_artifact_id") else None
            face_image_url = seg.get("face_image_url")  # SAS URL from media_assets.storage_ref
            aspect_ratio = seg["aspect_ratio"]
            duration_sec = min(int(getattr(settings, "MAX_SEGMENT_SECONDS", 120)), int(seg["duration_sec"]))
            text_chunk = seg["text_chunk"]

            voice_cfg = _as_dict(seg.get("voice_cfg"), field="voice_cfg")

            try:
                token_or_header = _resolve_auth_token(seg)
                if not token_or_header:
                    raise RuntimeError(
                        "Missing auth_token and no service bearer configured "
                        "(set SVC_TO_SVC_BEARER or SVC_FUSION_EXTENSION_BEARER)."
                    )

                # Face selector: prefer SAS URL always
                selected_face_image_url: Optional[str]
                selected_face_artifact_id: Optional[str]

                if face_image_url and str(face_image_url).strip():
                    selected_face_image_url = str(face_image_url).strip()
                    selected_face_artifact_id = None
                else:
                    selected_face_image_url = None
                    selected_face_artifact_id = face_artifact_id

                if not (selected_face_image_url or selected_face_artifact_id):
                    raise RuntimeError("Missing face selector: neither face_image_url nor face_artifact_id is present")

                # ✅ voice selection (male/female auto/manual) unless caller already set voice
                voice_cfg = _apply_voice_selection(seg, voice_cfg)

                logger.info(
                    "segment start seg_id=%s job_id=%s user_id=%s aspect=%s dur=%s has_face_url=%s has_face_artifact=%s voice=%s resolved_gender=%s",
                    seg_id,
                    longform_job_id,
                    user_id,
                    aspect_ratio,
                    duration_sec,
                    bool(selected_face_image_url),
                    bool(selected_face_artifact_id),
                    voice_cfg.get("voice") or voice_cfg.get("voice_id") or "<none>",
                    voice_cfg.get("voice_gender_resolved") or "<unset>",
                )

                # 1) TTS
                audio_res = await create_tts_audio_blocking(
                    token_or_header=token_or_header,
                    text=text_chunk,
                    voice_cfg=voice_cfg,
                    actor_user_id=user_id,  # required for svc bearer path; safe for user JWT too
                    poll_seconds=settings.AUDIO_POLL_SECONDS,
                    timeout_seconds=settings.AUDIO_TIMEOUT_SECONDS,
                )

                # ✅ Persist TTS job id + url (+ audio_artifact_id if present)
                async with pool.acquire() as conn:
                    await segs_repo.save_audio_result(
                        conn,
                        seg_id,
                        tts_job_id=audio_res["job_id"],
                        audio_url=audio_res["audio_url"],
                        audio_artifact_id=audio_res.get("audio_artifact_id"),
                    )

                # 2) Fusion
                created = await create_fusion_job(
                    token_or_header=token_or_header,
                    actor_user_id=user_id,
                    face_image_url=selected_face_image_url,
                    face_artifact_id=selected_face_artifact_id,
                    audio_url=audio_res["audio_url"],
                    duration_sec=duration_sec,
                    aspect_ratio=aspect_ratio,
                    tags={
                        "source": "svc-fusion-extension",
                        "longform_job_id": longform_job_id,
                        "segment_id": seg_id,
                        "user_id": user_id,
                        "voice_gender_resolved": voice_cfg.get("voice_gender_resolved"),
                        "voice": voice_cfg.get("voice") or voice_cfg.get("voice_id"),
                    },
                )
                fusion_job_id = created["job_id"]

                async with pool.acquire() as conn:
                    await segs_repo.save_fusion_job(conn, seg_id, fusion_job_id)

                # 3) Poll fusion
                st = await _poll_fusion_until_done(
                    token_or_header,
                    fusion_job_id,
                    actor_user_id=user_id,
                    timeout_seconds=settings.FUSION_TIMEOUT_SECONDS,
                )
                video_url = _pick_video_url(st)
                if not video_url:
                    raise RuntimeError("svc-fusion succeeded but no video artifact found")

                _, blob_path = parse_blob_path_from_sas_url(video_url)
                provider_job_id = st.get("provider_job_id")

                # 4) Segment succeeded + bump job progress
                async with pool.acquire() as conn:
                    await segs_repo.mark_succeeded(
                        conn,
                        seg_id,
                        segment_video_url=video_url,
                        segment_storage_path=blob_path,
                        provider_job_id=provider_job_id,
                    )

                    await conn.execute(
                        """
                        update public.longform_jobs
                        set
                          completed_segments = completed_segments + 1,
                          status = case
                            when completed_segments + 1 >= total_segments then 'stitching'
                            else status
                          end
                        where id = $1::uuid
                        """,
                        longform_job_id,
                    )

                logger.info("segment succeeded seg_id=%s fusion_job_id=%s", seg_id, fusion_job_id)

            except Exception as e:
                msg = _safe_errmsg(e)
                logger.error(
                    "segment failed seg_id=%s job_id=%s user_id=%s err=%s",
                    seg_id,
                    longform_job_id,
                    user_id,
                    msg,
                    exc_info=_exc_info_tuple(e),
                )

                async with pool.acquire() as conn:
                    await segs_repo.mark_failed(conn, seg_id, error_code="SEGMENT_FAILED", error_message=msg)
                    await conn.execute(
                        """
                        update public.longform_jobs
                        set status='failed', error_code='SEGMENT_FAILED', error_message=$2
                        where id=$1::uuid and status not in ('succeeded','failed')
                        """,
                        longform_job_id,
                        msg,
                    )

        await asyncio.sleep(settings.WORKER_POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(worker_loop())