from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.config import settings
from app.clients.svc_face_client import SvcFaceClient
from app.repos.music_jobs_repo import MusicJobsRepo
from app.repos.steps_repo import StepsRepo
from app.services.music_orchestrator_common import _as_dict, _clamp_int, _stable_json
from app.services.music_orchestrator_studio_jobs import persist_studio_payload_best_effort

JsonDict = Dict[str, Any]


def _hmac_seed_int(key_hex: str, msg: str) -> int:
    d = hmac.new(key_hex.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()
    return int.from_bytes(d[:4], "big") & 0x7FFFFFFF


def _pick_evenly(items: List[Any], k: int) -> List[Any]:
    if k <= 0:
        return []
    if k >= len(items):
        return items[:]
    if k == 1:
        return [items[len(items) // 2]]
    out: List[Any] = []
    for i in range(k):
        idx = round(i * (len(items) - 1) / (k - 1))
        out.append(items[idx])
    return out


def _clip_id_from_clip(clip: JsonDict) -> str:
    cid = str(clip.get("clip_id") or "").strip()
    if cid:
        return cid
    return hashlib.sha1(_stable_json(clip).encode("utf-8")).hexdigest()[:12]


def _build_broll_prompt(*, preset_name: str, scene_primary: str, scene_secondary: List[Any], clip: JsonDict, title: str) -> str:
    sec = ", ".join([str(x) for x in (scene_secondary or [])[:4] if x])
    section = str(clip.get("section") or "").strip()
    hints = clip.get("prompt_hints")
    if isinstance(hints, list):
        hints_s = ", ".join([str(x) for x in hints[:4] if x])
    else:
        hints_s = ""

    base = f"{preset_name}. {title}. Scene: {scene_primary}. {sec}."
    if section:
        base += f" Section: {section}."
    if hints_s:
        base += f" Hints: {hints_s}."

    return (
        base
        + " Cinematic b-roll shot, no humans, no faces, no text, no watermark, filmic lighting, high detail, 4k."
    ).strip()


def _svc_face_internal_bearer_token() -> str | None:
    fb = getattr(settings, "SVC_FACE_BEARER_TOKEN", None)
    fb = (str(fb).strip() if fb else "")
    return fb or None


def _get_int_setting(name: str, default: int) -> int:
    v = getattr(settings, name, None)
    try:
        return int(float(v)) if v is not None else int(default)
    except Exception:
        return int(default)


def _assign_library_to_all_clips(
    *,
    manifest_clips: List[Any],
    library_images: List[JsonDict],
    job_seed: str,
) -> Tuple[Dict[str, str], List[JsonDict]]:
    """
    Build by_clip mapping for *all* clips using the generated library.
    Deterministic mapping: idx = HMAC(job_seed, f"assign:{clip_id}") % len(library)
    Returns (by_clip, assigned_items)
    """
    by_clip: Dict[str, str] = {}
    assigned: List[JsonDict] = []

    if not library_images:
        return by_clip, assigned

    n = len(library_images)
    for raw in manifest_clips:
        if not isinstance(raw, dict):
            continue
        cid = _clip_id_from_clip(raw)
        idx = _hmac_seed_int(job_seed, f"assign:{cid}") % n
        img = str(library_images[idx].get("image_url") or "").strip()
        if not img:
            continue
        by_clip[cid] = img
        assigned.append({"clip_id": cid, "image_url": img, "library_index": idx})

    return by_clip, assigned


async def ensure_broll_for_manifest(
    *,
    steps: StepsRepo,
    jobs: MusicJobsRepo,
    job_id: UUID,
    proj: JsonDict,
    input_json: JsonDict,
    persist: bool = True,
) -> JsonDict:
    """
    Generate a small library (6–16) of b-roll images (no faces) via svc-face and attach them to computed.broll.

    IMPORTANT: We then map this library to *every* clip_id in the manifest (computed.broll.by_clip),
    so montage renders full length without needing 1 image per clip.

    Performance:
      - Two-phase parallelism:
          (1) submit many jobs quickly (SVC_FACE_BROLL_SUBMIT_PARALLEL, default 8)
          (2) wait/poll bounded (SVC_FACE_BROLL_WAIT_PARALLEL, default 4)
      - Crash-resume: writes computed.broll_pending early (if persist=True)

    Safety:
      - If persist=False, DO NOT write input_json back to DB; caller can merge and persist once.
    """
    t0 = time.time()

    computed = _as_dict(input_json.get("computed"))

    # Fast-path: already have broll with usable mapping
    existing = _as_dict(computed.get("broll"))
    if isinstance(existing.get("by_clip"), dict) and existing.get("by_clip"):
        return input_json

    manifest = _as_dict(computed.get("clip_manifest"))
    clips = manifest.get("clips")
    if not isinstance(clips, list) or not clips:
        raise RuntimeError("broll_missing_clip_manifest")

    # Generate only a small library for speed, but map across all clips
    library_k = _clamp_int(len(clips), 6, 16)
    selected = _pick_evenly([c for c in clips if isinstance(c, dict)], library_k)

    preset_name = str(manifest.get("preset_name") or computed.get("preset_name") or "Urban Neon Hustle Peak").strip()
    scene = _as_dict(manifest.get("scene"))
    primary = str(scene.get("primary_tag") or computed.get("scene_primary_tag") or "stage").strip()
    secondary = scene.get("secondary_tags") or computed.get("scene_secondary_tags") or []
    if not isinstance(secondary, list):
        secondary = []

    job_seed = (
        str(manifest.get("job_seed") or "").strip()
        or hashlib.sha256(f"{job_id}:{preset_name}".encode("utf-8")).hexdigest()[:32]
    )

    title = str(proj.get("title") or computed.get("title") or "Music Video").strip()

    submit_parallel = _clamp_int(_get_int_setting("SVC_FACE_BROLL_SUBMIT_PARALLEL", 8), 1, 32)
    wait_parallel = _clamp_int(_get_int_setting("SVC_FACE_BROLL_WAIT_PARALLEL", 4), 1, 16)

    try:
        await steps.upsert_step(
            job_id=job_id,
            step_code="generate_broll",
            status="running",
            meta_json={
                "library_k": library_k,
                "selected": len(selected),
                "preset_name": preset_name,
                "scene_primary_tag": primary,
                "submit_parallel": submit_parallel,
                "wait_parallel": wait_parallel,
            },
        )
    except Exception:
        pass

    face = SvcFaceClient(settings.SVC_FACE_URL)
    token = _svc_face_internal_bearer_token()
    post_timeout_s = float(getattr(settings, "SVC_FACE_TIMEOUT_SECS", 60) or 60)
    poll_s = float(getattr(settings, "SVC_FACE_POLL_SECS", 2) or 2)
    wait_timeout_s = float(getattr(settings, "SVC_FACE_WAIT_TIMEOUT_SECS", 180) or 180)

    # Resume support: if we have pending jobs, reuse them
    pending = _as_dict(computed.get("broll_pending"))
    pending_jobs = pending.get("jobs")
    if not isinstance(pending_jobs, list):
        pending_jobs = []

    pending_by_clip: Dict[str, JsonDict] = {}
    for it in pending_jobs:
        if not isinstance(it, dict):
            continue
        cid = str(it.get("clip_id") or "").strip()
        fj = str(it.get("face_job_id") or "").strip()
        if cid and fj:
            pending_by_clip[cid] = it

    submit_sem = asyncio.Semaphore(submit_parallel)
    wait_sem = asyncio.Semaphore(wait_parallel)

    async def _submit_one(clip: JsonDict) -> JsonDict:
        cid = _clip_id_from_clip(clip)

        # if already pending, reuse
        if cid in pending_by_clip:
            return dict(pending_by_clip[cid])

        seed = _hmac_seed_int(job_seed, f"broll:{cid}")
        prompt = _build_broll_prompt(
            preset_name=preset_name,
            scene_primary=primary,
            scene_secondary=secondary,
            clip=clip,
            title=title,
        )

        payload = {
            "mode": "text-to-image",
            "num_variants": 1,
            "language": "en",
            "user_prompt": prompt,
            "seed_mode": "deterministic",
            "seed": int(seed),
            "request_nonce": f"broll_{job_id}_{cid}_{seed}",
            "provider_hints": {
                "no_face": True,
                "broll": True,
                "aspect_ratio": "16:9",
                "style": "cinematic",
            },
        }

        async with submit_sem:
            face_job_id = await face.create_creator_face_job(
                bearer_token=token,
                payload=payload,
                timeout_s=post_timeout_s,
                retries=0,
            )

        return {
            "clip_id": cid,
            "seed": int(seed),
            "prompt": prompt,
            "face_job_id": str(face_job_id),
            "status": "submitted",
        }

    async def _wait_one(pending_item: JsonDict) -> Tuple[str, Optional[JsonDict], Optional[str]]:
        cid = str(pending_item.get("clip_id") or "").strip()
        face_job_id = str(pending_item.get("face_job_id") or "").strip()
        if not cid or not face_job_id:
            return cid or "unknown", None, "invalid_pending_item"

        async with wait_sem:
            res = await face.wait_for_creator_face(
                bearer_token=token,
                job_id=face_job_id,
                timeout_s=wait_timeout_s,
                poll_s=poll_s,
            )

        st = str(getattr(res, "status", "") or "").strip().lower()
        img = str(getattr(res, "image_url", "") or "").strip()

        if ("succeeded" not in st) or not img:
            return cid, None, f"generate_broll_failed: clip_id={cid} job_id={face_job_id} status={st} has_image={bool(img)}"

        out = dict(pending_item)
        out.update({"status": "succeeded", "image_url": img})
        return cid, out, None

    # 1) Submit phase (fast)
    submit_tasks: List[asyncio.Task] = []
    for clip in selected:
        if isinstance(clip, dict):
            submit_tasks.append(asyncio.create_task(_submit_one(clip)))

    submitted = await asyncio.gather(*submit_tasks, return_exceptions=True)

    pending_items: List[JsonDict] = []
    submit_errors: List[str] = []
    for r in submitted:
        if isinstance(r, Exception):
            submit_errors.append(str(r))
            continue
        if isinstance(r, dict) and str(r.get("face_job_id") or "").strip():
            pending_items.append(r)

    if not pending_items:
        raise RuntimeError(f"generate_broll_submit_failed_all errors={submit_errors[:3]}")

    # Write pending immediately (for resume) before waiting
    computed["broll_pending"] = {
        "job_seed": job_seed,
        "preset_name": preset_name,
        "scene_primary_tag": primary,
        "submitted": len(pending_items),
        "jobs": pending_items,
        "submit_errors": submit_errors[:6],
        "created_at": int(time.time()),
    }
    input_json["computed"] = computed

    if persist:
        await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
        await persist_studio_payload_best_effort(job_id=job_id, payload_json=input_json)

    # 2) Wait phase (bounded polling)
    wait_tasks = [asyncio.create_task(_wait_one(it)) for it in pending_items]
    waited = await asyncio.gather(*wait_tasks, return_exceptions=True)

    library_images: List[JsonDict] = []
    wait_errors: List[str] = []
    for r in waited:
        if isinstance(r, Exception):
            wait_errors.append(str(r))
            continue
        cid, item, err = r
        if err:
            wait_errors.append(err)
            continue
        if isinstance(item, dict) and str(item.get("image_url") or "").strip():
            library_images.append(item)

    if not library_images:
        raise RuntimeError("generate_broll_failed_all")

    # Map library to *all* clips for montage completeness
    by_clip, assigned = _assign_library_to_all_clips(
        manifest_clips=[c for c in clips if isinstance(c, dict)],
        library_images=library_images,
        job_seed=job_seed,
    )
    if not by_clip:
        raise RuntimeError("generate_broll_failed_no_assignments")

    computed["broll"] = {
        "library_images": library_images,  # the 6–16 actual generated images
        "assigned": assigned,              # mapping trace (optional)
        "by_clip": by_clip,                # IMPORTANT for montage
        "total_library": len(library_images),
        "total_assigned": len(by_clip),
        "desired_library": library_k,
        "source": "svc-face",
        "errors": {
            "submit": submit_errors[:10],
            "wait": wait_errors[:10],
        },
    }

    # Clear pending (best effort)
    computed.pop("broll_pending", None)

    input_json["computed"] = computed

    if persist:
        await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
        await persist_studio_payload_best_effort(job_id=job_id, payload_json=input_json)

    try:
        await steps.upsert_step(
            job_id=job_id,
            step_code="generate_broll",
            status="succeeded",
            meta_json={
                "library_k": library_k,
                "total_library": len(library_images),
                "total_assigned": len(by_clip),
                "preset_name": preset_name,
                "scene_primary_tag": primary,
                "submit_parallel": submit_parallel,
                "wait_parallel": wait_parallel,
                "elapsed_s": round(time.time() - t0, 3),
                "submit_errors": submit_errors[:3],
                "wait_errors": wait_errors[:3],
            },
        )
    except Exception:
        pass

    return input_json