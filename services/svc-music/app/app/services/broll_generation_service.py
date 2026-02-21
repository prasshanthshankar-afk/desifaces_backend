from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.config import settings
from app.clients.svc_face_client import SvcFaceClient

logger = logging.getLogger(__name__)
JsonDict = Dict[str, Any]


def _as_dict(x: Any) -> JsonDict:
    return x if isinstance(x, dict) else {}


def _as_list(x: Any) -> List[Any]:
    return x if isinstance(x, list) else []


def _is_truthy(x: Any) -> bool:
    if x is True:
        return True
    if x is False or x is None:
        return False
    if isinstance(x, (int, float)):
        return x != 0
    if isinstance(x, str):
        return x.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(x)


def _seed_from_hex(h: str, default: int = 12345) -> int:
    try:
        hh = (h or "").strip()
        if not hh:
            return default
        return int(hh[:8], 16)
    except Exception:
        return default


def _svc_face_token(bearer_token: Optional[str]) -> Optional[str]:
    t = (bearer_token or "").strip()
    if t:
        return t
    fb = getattr(settings, "SVC_FACE_BEARER_TOKEN", None)
    fb = (str(fb).strip() if fb else "")
    return fb or None


def _normalize_seed_mode(payload: JsonDict) -> None:
    """
    svc-face accepts only: auto | random | deterministic
    Normalize any legacy values (e.g., "fixed") and guard unknowns.
    """
    v = str(payload.get("seed_mode") or "auto").strip().lower()
    if v == "fixed":
        v = "deterministic"
    if v not in ("auto", "random", "deterministic"):
        v = "auto"
    payload["seed_mode"] = v


def _stable_clip_id(clip: JsonDict, idx: int) -> str:
    """
    Avoid uuid-based IDs for determinism. Prefer clip_id from manifest.
    Fallbacks are stable across runs.
    """
    for k in ("clip_id", "id", "name"):
        v = clip.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # stable fallback
    return f"clip_{idx:03d}"


def _stable_seed(clip: JsonDict, *, clip_id: str, default: int = 12345) -> int:
    """
    Prefer clip_seed if present. Otherwise, derive deterministic seed from clip_id + timing hints.
    """
    clip_seed_hex = str(clip.get("clip_seed") or "").strip()
    if clip_seed_hex:
        return _seed_from_hex(clip_seed_hex, default=default)

    # include a bit of clip content to avoid collisions if many clips lack IDs
    start_ms = clip.get("start_ms")
    end_ms = clip.get("end_ms")
    section = str(clip.get("section") or "")
    salt = f"{clip_id}|{start_ms}|{end_ms}|{section}"
    h = hashlib.sha256(salt.encode("utf-8")).hexdigest()
    return _seed_from_hex(h, default=default)


@dataclass(frozen=True)
class BrollImage:
    clip_id: str
    image_url: str
    seed: int
    prompt: str


class BrollGenerationService:
    def __init__(self) -> None:
        self._face = SvcFaceClient(settings.SVC_FACE_URL)

    def _build_prompt(
        self,
        *,
        clip: JsonDict,
        preset_name: str,
        scene_primary_tag: str,
        scene_secondary_tags: List[str],
        no_face: bool,
    ) -> str:
        section = str(clip.get("section") or "verse")
        role = str(clip.get("role") or "broll")
        hints = [str(x) for x in _as_list(clip.get("prompt_hints")) if str(x).strip()]
        camera = _as_dict(clip.get("camera"))
        look = _as_dict(_as_dict(clip.get("video")).get("look"))

        tags = [scene_primary_tag] + [t for t in (scene_secondary_tags or []) if t]
        tags_txt = ", ".join(tags[:6])

        base = (
            "Hollywood cinematic b-roll shot, ultra sharp, high dynamic range, rich colors, "
            "professional lighting, smooth motion feel, 4k. "
            f"Scene tags: {tags_txt}. Preset: {preset_name}. Section: {section}. Role: {role}. "
        )
        if hints:
            base += "Shot hints: " + ", ".join(hints[:10]) + ". "

        if camera:
            cam_bits = []
            for k in ("angle", "lens", "movement", "framing"):
                v = camera.get(k)
                if isinstance(v, str) and v.strip():
                    cam_bits.append(f"{k}={v.strip()}")
            if cam_bits:
                base += "Camera: " + ", ".join(cam_bits) + ". "

        if look:
            lk = []
            for k in ("lighting", "tone", "palette"):
                v = look.get(k)
                if isinstance(v, str) and v.strip():
                    lk.append(f"{k}={v.strip()}")
            if lk:
                base += "Look: " + ", ".join(lk) + ". "

        if no_face:
            base += "No people, no faces, no close-up portraits, faceless b-roll only. "

        base += "No text, no watermark, no logo."
        return base.strip()

    async def _gen_one(
        self,
        *,
        token: Optional[str],
        clip: JsonDict,
        clip_index: int,
        preset_name: str,
        scene_primary_tag: str,
        scene_secondary_tags: List[str],
        no_face: bool,
    ) -> BrollImage:
        clip_id = _stable_clip_id(clip, clip_index)
        seed_int = _stable_seed(clip, clip_id=clip_id, default=12345)

        prompt = self._build_prompt(
            clip=clip,
            preset_name=preset_name,
            scene_primary_tag=scene_primary_tag,
            scene_secondary_tags=scene_secondary_tags,
            no_face=no_face,
        )

        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:10]

        payload: JsonDict = {
            "mode": "text-to-image",
            "num_variants": 1,
            "language": "en",
            "user_prompt": prompt,
            "seed_mode": "deterministic",
            "seed": int(seed_int),
            "request_nonce": f"broll_{clip_id}_{uuid4().hex}",
        }
        _normalize_seed_mode(payload)

        post_timeout_s = float(getattr(settings, "SVC_FACE_TIMEOUT_SECS", 60) or 60)
        poll_s = float(getattr(settings, "SVC_FACE_POLL_SECS", 2) or 2)
        wait_timeout_s = float(getattr(settings, "SVC_FACE_WAIT_TIMEOUT_SECS", 240) or 240)

        logger.info(
            "broll.gen_one start clip_id=%s idx=%s seed=%s seed_mode=%s prompt_hash=%s preset=%s primary=%s no_face=%s",
            clip_id,
            clip_index,
            seed_int,
            payload.get("seed_mode"),
            prompt_hash,
            preset_name,
            scene_primary_tag,
            no_face,
        )

        face_job_id = await self._face.create_creator_face_job(
            bearer_token=token,
            payload=payload,
            timeout_s=post_timeout_s,
            retries=0,
        )

        res = await self._face.wait_for_creator_face(
            bearer_token=token,
            job_id=face_job_id,
            timeout_s=wait_timeout_s,
            poll_s=poll_s,
        )

        st = str(getattr(res, "status", "") or "").strip().lower()
        img = str(getattr(res, "image_url", "") or "").strip()

        if ("succeeded" not in st) or not img:
            raise RuntimeError(f"svc-face broll failed: clip_id={clip_id} idx={clip_index} job_id={face_job_id} status={st}")

        logger.info(
            "broll.gen_one ok clip_id=%s idx=%s job_id=%s status=%s image_url_prefix=%s",
            clip_id,
            clip_index,
            face_job_id,
            st,
            img[:64] + ("..." if len(img) > 64 else ""),
        )

        return BrollImage(clip_id=clip_id, image_url=img, seed=int(seed_int), prompt=prompt)

    async def generate_broll_images(
        self,
        *,
        input_json: JsonDict,
        preset_name: str,
        scene_primary_tag: str,
        scene_secondary_tags: List[str],
        bearer_token: Optional[str] = None,
        max_images: int = 12,
        concurrency: int = 3,
    ) -> List[BrollImage]:
        token = _svc_face_token(bearer_token)

        computed = _as_dict(input_json.get("computed"))
        cm = _as_dict(computed.get("clip_manifest"))
        clips = _as_list(cm.get("clips"))

        if not clips:
            logger.info("broll.generate: no clips in computed.clip_manifest.clips")
            return []

        no_face = _is_truthy(_as_dict(input_json.get("provider_hints")).get("no_face") or computed.get("no_face"))

        clips2: List[JsonDict] = []
        for c in clips:
            if isinstance(c, dict):
                clips2.append(c)
            if len(clips2) >= int(max_images):
                break

        logger.info(
            "broll.generate start clips=%s max_images=%s concurrency=%s preset=%s primary=%s no_face=%s",
            len(clips2),
            max_images,
            concurrency,
            preset_name,
            scene_primary_tag,
            no_face,
        )

        sem = asyncio.Semaphore(max(1, int(concurrency)))

        async def run_one(idx: int, clip: JsonDict) -> BrollImage:
            async with sem:
                return await self._gen_one(
                    token=token,
                    clip=clip,
                    clip_index=idx,
                    preset_name=preset_name,
                    scene_primary_tag=scene_primary_tag,
                    scene_secondary_tags=scene_secondary_tags,
                    no_face=no_face,
                )

        results = await asyncio.gather(*(run_one(i, c) for i, c in enumerate(clips2)), return_exceptions=True)

        out: List[BrollImage] = []
        errors: List[str] = []
        for r in results:
            if isinstance(r, Exception):
                errors.append(repr(r))
            else:
                out.append(r)

        if errors:
            logger.error("broll.generate failed errors=%s", errors[:5])
            raise RuntimeError(f"broll.generate failed: {len(errors)}/{len(results)} clips errored. first={errors[0]}")

        # stable sort: clip_id includes idx fallback when missing
        out.sort(key=lambda x: x.clip_id)
        logger.info("broll.generate ok images=%s", len(out))
        return out