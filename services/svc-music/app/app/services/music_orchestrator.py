from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import wave
from pathlib import Path
from typing import Optional, Any, Dict, List, Tuple
from uuid import UUID, uuid4

from app.config import settings
from app.db import get_pool
from app.domain.enums import (
    MusicJobStage,
    MusicTrackType,
    MusicJobStatus,
    MusicProjectMode,
)
from app.domain.models import MusicJobStatusOut, TrackItem, PublishMusicIn, PublishMusicOut
from app.repos.music_jobs_repo import MusicJobsRepo
from app.repos.music_projects_repo import MusicProjectsRepo
from app.repos.music_tracks_repo import MusicTracksRepo
from app.repos.steps_repo import StepsRepo
from app.services.azure_storage_service import AzureStorageService

from .music_graph import MusicGraphState, MusicGraphTools, GraphTrack, run_video_pipeline

from app.clients.svc_face_client import SvcFaceClient

# Optional: keep orchestrator lean; planner lives elsewhere.
try:
    from app.services.music_planning.service import MusicPlanningService  # type: ignore
except Exception:
    MusicPlanningService = None  # type: ignore

# Optional: autopilot provider (Fal Sonauto v2) lives elsewhere to keep this file manageable.
# If module isn't present, orchestration still works via native fallback.
try:
    from app.services.music_providers.autopilot_router import (  # type: ignore
        AutopilotComposeResult,
        compose_full_mix_fal_sonauto_v2,
        default_autopilot_provider,
        normalize_provider,
    )
except Exception:
    AutopilotComposeResult = Any  # type: ignore

    def normalize_provider(p: Any) -> str:  # type: ignore
        return str(p or "").strip().lower().replace("-", "_")

    def default_autopilot_provider() -> str:  # type: ignore
        # If the external provider module isn't installed, do NOT advertise fal provider here.
        return "native"

    compose_full_mix_fal_sonauto_v2 = None  # type: ignore


# -----------------------------
# Queue / worker integration
# -----------------------------
from app.services.music_tools import ConcreteMusicTools


async def enqueue_video_job(job_id: UUID) -> None:
    # DB polling worker: can be no-op. If you later add a real queue (Redis/ASB),
    # enqueue a message here.
    return None


# -----------------------------
# Helpers
# -----------------------------
def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        if s.startswith("{") or s.startswith("["):
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
        return {}
    return {}


def _normalize_jsonb_payload(x: Any) -> Dict[str, Any]:
    """
    Handles jsonb that is:
      - a dict already
      - a JSON string representing a dict
      - a JSON string-scalar whose value is JSON text (needs json.loads twice)
    """
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        for _ in range(2):
            try:
                obj = json.loads(s)
            except Exception:
                return {}
            if isinstance(obj, dict):
                return obj
            if isinstance(obj, str):
                s = obj.strip()
                continue
            return {}
        return {}
    return {}


def _default_face_prompt_from_music(*, proj: Dict[str, Any], input_json: Dict[str, Any], which: str) -> str:
    """
    Deterministic, safe default face prompt for music videos.
    You can later make this richer (region/style/etc).
    """
    computed = _as_dict(input_json.get("computed"))
    hints = _as_dict(input_json.get("provider_hints"))

    # Allow caller overrides from payload
    key_prompt = f"{which}_face_prompt"
    p = (computed.get(key_prompt) or hints.get(key_prompt) or computed.get("performer_face_prompt") or hints.get("performer_face_prompt") or "").strip()
    if p:
        return p

    title = str(proj.get("title") or "Music Video").strip()
    lang = str(proj.get("language_hint") or "en").strip()

    # Neutral + broadly “Indian performer” but not overfitted; safe for demos.
    return (
        f"High-quality studio headshot portrait of an Indian performer for '{title}'. "
        f"Photorealistic, natural skin texture, cinematic soft lighting, centered, sharp focus, "
        f"neutral background, 4k. Language hint: {lang}. "
        f"Distinct person, unique face."
    )


def _svc_face_internal_bearer_token(bearer_token: Optional[str]) -> Optional[str]:
    """
    Prefer request bearer_token (API call path).
    Fallback to service token for worker context.

    NOTE: SvcFaceClient also has its own fallback, but keeping this helper
    makes the intent explicit and avoids surprising missing-token errors.
    """
    t = (bearer_token or "").strip()
    if t:
        return t
    # do NOT crash if Settings doesn't define it
    fb = getattr(settings, "SVC_FACE_BEARER_TOKEN", None)
    fb = (str(fb).strip() if fb else "")
    return fb or None


async def _ensure_performer_face_image_url(
    *,
    bearer_token: Optional[str],
    face_prompt: str,
    request_nonce: Optional[str] = None,
) -> str:
    """
    Generates a face via svc-face and returns a usable image_url (SAS).

    IMPORTANT:
    - Uses seed_mode=random and a request_nonce to avoid deterministic repeats.
    - Works in worker context via SVC_FACE_BEARER_TOKEN fallback.
    """
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

    # Use explicit timeouts if you’ve added them in compose/env; safe defaults otherwise.
    post_timeout_s = float(getattr(settings, "SVC_FACE_TIMEOUT_SECS", 60) or 60)
    poll_s = float(getattr(settings, "SVC_FACE_POLL_SECS", 2) or 2)
    wait_timeout_s = float(getattr(settings, "SVC_FACE_WAIT_TIMEOUT_SECS", 180) or 180)

    face_job_id = await face.create_creator_face_job(
        bearer_token=token,
        payload=payload,
        timeout_s=post_timeout_s,
        retries=0,  # keep 0 to avoid duplicate jobs if svc-face doesn't dedupe by request_nonce
    )

    res = await face.wait_for_creator_face(
        bearer_token=token,
        job_id=face_job_id,
        timeout_s=wait_timeout_s,
        poll_s=poll_s,
    )

    st = str(getattr(res, "status", "") or "").strip().lower()
    img = str(getattr(res, "image_url", "") or "").strip()

    # IMPORTANT: tolerate "jobstatus.succeeded"
    if ("succeeded" not in st) or not img:
        raise RuntimeError(
            f"svc-face failed or timed out: job_id={face_job_id} status={st} has_image={bool(img)}"
        )

    return img


def _safe_default_face_prompt_from_music(
    *, proj: Dict[str, Any], input_json: Dict[str, Any], which: str
) -> str:
    """
    Uses your existing _default_face_prompt_from_music() if present; otherwise a safe fallback.
    Prevents NameError and guarantees a usable prompt.
    """
    fn = globals().get("_default_face_prompt_from_music")
    if callable(fn):
        try:
            p = fn(proj=proj, input_json=input_json, which=which)
            p = str(p or "").strip()
            if p:
                return p
        except Exception:
            pass

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


async def _ensure_music_job_performer_faces(
    *,
    jobs: MusicJobsRepo,
    steps: StepsRepo,
    job_id: UUID,
    proj: Dict[str, Any],
    input_json: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Ensures performer face image URL(s) exist in input_json['computed'].
    Writes:
      computed.performer_a_image_url
      computed.performer_b_image_url (if duet layout)
      computed.performer_images = [..]
    """
    computed = _as_dict(input_json.get("computed"))
    duet_layout = str(proj.get("duet_layout") or "split_screen").strip().lower()

    # Expand safely if you add layouts later
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

    # Read current values
    a_url = str(computed.get("performer_a_image_url") or "").strip()
    b_url = str(computed.get("performer_b_image_url") or "").strip()

    # If duet not needed, ignore B in performer_images (but keep stored if present)
    if a_url and (b_url or not needs_two):
        computed["performer_images"] = [a_url] + ([b_url] if (needs_two and b_url) else [])
        input_json["computed"] = computed
        return input_json

    # Mark step running (best-effort)
    try:
        await steps.upsert_step(
            job_id=job_id,
            step_code="ensure_performer_faces",
            status="running",
            meta_json={"duet_layout": duet_layout, "needs_two": needs_two},
        )
    except Exception:
        pass

    # Worker path: no user JWT; _ensure_performer_face_image_url() will fallback to internal token.
    token: Optional[str] = None

    # Generate performer A if missing
    if not a_url:
        prompt_a = _safe_default_face_prompt_from_music(proj=proj, input_json=input_json, which="performer_a")
        a_url = await _ensure_performer_face_image_url(
            bearer_token=token,
            face_prompt=prompt_a,
            request_nonce=f"a_{uuid4().hex}",
        )
        computed["performer_a_image_url"] = a_url

    # Generate performer B if needed + missing
    if needs_two and not b_url:
        prompt_b = _safe_default_face_prompt_from_music(proj=proj, input_json=input_json, which="performer_b")
        # Nudge distinctness (minimal, won’t change overall semantics)
        prompt_b = f"{prompt_b} Different person from performer A."
        b_url = await _ensure_performer_face_image_url(
            bearer_token=token,
            face_prompt=prompt_b,
            request_nonce=f"b_{uuid4().hex}",
        )
        computed["performer_b_image_url"] = b_url

    # Always write performer_images deterministically
    computed["performer_images"] = [a_url] + ([b_url] if (needs_two and b_url) else [])
    input_json["computed"] = computed

    # Persist for downstream tools/pipeline (this is important; don't swallow failures)
    await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)

    # Mark step succeeded (best-effort)
    try:
        await steps.upsert_step(
            job_id=job_id,
            step_code="ensure_performer_faces",
            status="succeeded",
            meta_json={
                "performer_a": bool(a_url),
                "performer_b": bool(b_url) if needs_two else False,
                "needs_two": needs_two,
            },
        )
    except Exception:
        pass

    return input_json


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return []
        if s.startswith("[") or s.startswith("{"):
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, list) else []
            except Exception:
                return []
        return []
    return []


def _track_url(meta: Any) -> Optional[str]:
    # meta_json can be dict OR json-string depending on asyncpg codecs
    m = _as_dict(meta)
    return m.get("url") or m.get("byo_audio_url") or m.get("audio_master_url")


def _track_ct(meta: Any) -> Optional[str]:
    m = _as_dict(meta)
    return m.get("content_type") or m.get("mime")


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


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


def _guess_audio_content_type(url: Optional[str], default: str = "audio/mpeg") -> str:
    if not url:
        return default
    s = str(url).split("?", 1)[0].lower()
    if s.endswith(".wav"):
        return "audio/wav"
    if s.endswith(".mp3"):
        return "audio/mpeg"
    if s.endswith(".m4a") or s.endswith(".mp4"):
        return "audio/mp4"
    if s.endswith(".aac"):
        return "audio/aac"
    if s.endswith(".ogg") or s.endswith(".opus"):
        return "audio/ogg"
    return default


def _fallback_music_plan(*, mode: str, language: str | None, hints: Dict[str, Any]) -> Dict[str, Any]:
    """
    Tiny, deterministic fallback plan generator.
    Purpose: always provide a usable 'music_plan' even when MusicPlanningService is unavailable.
    Keep this compact + JSON-serializable (mobile can render this).
    """
    title = str(hints.get("title") or "Untitled").strip() or "Untitled"
    genre = str(hints.get("genre") or hints.get("genre_hint") or "pop").strip() or "pop"
    mood = str(hints.get("mood") or hints.get("vibe_hint") or "uplifting").strip() or "uplifting"
    tempo = str(hints.get("tempo") or "mid").strip() or "mid"
    style_refs = hints.get("style_refs") or hints.get("style_ref") or []

    # Normalize style refs into list[str]
    if isinstance(style_refs, str):
        s = style_refs.strip()
        if s and s.startswith("["):
            try:
                style_refs = json.loads(s)
            except Exception:
                style_refs = [style_refs]
        elif s:
            style_refs = [style_refs]
        else:
            style_refs = []
    if not isinstance(style_refs, list):
        style_refs = []

    if str(mode).lower() == MusicProjectMode.byo.value:
        steps = [
            {"step": "ingest_audio", "why": "Use your uploaded track as the master audio"},
            {"step": "lyrics_strategy", "why": "Lyrics optional unless timed_lyrics_json requested"},
            {"step": "alignment_optional", "why": "If timed lyrics requested, align lyrics to audio"},
            {"step": "publish", "why": "Prepare payload for Viewer/Fusion"},
        ]
    else:
        steps = [
            {"step": "creative_brief", "why": "Lock title/genre/mood/tempo"},
            {"step": "lyrics", "why": "Generate or use provided lyrics"},
            {"step": "arrangement", "why": "Define sections (intro/verse/chorus/bridge/outro)"},
            {"step": "provider_route", "why": "Choose provider based on availability/constraints"},
            {"step": "generate_audio", "why": "Produce full mix + stems if requested"},
            {"step": "align_lyrics_optional", "why": "Generate timed_lyrics_json if requested"},
            {"step": "publish", "why": "Prepare payload for Viewer/Fusion"},
        ]

    summary = f"{title} — {genre}, {mood}, tempo {tempo} ({language or 'en'})"

    return {
        "version": 1,
        "source": "fallback",
        "summary": summary,
        "mode": str(mode),
        "language": language,
        "brief": {
            "title": title,
            "genre": genre,
            "mood": mood,
            "tempo": tempo,
            "style_refs": [str(x) for x in style_refs if str(x).strip()],
        },
        "steps": steps,
        "notes": [
            "This is a lightweight fallback plan.",
            "If MusicPlanningService is enabled, its plan will replace this.",
        ],
    }


def _safe_stage(val: str | None) -> Optional[MusicJobStage]:
    if not val:
        return None
    try:
        return MusicJobStage(val)
    except Exception:
        return None


def _infer_stage_from_progress(progress_0_100: int) -> MusicJobStage:
    p = int(progress_0_100 or 0)
    if p < 10:
        return MusicJobStage.intent
    if p < 25:
        return MusicJobStage.creative_brief
    if p < 35:
        return MusicJobStage.lyrics
    if p < 45:
        return MusicJobStage.arrangement
    if p < 60:
        return MusicJobStage.provider_route
    if p < 75:
        return MusicJobStage.generate_audio
    if p < 82:
        return MusicJobStage.align_lyrics
    if p < 90:
        return MusicJobStage.generate_performer_videos
    if p < 97:
        return MusicJobStage.compose_video
    return MusicJobStage.publish


def _progress01(raw: Any) -> float:
    """
    Normalize progress to [0..1].
    Accepts DB values either as 0..100 or 0..1.
    """
    try:
        p = float(raw or 0)
    except Exception:
        return 0.0
    if p <= 0:
        return 0.0
    if p > 1.0:
        return min(1.0, p / 100.0)
    return min(1.0, p)


def _progress_for_stage(raw: Any) -> int:
    """
    Normalize to 0..100 integer for stage inference.
    """
    return int(round(_progress01(raw) * 100))


def _normalize_mode(val: Any) -> str:
    # Fix: allow enum inputs without producing "MusicProjectMode.autopilot"
    v = getattr(val, "value", val)
    s = str(v or "").strip()
    if not s:
        return MusicProjectMode.autopilot.value
    return s.lower()


def _normalize_outputs(input_json: Dict[str, Any]) -> List[str]:
    """
    input_json originates from payload.model_dump(mode="json"), so enums become strings.
    outputs should be a list[str] of MusicTrackType values.
    """
    outs = _as_list(input_json.get("outputs"))
    out_strs: List[str] = []
    for x in outs:
        if x is None:
            continue
        v = str(x).strip().lower()
        if not v:
            continue
        try:
            MusicTrackType(v)
            out_strs.append(v)
        except Exception:
            continue

    if not out_strs:
        out_strs = [MusicTrackType.full_mix.value]

    seen = set()
    dedup: List[str] = []
    for o in out_strs:
        if o not in seen:
            seen.add(o)
            dedup.append(o)
    return dedup


def _outputs_set(outputs: List[str]) -> set[str]:
    return {str(x).strip().lower() for x in (outputs or []) if x}


def _get_byo_audio(
    hints: Dict[str, Any], input_json: Dict[str, Any] | None = None
) -> Tuple[Optional[str], Optional[int]]:
    """
    Standardize BYO audio hint keys across earlier experiments.
    IMPORTANT: This is the *actual song audio* (not voice reference).
    """
    ij = input_json or {}
    url = (
        ij.get("uploaded_audio_url")
        or ij.get("audio_master_url")
        or hints.get("byo_audio_url")
        or hints.get("uploaded_audio_url")
        or hints.get("audio_url")
        or hints.get("audio_master_url")
    )
    dur = (
        ij.get("uploaded_audio_duration_ms")
        or ij.get("audio_master_duration_ms")
        or hints.get("byo_duration_ms")
        or hints.get("duration_ms")
        or hints.get("audio_master_duration_ms")
    )
    try:
        dur_i = int(dur) if dur is not None else None
    except Exception:
        dur_i = None
    return (str(url) if url else None, dur_i)


def _maybe_import_alignment():
    """
    Try to use your real lyrics_alignment_service implementation if present.
    Fallback to a deterministic naive implementation if not.
    """
    try:
        import app.services.lyrics_alignment_service as las  # type: ignore
    except Exception:
        las = None

    real = getattr(las, "align_lyrics", None) if las else None
    naive = getattr(las, "naive_timed_lyrics", None) if las else None

    if naive is None:

        def naive_timed_lyrics_fallback(
            lyrics_text: str, duration_ms: int, *, language: str | None = None
        ) -> Dict[str, Any]:
            duration_ms = max(1, int(duration_ms or 1))
            lines = [ln.strip() for ln in (lyrics_text or "").splitlines()]
            lines = [ln for ln in lines if ln]
            if not lines:
                return {"version": 1, "language": language, "segments": []}

            n = len(lines)
            base = duration_ms // n
            rem = duration_ms % n
            t = 0
            segments: List[Dict[str, Any]] = []
            for i, line in enumerate(lines):
                seg_dur = base + (1 if i < rem else 0)
                start = t
                end = min(duration_ms, t + seg_dur)
                t = end
                words = [w for w in line.split(" ") if w]
                if not words:
                    segments.append({"start_ms": start, "end_ms": end, "text": line, "words": []})
                    continue
                wn = len(words)
                wbase = max(1, (end - start) // wn)
                wrem = (end - start) - (wbase * wn)
                wt = start
                witems = []
                for wi, w in enumerate(words):
                    wdur = wbase + (1 if wi < wrem else 0)
                    wstart = wt
                    wend = min(end, wt + wdur)
                    wt = wend
                    witems.append({"w": w, "start_ms": wstart, "end_ms": wend})
                segments.append({"start_ms": start, "end_ms": end, "text": line, "words": witems})

            if segments:
                segments[-1]["end_ms"] = duration_ms
                if segments[-1]["words"]:
                    segments[-1]["words"][-1]["end_ms"] = duration_ms

            return {"version": 1, "language": language, "segments": segments}

        naive = naive_timed_lyrics_fallback

    return real, naive


# -----------------------------
# studio_jobs envelope helpers
# -----------------------------
async def _table_exists(*, pool, regclass_text: str) -> bool:
    try:
        v = await pool.fetchval("select to_regclass($1)", str(regclass_text))
        return v is not None
    except Exception:
        return False


async def _get_table_columns(*, pool, schema: str, table: str) -> set[str]:
    try:
        rows = await pool.fetch(
            """
            select column_name
            from information_schema.columns
            where table_schema=$1 and table_name=$2
            """,
            schema,
            table,
        )
        return {str(r["column_name"]) for r in (rows or []) if r and r.get("column_name")}
    except Exception:
        return set()


def _stable_json(obj: Any) -> str:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return str(obj)


def _studio_request_hash(*, user_id: UUID, studio_type: str, job_id: UUID, payload_json: Dict[str, Any]) -> str:
    base = {
        "user_id": str(user_id),
        "studio_type": str(studio_type),
        "job_id": str(job_id),
        "payload": payload_json or {},
    }
    return hashlib.sha256(_stable_json(base).encode("utf-8")).hexdigest()


def _studio_type_candidates() -> List[str]:
    vals = ["music", "MUSIC", "music_studio", "MUSIC_STUDIO", "svc_music", "SVC_MUSIC"]
    out: List[str] = []
    seen = set()
    for v in vals:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _studio_status_candidates(status: str) -> List[str]:
    s = str(status or "").strip()
    vals = [s, s.lower(), s.upper()]
    out: List[str] = []
    seen = set()
    for v in vals:
        v = str(v or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


async def _ensure_studio_job_envelope(
    *,
    pool,
    job_id: UUID,
    user_id: UUID,
    project_id: UUID | None,
    status: str,
    input_json: Dict[str, Any] | None = None,
    meta_json: Dict[str, Any] | None = None,
) -> None:
    if not await _table_exists(pool=pool, regclass_text="public.studio_jobs"):
        return

    try:
        exists = await pool.fetchval("select 1 from public.studio_jobs where id=$1 limit 1", job_id)
        if exists:
            return
    except Exception:
        pass

    cols = await _get_table_columns(pool=pool, schema="public", table="studio_jobs")
    required = {"id", "studio_type", "status", "request_hash", "payload_json", "meta_json", "user_id"}
    if not required.issubset(cols):
        return

    payload = input_json if isinstance(input_json, dict) else {}
    meta: Dict[str, Any] = dict(meta_json or {})
    meta.setdefault("source", "svc-music")
    meta.setdefault("request_type", "music_video")
    if project_id:
        meta.setdefault("music_project_id", str(project_id))

    for stype in _studio_type_candidates():
        rh = _studio_request_hash(user_id=user_id, studio_type=stype, job_id=job_id, payload_json=payload)
        for st in _studio_status_candidates(status or "queued"):
            try:
                await pool.execute(
                    """
                    insert into public.studio_jobs(
                        id, studio_type, status, request_hash, payload_json, meta_json, user_id
                    )
                    values($1,$2,$3,$4,coalesce($5,'{}'::jsonb),coalesce($6,'{}'::jsonb),$7)
                    on conflict (id) do nothing
                    """,
                    job_id,
                    stype,
                    st,
                    rh,
                    payload,
                    meta,
                    user_id,
                )
                return
            except Exception:
                continue

    return


async def _update_studio_job_status_best_effort(
    *,
    pool,
    job_id: UUID,
    status: str,
    error_message: str | None = None,
    meta_patch: Dict[str, Any] | None = None,
) -> None:
    if not await _table_exists(pool=pool, regclass_text="public.studio_jobs"):
        return

    cols = await _get_table_columns(pool=pool, schema="public", table="studio_jobs")
    if not cols:
        return

    for st in _studio_status_candidates(status):
        sets: List[str] = []
        params: List[Any] = []

        def set_param(col: str, val: Any) -> None:
            params.append(val)
            sets.append(f"{col}=${len(params) + 1}")  # +1 because id is $1

        if "status" in cols:
            set_param("status", st)

        if "updated_at" in cols:
            sets.append("updated_at=now()")

        if error_message:
            if "error_message" in cols:
                set_param("error_message", error_message)
            elif "error" in cols:
                set_param("error", error_message)

        if meta_patch and "meta_json" in cols:
            params.append(meta_patch)
            sets.append(f"meta_json=coalesce(meta_json,'{{}}'::jsonb) || ${len(params) + 1}::jsonb")

        if not sets:
            return

        try:
            await pool.execute(
                f"""
                update public.studio_jobs
                set {", ".join(sets)}
                where id=$1
                """,
                job_id,
                *params,
            )
            return
        except Exception:
            continue

    return


async def _persist_fusion_payload_best_effort(*, job_id: UUID, fusion_payload: Dict[str, Any]) -> None:
    """
    Persist fusion_payload into public.studio_jobs.payload_json for dashboard access.
    Safe no-op if studio_jobs/payload_json isn't present.
    """
    pool = await get_pool()
    if not await _table_exists(pool=pool, regclass_text="public.studio_jobs"):
        return

    cols = await _get_table_columns(pool=pool, schema="public", table="studio_jobs")
    if "payload_json" not in cols:
        return

    try:
        await pool.execute(
            """
            update public.studio_jobs
            set payload_json = jsonb_set(
                    case
                        when payload_json is null then '{}'::jsonb
                        when jsonb_typeof(payload_json) = 'object' then payload_json
                        when jsonb_typeof(payload_json) = 'string'
                         and left(payload_json #>> '{}', 1) in ('{','[')
                        then (payload_json #>> '{}')::jsonb
                        else '{}'::jsonb
                    end,
                    '{fusion_payload}',
                    $2::jsonb,
                    true
                ),
                updated_at = now()
            where id = $1
            """,
            job_id,
            json.dumps(fusion_payload),
        )
    except Exception:
        # Never fail publish because dashboard persistence failed.
        return


# -----------------------------
# Voice reference resolution (fresh SAS)
# -----------------------------
def _fallback_input_container() -> str:
    return (getattr(settings, "MUSIC_INPUT_CONTAINER", None) or "music-input").strip() or "music-input"


def _fallback_output_container() -> str:
    return (getattr(settings, "MUSIC_OUTPUT_CONTAINER", None) or "music-output").strip() or "music-output"


def _extract_container_and_path_from_meta(meta_json: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    meta_json may be TEXT or dict.
    Preferred new format:
      {"container": "music-input", "storage_path": "..."}
    Back-compat:
      {"storage_path": "..."}  (container missing)
    """
    m = _as_dict(meta_json)
    c = m.get("container") or m.get("blob_container")
    p = m.get("storage_path") or m.get("path")
    c = str(c).strip() if isinstance(c, str) and c.strip() else None
    p = str(p).strip() if isinstance(p, str) and p.strip() else None
    return c, p


async def _update_media_asset_refs_best_effort(
    *,
    pool,
    asset_id: UUID,
    new_storage_ref: str,
    container: Optional[str],
    storage_path: Optional[str],
) -> None:
    """
    media_assets.meta_json is JSONB.
    Merge container/storage_path into existing meta_json (best effort), then update storage_ref.
    """
    existing_meta: Any = {}
    try:
        r = await pool.fetchrow("select meta_json from public.media_assets where id=$1", asset_id)
        if r and r.get("meta_json") is not None:
            existing_meta = r["meta_json"]
    except Exception:
        existing_meta = {}

    meta_obj = _as_dict(existing_meta)

    if container and not meta_obj.get("container"):
        meta_obj["container"] = container
    if storage_path and not meta_obj.get("storage_path"):
        meta_obj["storage_path"] = storage_path

    try:
        await pool.execute(
            """
            update public.media_assets
            set storage_ref=$2, meta_json=$3, updated_at=now()
            where id=$1
            """,
            asset_id,
            new_storage_ref,
            meta_obj if meta_obj else {},
        )
    except Exception:
        await pool.execute(
            """
            update public.media_assets
            set storage_ref=$2, updated_at=now()
            where id=$1
            """,
            asset_id,
            new_storage_ref,
        )


def _resolve_container_and_path(
    *, storage_ref: str | None, meta_json: Any
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Returns (container, blob_path, meta_container, url_container) for diagnostics / ordering.

    Resolution order:
      - container: meta_json.container, else URL container, else fallback input/output
      - path: meta_json.storage_path, else URL blob_path
    """
    meta_container, meta_path = _extract_container_and_path_from_meta(meta_json)

    url_container, url_path = (None, None)
    if storage_ref:
        try:
            url_container, url_path = AzureStorageService.parse_blob_url(storage_ref)
        except Exception:
            url_container, url_path = (None, None)

    blob_path = meta_path or url_path

    container = meta_container or url_container
    if not container and blob_path:
        # default container is chosen by caller; leave None here to allow caller selection.
        container = None

    return container, blob_path, meta_container, url_container


async def _resolve_voice_ref_sas_url(
    *, project_id: UUID, user_id: UUID, voice_ref_asset_id: UUID | None
) -> Optional[str]:
    """
    Resolve a fresh SAS URL for voice reference (NOT song audio).
    """
    pool = await get_pool()

    if not voice_ref_asset_id:
        rowp = await pool.fetchrow(
            "select voice_ref_asset_id from public.music_projects where id=$1 and user_id=$2",
            project_id,
            user_id,
        )
        if not rowp or not rowp["voice_ref_asset_id"]:
            return None
        voice_ref_asset_id = UUID(str(rowp["voice_ref_asset_id"]))

    asset = await pool.fetchrow(
        """
        select id, storage_ref, meta_json
        from public.media_assets
        where id=$1 and user_id=$2
        limit 1
        """,
        voice_ref_asset_id,
        user_id,
    )
    if not asset:
        return None

    storage_ref = str(asset.get("storage_ref") or "") or None
    meta_json = asset.get("meta_json")

    container, blob_path, meta_container, url_container = _resolve_container_and_path(
        storage_ref=storage_ref,
        meta_json=meta_json,
    )

    if not blob_path:
        return storage_ref or None

    candidates: List[str] = []
    for c in (meta_container, url_container):
        if c and c not in candidates:
            candidates.append(c)
    for c in (_fallback_input_container(), _fallback_output_container()):
        if c and c not in candidates:
            candidates.append(c)

    for c in candidates:
        try:
            storage = AzureStorageService(container=c)
            refreshed = storage.sas_url_for(blob_path)

            await _update_media_asset_refs_best_effort(
                pool=pool,
                asset_id=voice_ref_asset_id,
                new_storage_ref=refreshed,
                container=c,
                storage_path=blob_path,
            )
            return refreshed
        except Exception:
            continue

    return storage_ref or None


# -----------------------------
# FULL MIX URL resolution (production-grade)
# -----------------------------
async def _resolve_full_mix_audio_url_from_track(*, project_id: UUID, user_id: UUID | None = None) -> Optional[str]:
    """
    Resolve a *fresh* SAS URL for the full_mix audio by reading music_tracks refs.
    Priority:
      1) music_tracks.media_asset_id -> media_assets(storage_ref/meta_json) -> refresh SAS using container+storage_path if possible
      2) music_tracks.artifact_id    -> music_artifacts(storage_path) -> SAS via MUSIC_OUTPUT_CONTAINER
      3) fallback to stored media_assets.storage_ref if it is already a URL
    """
    pool = await get_pool()

    track = await pool.fetchrow(
        """
        select media_asset_id, artifact_id
        from public.music_tracks
        where project_id=$1 and track_type='full_mix'
        limit 1
        """,
        project_id,
    )
    if not track:
        return None

    media_asset_id = track.get("media_asset_id")
    if media_asset_id:
        try:
            if user_id:
                ma = await pool.fetchrow(
                    "select id, user_id, storage_ref, meta_json from public.media_assets where id=$1 and user_id=$2",
                    media_asset_id,
                    user_id,
                )
            else:
                ma = await pool.fetchrow(
                    "select id, user_id, storage_ref, meta_json from public.media_assets where id=$1",
                    media_asset_id,
                )

            if ma:
                storage_ref = str(ma.get("storage_ref") or "") or None
                meta_json = ma.get("meta_json")

                container, blob_path, meta_container, url_container = _resolve_container_and_path(
                    storage_ref=storage_ref,
                    meta_json=meta_json,
                )

                # Full-mix is output by default, but allow meta/url container if present.
                candidates: List[str] = []
                for c in (meta_container, url_container, container):
                    if c and c not in candidates:
                        candidates.append(c)
                for c in (_fallback_output_container(), _fallback_input_container()):
                    if c and c not in candidates:
                        candidates.append(c)

                if blob_path:
                    for c in candidates:
                        try:
                            storage = AzureStorageService(container=c)
                            refreshed = storage.sas_url_for(blob_path)
                            try:
                                await _update_media_asset_refs_best_effort(
                                    pool=pool,
                                    asset_id=UUID(str(media_asset_id)),
                                    new_storage_ref=refreshed,
                                    container=c,
                                    storage_path=blob_path,
                                )
                            except Exception:
                                pass
                            return refreshed
                        except Exception:
                            continue

                # If we can't refresh, last resort: return stored URL if present (may still be valid).
                if storage_ref and storage_ref.startswith("http"):
                    return storage_ref
        except Exception:
            pass

    artifact_id = track.get("artifact_id")
    if artifact_id:
        try:
            r = await pool.fetchrow(
                "select storage_path from public.music_artifacts where id=$1",
                artifact_id,
            )
            sp = str(r["storage_path"]).strip() if r and r.get("storage_path") else None
            if sp:
                storage = AzureStorageService(container=_fallback_output_container())
                return storage.sas_url_for(sp)
        except Exception:
            pass

    return None


async def _resolve_url_from_refs(
    *, user_id: UUID, media_asset_id: UUID | None, artifact_id: UUID | None
) -> Optional[str]:
    """
    Best-effort URL resolution:
      - media_assets: refresh SAS using meta_json.container + storage_path if available; else return storage_ref if URL
      - music_artifacts: SAS from storage_path via MUSIC_OUTPUT_CONTAINER
    """
    pool = await get_pool()

    if media_asset_id:
        try:
            r = await pool.fetchrow(
                "select storage_ref, meta_json from public.media_assets where id=$1 and user_id=$2",
                media_asset_id,
                user_id,
            )
            if r:
                storage_ref = str(r.get("storage_ref") or "") or None
                container, blob_path, meta_container, url_container = _resolve_container_and_path(
                    storage_ref=storage_ref,
                    meta_json=r.get("meta_json"),
                )

                candidates: List[str] = []
                for c in (meta_container, url_container, container):
                    if c and c not in candidates:
                        candidates.append(c)
                for c in (_fallback_output_container(), _fallback_input_container()):
                    if c and c not in candidates:
                        candidates.append(c)

                if blob_path:
                    for c in candidates:
                        try:
                            storage = AzureStorageService(container=c)
                            refreshed = storage.sas_url_for(blob_path)
                            try:
                                await _update_media_asset_refs_best_effort(
                                    pool=pool,
                                    asset_id=UUID(str(media_asset_id)),
                                    new_storage_ref=refreshed,
                                    container=c,
                                    storage_path=blob_path,
                                )
                            except Exception:
                                pass
                            return refreshed
                        except Exception:
                            continue

                if storage_ref and storage_ref.startswith("http"):
                    return storage_ref
        except Exception:
            pass

    if artifact_id:
        try:
            r = await pool.fetchrow(
                "select storage_path from public.music_artifacts where id=$1",
                artifact_id,
            )
            sp = str(r["storage_path"]).strip() if r and r.get("storage_path") else None
            if sp:
                storage = AzureStorageService(container=_fallback_output_container())
                return storage.sas_url_for(sp)
        except Exception:
            pass

    return None


# -----------------------------
# Status + Publish (API helpers)
# -----------------------------
async def get_video_job_status(*, job_id: UUID, user_id: UUID) -> Optional[MusicJobStatusOut]:
    jobs = MusicJobsRepo()
    projects = MusicProjectsRepo()
    tracks_repo = MusicTracksRepo()
    steps = StepsRepo()

    job = await jobs.get_video_job(job_id=job_id)
    if not job:
        return None

    payload = _normalize_jsonb_payload(job.get("payload_json"))

    computed: Dict[str, Any] = _as_dict(payload.get("computed"))
    clip_manifest_raw = computed.get("clip_manifest") or payload.get("clip_manifest")
    clip_manifest_dict = clip_manifest_raw if isinstance(clip_manifest_raw, dict) else _as_dict(clip_manifest_raw)
    clip_manifest: Optional[Dict[str, Any]] = clip_manifest_dict if clip_manifest_dict else None

    proj = await projects.get(project_id=job["project_id"], user_id=user_id)
    if not proj:
        return None

    track_rows = await tracks_repo.list_by_project(project_id=job["project_id"])
    last = await steps.latest_step(job_id=job_id)

    progress01 = _progress01(job.get("progress"))
    stage_progress = _progress_for_stage(job.get("progress"))
    stage = _safe_stage(last["step_code"] if last else None) or _infer_stage_from_progress(stage_progress)

    return MusicJobStatusOut(
        job_id=job["id"],
        project_id=job["project_id"],
        status=job["status"],
        stage=stage,
        progress=progress01,
        tracks=[
            TrackItem(
                track_type=r["track_type"],
                artifact_id=r.get("artifact_id"),
                media_asset_id=r.get("media_asset_id"),
                duration_ms=r.get("duration_ms"),
                url=_track_url(r.get("meta_json")),
                content_type=_track_ct(r.get("meta_json")),
            )
            for r in track_rows
        ],
        error=job.get("error"),
        computed=computed,
        clip_manifest=clip_manifest,
    )


async def publish_project_to_video_or_fusion(
    *, job_id: UUID, user_id: UUID, publish_in: PublishMusicIn
) -> Optional[PublishMusicOut]:
    """
    Production-grade publish:
      - Enforces consent
      - Resolves FULL MIX URL from music_tracks refs (media_assets / music_artifacts) with fresh SAS
      - Persists fusion_payload into studio_jobs.payload_json for dashboards
    """
    jobs = MusicJobsRepo()
    projects = MusicProjectsRepo()
    tracks_repo = MusicTracksRepo()

    job = await jobs.get_video_job(job_id=job_id)
    if not job:
        return None

    proj = await projects.get(project_id=job["project_id"], user_id=user_id)
    if not proj:
        return None

    consent_dict = _as_dict(getattr(publish_in, "consent", None))
    if not _is_truthy(consent_dict.get("accepted")):
        return PublishMusicOut(status="error_consent_required", video_job_id=job_id, fusion_payload=None)

    target = str(getattr(publish_in, "target", "fusion") or "fusion").strip().lower()
    if target not in ("viewer", "fusion"):
        target = "fusion"

    if str(job["status"]) == MusicJobStatus.failed.value:
        return PublishMusicOut(status="error_job_failed", video_job_id=job_id, fusion_payload=None)

    if str(job["status"]) != MusicJobStatus.succeeded.value:
        return PublishMusicOut(status="error_job_not_ready", video_job_id=job_id, fusion_payload=None)

    input_json = _as_dict(job.get("input_json"))
    hints = _as_dict(input_json.get("provider_hints"))
    computed = _as_dict(input_json.get("computed"))

    # Start with any hinted BYO URL (legacy), but we will override with track refs if present.
    audio_url, _ = _get_byo_audio(hints, input_json)
    if not audio_url:
        audio_url = computed.get("audio_master_url") or computed.get("byo_audio_url") or computed.get("demo_audio_url")

    tracks = await tracks_repo.list_by_project(project_id=job["project_id"])

    def find_track(tt: str):
        for t in tracks:
            if str(t.get("track_type") or "") == tt:
                return t
        return None

    full = find_track(MusicTrackType.full_mix.value)
    timed = find_track(MusicTrackType.timed_lyrics_json.value)

    if not full:
        return PublishMusicOut(status="error_missing_full_mix", video_job_id=job_id, fusion_payload=None)

    # 1) If meta_json already has url, it is acceptable (but may be stale). Prefer refreshing from refs.
    # 2) If refs exist, resolve fresh SAS from media_assets/music_artifacts.
    resolved_from_track: Optional[str] = None
    try:
        if full.get("media_asset_id") or full.get("artifact_id"):
            resolved_from_track = await _resolve_full_mix_audio_url_from_track(
                project_id=UUID(str(proj["id"])),
                user_id=user_id,
            )
    except Exception:
        resolved_from_track = None

    track_url = _track_url(full.get("meta_json"))
    if resolved_from_track:
        audio_url = resolved_from_track
    elif track_url:
        audio_url = track_url

    # Final fallback: resolve from refs directly
    if not audio_url:
        try:
            audio_url = await _resolve_url_from_refs(
                user_id=user_id,
                media_asset_id=full.get("media_asset_id"),
                artifact_id=full.get("artifact_id"),
            )
        except Exception:
            audio_url = None

    has_audio_ref = bool(audio_url or full.get("artifact_id") or full.get("media_asset_id"))
    if not has_audio_ref:
        return PublishMusicOut(status="error_missing_full_mix_ref", video_job_id=job_id, fusion_payload=None)

    # Voice reference (fresh SAS) — optional
    voice_ref_asset_id = None
    try:
        if proj.get("voice_ref_asset_id"):
            voice_ref_asset_id = str(proj["voice_ref_asset_id"])
        elif input_json.get("voice_ref_asset_id"):
            voice_ref_asset_id = str(input_json.get("voice_ref_asset_id"))
    except Exception:
        voice_ref_asset_id = None

    voice_ref_url = computed.get("voice_ref_url")
    if voice_ref_asset_id:
        try:
            vr_uuid = UUID(str(voice_ref_asset_id))
        except Exception:
            vr_uuid = None
        if vr_uuid:
            try:
                fresh = await _resolve_voice_ref_sas_url(
                    project_id=UUID(str(proj["id"])),
                    user_id=user_id,
                    voice_ref_asset_id=vr_uuid,
                )
                if fresh:
                    voice_ref_url = fresh
            except Exception:
                pass

    # Duration: prefer track row; if empty/zero try media_assets.duration_ms (best effort)
    duration_ms = int(full.get("duration_ms") or 0)
    if duration_ms <= 0 and full.get("media_asset_id"):
        try:
            pool = await get_pool()
            d = await pool.fetchval(
                "select duration_ms from public.media_assets where id=$1 and user_id=$2",
                full.get("media_asset_id"),
                user_id,
            )
            if d:
                duration_ms = int(d)
        except Exception:
            pass

    base_payload = {
        "project_id": str(proj["id"]),
        "audio": {
            "track_type": MusicTrackType.full_mix.value,
            "artifact_id": str(full["artifact_id"]) if full.get("artifact_id") else None,
            "media_asset_id": str(full["media_asset_id"]) if full.get("media_asset_id") else None,
            "url": audio_url,
            "duration_ms": duration_ms,
            "content_type": _guess_audio_content_type(audio_url, default=_track_ct(full.get("meta_json")) or "audio/mpeg"),
        },
        "voice_reference": {"voice_ref_asset_id": voice_ref_asset_id, "url": voice_ref_url}
        if (voice_ref_asset_id or voice_ref_url)
        else None,
        "lyrics_text": computed.get("lyrics_text") or hints.get("lyrics_text") or hints.get("lyrics"),
        "timed_lyrics": {"artifact_id": str(timed["artifact_id"])} if timed and timed.get("artifact_id") else None,
        "timed_lyrics_inline": computed.get("timed_lyrics_json"),
        "duet_layout": proj["duet_layout"],
        "language_hint": proj.get("language_hint"),
        "target": target,
        "consent": consent_dict,
    }

    # Persist for dashboards (your stored_audio_url query)
    try:
        await _persist_fusion_payload_best_effort(job_id=job_id, fusion_payload=base_payload)
    except Exception:
        pass

    if target == "viewer":
        return PublishMusicOut(status="published_viewer", video_job_id=job_id, fusion_payload=base_payload)

    return PublishMusicOut(status="published", video_job_id=job_id, fusion_payload=base_payload)


# -----------------------------
# Worker entrypoint
# -----------------------------
async def run_music_video_job(job_id: UUID) -> None:
    jobs = MusicJobsRepo()
    tracks_repo = MusicTracksRepo()
    steps = StepsRepo()

    job = await jobs.get_video_job(job_id=job_id)
    if not job:
        return

    job_status = str(job.get("status") or "").strip()

    pool = await get_pool()
    input_json = _as_dict(job.get("input_json"))

    proj_row = await pool.fetchrow("select * from music_projects where id=$1", job["project_id"])
    if not proj_row:
        if job_status in (MusicJobStatus.succeeded.value, MusicJobStatus.failed.value):
            return
        await jobs.set_video_job_failed(job_id=job_id, error="project_not_found")
        return

    proj = dict(proj_row)
    proj_user_id = UUID(str(proj["user_id"]))
    proj_id = UUID(str(proj["id"]))

    # ALWAYS ensure studio_jobs envelope (even if job already succeeded/failed)
    try:
        current_status = job_status or "queued"
        await _ensure_studio_job_envelope(
            pool=pool,
            job_id=job_id,
            user_id=proj_user_id,
            project_id=proj_id,
            status=current_status,
            input_json=input_json,
            meta_json={"source": "svc-music", "music_project_id": str(proj_id), "request_type": "music_video"},
        )
        await _update_studio_job_status_best_effort(
            pool=pool,
            job_id=job_id,
            status=current_status,
            meta_patch={"svc": "svc-music", "music_project_id": str(proj_id)},
        )
    except Exception:
        pass

    if job_status in (MusicJobStatus.succeeded.value, MusicJobStatus.failed.value):
        return

    await jobs.set_video_job_running(job_id=job_id)

    try:
        await _update_studio_job_status_best_effort(
            pool=pool,
            job_id=job_id,
            status="running",
            meta_patch={"svc": "svc-music", "music_project_id": str(proj_id)},
        )
    except Exception:
        pass

    computed_pre = _as_dict(input_json.get("computed"))

    vr_raw = input_json.get("voice_ref_asset_id") or proj.get("voice_ref_asset_id")
    try:
        vr_id = UUID(str(vr_raw)) if vr_raw else None
    except Exception:
        vr_id = None

    try:
        voice_ref_url = await _resolve_voice_ref_sas_url(
            project_id=proj_id,
            user_id=proj_user_id,
            voice_ref_asset_id=vr_id,
        )
    except Exception:
        voice_ref_url = None

    if computed_pre.get("voice_ref_url") != voice_ref_url:
        computed_pre["voice_ref_url"] = voice_ref_url
        input_json["computed"] = computed_pre
        await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)

    # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
    # NEW: Ensure performer face image(s) exist via svc-face BEFORE pipeline runs
    # This is the svc-music -> svc-face "handoff" you asked for.
    # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
    try:
        input_json = await _ensure_music_job_performer_faces(
            jobs=jobs,
            steps=steps,
            job_id=job_id,
            proj=proj,
            input_json=input_json,
        )
    except Exception as e:
        # If faces are truly required for your pipeline, you may want to fail hard here.
        # For now: fail hard, because "hand-off to svc-face" is a pipeline prerequisite.
        await jobs.set_video_job_failed(job_id=job_id, error=f"ensure_performer_faces_failed:{e}")
        try:
            await _update_studio_job_status_best_effort(
                pool=pool,
                job_id=job_id,
                status="failed",
                error_message=f"ensure_performer_faces_failed:{e}",
                meta_patch={"music_project_id": str(proj_id), "svc": "svc-music"},
            )
        except Exception:
            pass
        return
    # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

    try:
        computed_before = json.loads(json.dumps(_as_dict(input_json.get("computed"))))
    except Exception:
        computed_before = dict(_as_dict(input_json.get("computed")))

    requested_outputs = _normalize_outputs(input_json)

    state = MusicGraphState(
        job_id=job_id,
        project_id=proj["id"],
        user_id=proj["user_id"],
        mode=_normalize_mode(proj.get("mode")),
        duet_layout=str(proj.get("duet_layout") or "split_screen").lower(),
        language_hint=proj.get("language_hint") or "en-IN",
        scene_pack_id=proj.get("scene_pack_id"),
        camera_edit=str(proj.get("camera_edit") or "beat_cut").lower(),
        band_pack=proj.get("band_pack") or [],
        requested_outputs=requested_outputs,
    )

    tools = ConcreteMusicTools(
        job_id=job_id,
        project_id=state.project_id,
        user_id=state.user_id,
        input_json=input_json,
    )

    try:
        state = await run_video_pipeline(state, tools, jobs=jobs, steps=steps)

        computed = _as_dict(input_json.get("computed"))

        for t in state.tracks:
            tt = str(getattr(t, "track_type", ""))
            meta = getattr(t, "meta", None)

            if tt == MusicTrackType.full_mix.value and isinstance(meta, dict):
                am = meta.get("audio_master_url")
                if am:
                    computed["audio_master_url"] = am
                    computed["byo_audio_url"] = am

                demo = meta.get("demo_audio_url")
                if demo:
                    computed["demo_audio_url"] = demo

                dur = meta.get("audio_duration_ms") or meta.get("byo_duration_ms")
                if dur:
                    try:
                        computed["audio_master_duration_ms"] = int(dur)
                    except Exception:
                        pass

            if tt == MusicTrackType.timed_lyrics_json.value and isinstance(meta, dict):
                inline = meta.get("inline_json")
                if inline:
                    computed["timed_lyrics_json"] = inline

        tool_computed = tools._computed()
        for k in (
            "lyrics_text",
            "lyrics_source_effective",
            "music_plan",
            "plan_summary",
            "voice_ref_url",
            "audio_provider",
            "provider_request_id",
            "autopilot_provider_error",
        ):
            if k in tool_computed and tool_computed.get(k) is not None:
                computed[k] = tool_computed.get(k)

        if computed != computed_before:
            input_json["computed"] = computed
            await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)

        for t in state.tracks:
            await tracks_repo.upsert_track(
                project_id=state.project_id,
                track_type=t.track_type,
                duration_ms=int(t.duration_ms or 0),
                artifact_id=t.artifact_id,
                media_asset_id=t.media_asset_id,
                meta_json=(t.meta if isinstance(t.meta, dict) else None),
            )

        await jobs.set_video_job_succeeded(
            job_id=job_id,
            preview_video_asset_id=state.preview_video_asset_id,
            final_video_asset_id=state.final_video_asset_id,
            performer_a_video_asset_id=state.performer_a_video_asset_id,
            performer_b_video_asset_id=state.performer_b_video_asset_id,
        )

        try:
            await _update_studio_job_status_best_effort(
                pool=pool,
                job_id=job_id,
                status="succeeded",
                meta_patch={"music_project_id": str(proj_id), "svc": "svc-music"},
            )
        except Exception:
            pass

    except Exception as e:
        try:
            await steps.upsert_step(
                job_id=job_id,
                step_code="failed",
                status="failed",
                meta_json={"error": str(e)},
            )
        except Exception:
            pass

        await jobs.set_video_job_failed(job_id=job_id, error=str(e))

        try:
            await _update_studio_job_status_best_effort(
                pool=pool,
                job_id=job_id,
                status="failed",
                error_message=str(e),
                meta_patch={"music_project_id": str(proj_id), "svc": "svc-music"},
            )
        except Exception:
            pass


async def run_compose_job(job_id: UUID) -> None:
    await run_music_video_job(job_id)