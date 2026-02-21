from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from app.clients.svc_face_client import SvcFaceClient
from app.config import settings
from app.repos.music_jobs_repo import MusicJobsRepo
from app.repos.steps_repo import StepsRepo
from app.services.music_orchestrator_common import _as_dict
from app.services.music_orchestrator_studio_jobs import persist_studio_payload_best_effort

JsonDict = Dict[str, Any]


def _svc_face_internal_bearer_token(bearer_token: Optional[str]) -> Optional[str]:
    t = (bearer_token or "").strip()
    if t:
        return t
    fb = getattr(settings, "SVC_FACE_BEARER_TOKEN", None)
    fb = (str(fb).strip() if fb else "")
    return fb or None


def _safe_default_face_prompt_from_music(*, proj: JsonDict, input_json: JsonDict, which: str) -> str:
    computed = _as_dict(input_json.get("computed"))
    lang = str(proj.get("language_hint") or computed.get("language_hint") or "en-IN").strip()
    title = str(proj.get("title") or computed.get("title") or "Untitled").strip()
    gender_hint = str(computed.get(f"{which}_gender") or computed.get("gender_hint") or "").strip()

    base = (
        "Ultra-realistic portrait photo, high detail, natural skin texture, "
        "soft studio lighting, sharp focus, neutral background, looking at camera."
    )
    who = "Indian performer A" if which == "performer_a" else "Indian performer B"
    extra = f" {gender_hint}." if gender_hint else ""
    return f"{base} {who}.{extra} For music video titled '{title}'. Language hint {lang}."


async def _ensure_performer_face_image_url(
    *,
    bearer_token: Optional[str],
    face_prompt: str,
    request_nonce: Optional[str] = None,
) -> str:
    token = _svc_face_internal_bearer_token(bearer_token)
    face = SvcFaceClient(settings.SVC_FACE_URL)

    payload = {
        "mode": "text-to-image",
        "num_variants": 1,
        "language": "en",
        "user_prompt": face_prompt,
        "seed_mode": "random",
        "request_nonce": request_nonce or uuid4().hex,
    }

    post_timeout_s = float(getattr(settings, "SVC_FACE_TIMEOUT_SECS", 60) or 60)
    poll_s = float(getattr(settings, "SVC_FACE_POLL_SECS", 2) or 2)
    wait_timeout_s = float(getattr(settings, "SVC_FACE_WAIT_TIMEOUT_SECS", 180) or 180)

    face_job_id = await face.create_creator_face_job(
        bearer_token=token,
        payload=payload,
        timeout_s=post_timeout_s,
        retries=0,
    )

    res = await face.wait_for_creator_face(
        bearer_token=token,
        job_id=face_job_id,
        timeout_s=wait_timeout_s,
        poll_s=poll_s,
    )

    st = str(getattr(res, "status", "") or "").strip().lower()
    img = str(getattr(res, "image_url", "") or "").strip()

    if ("succeeded" not in st) or not img:
        raise RuntimeError(f"svc-face failed or timed out: job_id={face_job_id} status={st} has_image={bool(img)}")

    return img


def _apply_performer_face_aliases(*, computed: JsonDict) -> JsonDict:
    """
    Downstream performer-video code expects:
      computed.performer_face_image_url / performer_face_artifact_id

    Face generation currently produces:
      computed.performer_a_image_url (and optionally performer_b_image_url)

    This function adds safe aliases so performer video never fails with
      performer_videos_missing_face_ref.
    """
    c = _as_dict(computed)

    a_url = str(c.get("performer_a_image_url") or "").strip()
    b_url = str(c.get("performer_b_image_url") or "").strip()

    # Prefer A as the default performer face
    if a_url and not str(c.get("performer_face_image_url") or "").strip():
        c["performer_face_image_url"] = a_url

    # If you ever start storing artifact ids for A/B, alias those too (safe no-op today)
    a_art = str(c.get("performer_a_artifact_id") or "").strip()
    b_art = str(c.get("performer_b_artifact_id") or "").strip()

    if a_art and not str(c.get("performer_face_artifact_id") or "").strip():
        c["performer_face_artifact_id"] = a_art
    elif b_art and (not str(c.get("performer_face_artifact_id") or "").strip()):
        c["performer_face_artifact_id"] = b_art

    # Also keep a small convenience list (used elsewhere)
    imgs: list[str] = []
    if a_url:
        imgs.append(a_url)
    if b_url and b_url != a_url:
        imgs.append(b_url)
    if imgs:
        c["performer_images"] = imgs

    return c


async def ensure_music_job_performer_faces(
    *,
    jobs: MusicJobsRepo,
    steps: StepsRepo,
    job_id: UUID,
    proj: JsonDict,
    input_json: JsonDict,
    persist: bool = True,
) -> JsonDict:
    """
    Ensure computed.performer_a_image_url and (optionally) performer_b_image_url exist.
    Also writes aliases:
      computed.performer_face_image_url (points to performer_a_image_url)
      computed.performer_face_artifact_id (if available)

    Performance:
      - If both A & B are missing and duet layout requires two, generate them concurrently.

    If persist=False, we do not write back to DB/studio_jobs; caller can merge and persist once.
    """
    computed = _as_dict(input_json.get("computed"))
    duet_layout = str(proj.get("duet_layout") or "split_screen").strip().lower()

    needs_two = duet_layout in {
        "split_screen",
        "split-screen",
        "duet",
        "side_by_side",
        "side-by-side",
        "two_shot",
        "two-shot",
        "two_shots",
        "two-shots",
        "dual",
        "double",
    }

    a_url = str(computed.get("performer_a_image_url") or "").strip()
    b_url = str(computed.get("performer_b_image_url") or "").strip()

    # If already present, just ensure aliases and persist (optional)
    if a_url and (b_url or not needs_two):
        computed = _apply_performer_face_aliases(computed=computed)
        input_json["computed"] = computed
        if persist:
            await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
            await persist_studio_payload_best_effort(job_id=job_id, payload_json=input_json)
        return input_json

    try:
        await steps.upsert_step(
            job_id=job_id,
            step_code="ensure_performer_faces",
            status="running",
            meta_json={"duet_layout": duet_layout, "needs_two": needs_two},
        )
    except Exception:
        pass

    token: Optional[str] = None
    tasks = []

    if not a_url:
        prompt_a = _safe_default_face_prompt_from_music(proj=proj, input_json=input_json, which="performer_a")
        tasks.append(
            (
                "a",
                asyncio.create_task(
                    _ensure_performer_face_image_url(
                        bearer_token=token,
                        face_prompt=prompt_a,
                        request_nonce=f"a_{uuid4().hex}",
                    )
                ),
            )
        )

    if needs_two and not b_url:
        prompt_b = _safe_default_face_prompt_from_music(proj=proj, input_json=input_json, which="performer_b")
        prompt_b = f"{prompt_b} Different person from performer A."
        tasks.append(
            (
                "b",
                asyncio.create_task(
                    _ensure_performer_face_image_url(
                        bearer_token=token,
                        face_prompt=prompt_b,
                        request_nonce=f"b_{uuid4().hex}",
                    )
                ),
            )
        )

    if tasks:
        results = await asyncio.gather(*[t for _, t in tasks], return_exceptions=True)
        for (label, _), r in zip(tasks, results):
            if isinstance(r, Exception):
                raise r
            if label == "a":
                a_url = str(r).strip()
                computed["performer_a_image_url"] = a_url
            elif label == "b":
                b_url = str(r).strip()
                computed["performer_b_image_url"] = b_url

    # Always apply aliases before persisting
    computed = _apply_performer_face_aliases(computed=computed)
    input_json["computed"] = computed

    if persist:
        await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
        await persist_studio_payload_best_effort(job_id=job_id, payload_json=input_json)

    try:
        await steps.upsert_step(
            job_id=job_id,
            step_code="ensure_performer_faces",
            status="succeeded",
            meta_json={
                "performer_a": bool(str(computed.get("performer_a_image_url") or "").strip()),
                "performer_b": bool(str(computed.get("performer_b_image_url") or "").strip()) if needs_two else False,
                "needs_two": needs_two,
                "aliased_face_ref": bool(str(computed.get("performer_face_image_url") or "").strip()),
            },
        )
    except Exception:
        pass

    return input_json