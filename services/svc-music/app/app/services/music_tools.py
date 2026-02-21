from __future__ import annotations

import os
import inspect
import json
import logging
import shutil
import subprocess
import time
import wave
import base64
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.config import settings
from app.domain.enums import MusicProjectMode, MusicTrackType
from app.services.azure_storage_service import AzureStorageService

from .music_graph import GraphTrack, MusicGraphState, MusicGraphTools

logger = logging.getLogger(__name__)

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
        return "native"

    compose_full_mix_fal_sonauto_v2 = None  # type: ignore


# -----------------------------
# Core auth cache (in-memory, worker process)
# -----------------------------
_CORE_TOKEN: str | None = None
_CORE_TOKEN_EXP: float = 0.0
_CORE_REFRESH: str | None = None


# -----------------------------
# Helpers
# -----------------------------
JsonDict = Dict[str, Any]


def _as_dict(x: Any) -> JsonDict:
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


def _as_dict_loose(x: Any) -> JsonDict:
    """
    Handles:
      - dict
      - JSON string of dict
      - JSON string-scalar whose value is JSON text (double json.loads)
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


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, list) else []
            except Exception:
                return []
        return [s]
    return []


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


def _jwt_exp_epoch_seconds(token: str) -> float:
    """
    Best-effort parse of JWT exp without PyJWT dependency.
    Returns 0.0 if unknown.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return 0.0
        payload_b64 = parts[1]
        pad = "=" * (-len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode((payload_b64 + pad).encode("utf-8"))
        obj = json.loads(payload.decode("utf-8"))
        exp = obj.get("exp")
        if exp is None:
            return 0.0
        return float(exp)
    except Exception:
        return 0.0


def _running_in_docker() -> bool:
    return os.path.exists("/.dockerenv")


def _default_fusion_base() -> str:
    # inside docker network -> service DNS; on host -> localhost
    return "http://svc-fusion:8002" if _running_in_docker() else "http://localhost:8002"


def _get_fusion_base() -> str:
    base = (os.getenv("DF_FUSION_URL") or os.getenv("FUSION_URL") or "").strip()
    return (base or _default_fusion_base()).rstrip("/")


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


def _first_http_url(*vals: Any) -> str:
    for v in vals:
        s = str(v or "").strip()
        if s.startswith("http"):
            return s
    return ""


def _first_nonempty(*vals: Any) -> str:
    for v in vals:
        s = str(v or "").strip()
        if s:
            return s
    return ""


async def _get_core_access_token(*, force_refresh: bool = False) -> str:
    """
    Product-grade runtime token minting for service-to-service calls.

    Preference order:
      1) DF_SERVICE_REFRESH_TOKEN (long-lived) -> /api/auth/refresh
      2) cached refresh_token from prior login
      3) DF_SERVICE_EMAIL + DF_SERVICE_PASSWORD -> /api/auth/login
    """
    global _CORE_TOKEN, _CORE_TOKEN_EXP, _CORE_REFRESH

    try:
        import httpx  # type: ignore
    except Exception as e:
        raise RuntimeError("httpx_missing_required_for_core_auth") from e

    now = time.time()
    if not force_refresh and _CORE_TOKEN and now < (_CORE_TOKEN_EXP - 30):
        return _CORE_TOKEN

    core = (os.getenv("DF_CORE_URL") or "http://svc-core:8000").rstrip("/")

    env_refresh = (os.getenv("DF_SERVICE_REFRESH_TOKEN") or "").strip()
    if env_refresh:
        _CORE_REFRESH = env_refresh

    # Try refresh first if we have one
    if _CORE_REFRESH:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{core}/api/auth/refresh",
                    headers={"Content-Type": "application/json"},
                    json={"refresh_token": _CORE_REFRESH},
                )
                if r.status_code < 400:
                    j = r.json()
                    tok = str(j.get("access_token") or j.get("token") or "").strip()
                    if tok:
                        exp_epoch = _jwt_exp_epoch_seconds(tok)
                        if exp_epoch > 0:
                            _CORE_TOKEN_EXP = exp_epoch
                        else:
                            exp_in = int(j.get("expires_in") or 900)
                            _CORE_TOKEN_EXP = time.time() + max(60, exp_in)
                        _CORE_TOKEN = tok
                        return _CORE_TOKEN
        except Exception:
            pass

    email = (os.getenv("DF_SERVICE_EMAIL") or "").strip()
    password = (os.getenv("DF_SERVICE_PASSWORD") or "").strip()

    # IMPORTANT: don't ever default to client_type="service" (your DB constraint rejects it)
    client_type = (os.getenv("DF_AUTH_CLIENT_TYPE") or "web").strip()
    device_id = (os.getenv("DF_AUTH_DEVICE_ID") or "svc-music-worker").strip()

    if not email or not password:
        raise RuntimeError("missing_service_account_creds_set_DF_SERVICE_EMAIL_DF_SERVICE_PASSWORD_or_DF_SERVICE_REFRESH_TOKEN")

    payload = {
        "email": email,
        "password": password,
        "device_id": device_id,
        "client_type": client_type,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{core}/api/auth/login",
            headers={"Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"core_login_failed code={r.status_code} email={email} pass_len={len(password)} body={r.text[:240]}"
            )

        j = r.json()
        tok = str(j.get("access_token") or j.get("token") or "").strip()
        if not tok:
            raise RuntimeError("core_login_missing_access_token")

        _CORE_REFRESH = str(j.get("refresh_token") or "").strip() or _CORE_REFRESH

        exp_epoch = _jwt_exp_epoch_seconds(tok)
        if exp_epoch > 0:
            _CORE_TOKEN_EXP = exp_epoch
        else:
            exp_in = int(j.get("expires_in") or 900)
            _CORE_TOKEN_EXP = time.time() + max(60, exp_in)

        _CORE_TOKEN = tok
        return _CORE_TOKEN


async def _get_fusion_bearer_token() -> Tuple[str, str]:
    """
    Returns (authorization_header_value, source)
      - If DF_FUSION_BEARER_TOKEN / DF_INTERNAL_BEARER_TOKEN exists, use it
      - Else mint via svc-core (refresh or login), cache in-memory
    """
    raw = (
        (os.getenv("DF_INTERNAL_BEARER_TOKEN") or "").strip()
        or (os.getenv("DF_FUSION_BEARER_TOKEN") or "").strip()
        or (os.getenv("DF_AUTH_TOKEN") or "").strip()
        or (os.getenv("BEARER_TOKEN") or "").strip()
    )

    if raw:
        tok = raw
        if not tok.lower().startswith("bearer "):
            tok = f"Bearer {tok}"
        return tok, "env"

    tok = await _get_core_access_token()
    return f"Bearer {tok}", "core"


def _fusion_headers(*, user_id: str, authorization: str) -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": authorization,
        "X-User-Id": user_id,
        "X-Request-Source": "svc-music",
    }


def _extract_fusion_job_id(obj: Dict[str, Any]) -> str:
    for k in ("job_id", "id"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _extract_fusion_status(obj: Dict[str, Any]) -> str:
    v = obj.get("status")
    return str(v or "").strip().lower()


def _extract_fusion_video_url(obj: Dict[str, Any]) -> Optional[str]:
    arts = obj.get("artifacts")
    if isinstance(arts, list):
        for a in arts:
            if not isinstance(a, dict):
                continue
            url = str(a.get("url") or "").strip()
            kind = str(a.get("kind") or "").lower()
            if url.startswith("http") and ("mp4" in url.lower() or "video" in kind):
                return url

    for k in ("video_url", "final_url", "url", "preview_url", "mp4_url"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip().startswith("http"):
            return v.strip()

    return None


def _outputs_set(outputs: List[str]) -> set[str]:
    return {str(x).strip().lower() for x in (outputs or []) if x}


def _normalize_mode(val: Any) -> str:
    v = getattr(val, "value", val)
    s = str(v or "").strip()
    if not s:
        return MusicProjectMode.autopilot.value
    return s.lower()


def _get_byo_audio(hints: Dict[str, Any], input_json: Dict[str, Any] | None = None) -> Tuple[Optional[str], Optional[int]]:
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
        dur_i = int(float(dur)) if dur is not None else None
    except Exception:
        dur_i = None
    return (str(url) if url else None, dur_i)


def _maybe_import_alignment():
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


async def _call_any_upload_method(
    storage: Any,
    *,
    user_id: str,
    project_id: str,
    job_id: str,
    local_path: Path,
    content_type: str,
    blob_filename: str,
) -> str:
    candidates = [
        "upload_music_fallback_audio_and_get_sas_url",
        "upload_music_audio_and_get_sas_url",
        "upload_audio_and_get_sas_url",
        "upload_file_and_get_sas_url",
        "upload_local_file_and_get_sas_url",
        "upload_and_get_sas_url",
    ]

    kwargs = {
        "user_id": user_id,
        "project_id": project_id,
        "job_id": job_id,
        "local_path": local_path,
        "path": local_path,
        "file_path": local_path,
        "content_type": content_type,
        "mime": content_type,
        "blob_filename": blob_filename,
        "filename": blob_filename,
        "dst_filename": blob_filename,
    }

    for name in candidates:
        fn = getattr(storage, name, None)
        if not callable(fn):
            continue

        try:
            sig = inspect.signature(fn)
            call_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
            res = fn(**call_kwargs)
            if inspect.isawaitable(res):
                res = await res
            url = str(res or "").strip()
            if url:
                return url
        except Exception:
            continue

    raise RuntimeError("azure_storage_upload_method_missing_for_fallback_audio")


def _fallback_music_plan(*, mode: str, language: str | None, hints: Dict[str, Any]) -> JsonDict:
    title = str(hints.get("title") or "Untitled").strip() or "Untitled"
    genre = str(hints.get("genre") or hints.get("genre_hint") or "pop").strip() or "pop"
    mood = str(hints.get("mood") or hints.get("vibe_hint") or "uplifting").strip() or "uplifting"
    tempo = str(hints.get("tempo") or "mid").strip() or "mid"
    style_refs = hints.get("style_refs") or hints.get("style_ref") or []

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
            "Lightweight fallback plan.",
            "If MusicPlanningService is enabled, its plan will replace this.",
        ],
    }


class ConcreteMusicTools(MusicGraphTools):
    def __init__(self, *, job_id: UUID, project_id: UUID, user_id: UUID, input_json: Dict[str, Any] | None = None):
        self.job_id = job_id
        self.project_id = project_id
        self.user_id = user_id
        self.input_json = input_json or {}

        self.hints = _as_dict(self.input_json.get("provider_hints"))
        self.quality = str(self.input_json.get("quality") or "standard")
        self.seed = self.input_json.get("seed")

        self._align_real, self._align_naive = _maybe_import_alignment()
        self._planner = MusicPlanningService() if MusicPlanningService else None

    def _demo_use_voice_ref_as_audio(self) -> bool:
        return _is_truthy(self.hints.get("demo_use_voice_ref_as_audio") or self.hints.get("demo_voice_ref_as_audio"))

    def _computed(self) -> JsonDict:
        raw = self.input_json.get("computed")
        c = _as_dict_loose(raw)
        if c and not isinstance(raw, dict):
            self.input_json["computed"] = c
        return c

    def _set_computed(self, key: str, value: Any) -> None:
        c = self._computed()
        c[key] = value
        self.input_json["computed"] = c

    def _get_mode(self, s: MusicGraphState) -> str:
        return _normalize_mode(getattr(s, "mode", None))

    def _get_requested_outputs(self, s: MusicGraphState) -> set[str]:
        return _outputs_set(getattr(s, "requested_outputs", []) or [])

    def _pick_lyrics_source(self, *, mode: str, outputs: set[str], provided_lyrics: bool) -> str:
        src = ((self.input_json.get("lyrics_source") or self.hints.get("lyrics_source") or "").strip().lower())

        if provided_lyrics:
            return "upload"

        if src in ("generate", "upload", "none"):
            if src == "none" and MusicTrackType.timed_lyrics_json.value in outputs:
                return "generate"
            return src

        if mode == MusicProjectMode.byo.value:
            return "generate" if MusicTrackType.timed_lyrics_json.value in outputs else "none"

        return "generate"

    def _set_audio_probe_from_known_duration(self, *, duration_ms: int) -> None:
        try:
            duration_ms = int(float(duration_ms or 0))
        except Exception:
            duration_ms = 0
        if duration_ms <= 0:
            return

        c = self._computed()
        ap = _as_dict(c.get("audio_probe"))

        try:
            existing_sec = float(ap.get("duration_sec") or 0)
        except Exception:
            existing_sec = 0.0

        if existing_sec <= 0.0:
            ap["duration_ms"] = duration_ms
            ap["duration_sec"] = float(duration_ms) / 1000.0
            ap.setdefault("beats_per_bar", 4)
            ap.setdefault("source", "known_duration")
            self._set_computed("audio_probe", ap)

        try:
            td = float(c.get("track_duration_sec") or 0)
        except Exception:
            td = 0.0
        if td <= 0.0:
            self._set_computed("track_duration_sec", float(duration_ms) / 1000.0)

    def _generate_fallback_lyrics(self, s: MusicGraphState) -> str:
        title = str(self.hints.get("title") or self.input_json.get("title") or "My Song").strip()
        mood = str(self.hints.get("mood") or self.hints.get("vibe_hint") or "uplifting").strip()
        genre = str(self.hints.get("genre") or self.hints.get("genre_hint") or "pop").strip()

        chorus = f"{title}, {title}\nWe rise with a {mood} glow\n{title}, {title}\nLet the whole world know"
        verse1 = (
            f"Verse 1:\nIn the {mood} night, we find our way\nOne small step, then we sway\n"
            f"Heartbeats sync to {genre} dreams\nNothing’s ever as it seems"
        )
        verse2 = (
            "Verse 2:\nHold the line, don’t let it fade\nMoments bright that we have made\n"
            "From today into the new\nI believe, and so do you"
        )
        bridge = "Bridge:\nBreathe in… breathe out…\nWe’re not alone, we’re here right now"

        return f"{verse1}\n\nChorus:\n{chorus}\n\n{verse2}\n\n{bridge}\n\nChorus:\n{chorus}\n"

    async def intent(self, s: MusicGraphState) -> Dict[str, Any]:
        await self.ensure_music_plan(s)
        return {
            "mode": getattr(s, "mode", None),
            "language_hint": getattr(s, "language_hint", None),
            "quality": self.quality,
            "seed": self.seed,
        }

    async def ensure_music_plan(self, s: MusicGraphState) -> None:
        computed = self._computed()
        force = _is_truthy(self.hints.get("force_replan") or self.input_json.get("force_replan"))
        if not force and computed.get("music_plan"):
            return

        mode = self._get_mode(s)
        language = getattr(s, "language_hint", None)

        if self._planner:
            plan_out = await self._planner.build_plan(
                mode=mode,
                language=language,
                hints=self.hints,
                computed=computed,
            )
            if hasattr(plan_out, "model_dump"):
                plan_payload = plan_out.model_dump(mode="json")  # type: ignore
            elif isinstance(plan_out, dict):
                plan_payload = plan_out
            else:
                plan_payload = {"summary": str(plan_out)}
        else:
            plan_payload = _fallback_music_plan(mode=mode, language=language, hints=self.hints)

        self._set_computed("music_plan", plan_payload)
        summary = _as_dict(plan_payload).get("summary")
        if summary:
            self._set_computed("plan_summary", summary)

    async def creative_brief(self, s: MusicGraphState) -> Dict[str, Any]:
        brief = {
            "title": self.hints.get("title"),
            "genre": self.hints.get("genre"),
            "mood": self.hints.get("mood"),
            "tempo": self.hints.get("tempo"),
            "style_refs": self.hints.get("style_refs"),
        }

        await self.ensure_music_plan(s)

        plan_summary = _as_dict(self._computed().get("music_plan")).get("summary") or self._computed().get("plan_summary")
        if plan_summary:
            brief["plan_summary"] = plan_summary

        return brief

    async def lyrics(self, s: MusicGraphState) -> Dict[str, Any]:
        mode = self._get_mode(s)
        outputs = self._get_requested_outputs(s)

        provided = (
            self.input_json.get("lyrics_text")
            or self.hints.get("lyrics_text")
            or self.hints.get("lyrics")
            or self._computed().get("lyrics_text")
        )
        provided_text = str(provided).strip() if provided else ""
        provided_lyrics = bool(provided_text)

        src = self._pick_lyrics_source(mode=mode, outputs=outputs, provided_lyrics=provided_lyrics)

        needs_lyrics = src in ("generate", "upload") or (MusicTrackType.timed_lyrics_json.value in outputs)
        if not needs_lyrics or src == "none":
            self._set_computed("lyrics_source_effective", "none")
            return {}

        if src == "upload" and not provided_text:
            if MusicTrackType.timed_lyrics_json.value in outputs:
                src = "generate"
            else:
                self._set_computed("lyrics_source_effective", "none")
                return {}

        if src == "generate" and not provided_text:
            provided_text = self._generate_fallback_lyrics(s)

        self._set_computed("lyrics_text", provided_text)
        self._set_computed("lyrics_source_effective", src)
        return {"lyrics_text": provided_text, "lyrics_source": src}

    async def arrangement(self, s: MusicGraphState) -> Dict[str, Any]:
        return {"arrangement_hint": self.hints.get("arrangement_hint")}

    async def route_provider(self, s: MusicGraphState) -> Dict[str, Any]:
        computed = self._computed()
        byo_url, _ = _get_byo_audio(self.hints, self.input_json)

        has_audio_master = bool(
            byo_url
            or computed.get("audio_master_url")
            or computed.get("byo_audio_url")
            or computed.get("demo_audio_url")
        )
        has_demo_voice_ref_audio = self._demo_use_voice_ref_as_audio() and bool(computed.get("voice_ref_url"))

        mode = self._get_mode(s)
        if mode == MusicProjectMode.byo.value or has_audio_master or has_demo_voice_ref_audio:
            return {"provider": "byo"}

        provider = (
            self.hints.get("music_provider")
            or self.hints.get("provider")
            or getattr(settings, "MUSIC_AUTOPILOT_PROVIDER", None)
        )
        provider = normalize_provider(provider) if provider else default_autopilot_provider()
        return {"provider": provider or "native"}

    def _ffmpeg_available(self) -> bool:
        return bool(shutil.which("ffmpeg"))

    def _write_silence_wav(self, *, path: Path, duration_ms: int, sample_rate: int = 44100) -> None:
        duration_ms = max(1000, int(duration_ms or 30_000))
        frames = int(sample_rate * (duration_ms / 1000.0))
        silence = b"\x00\x00" * frames  # 16-bit mono silence
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(silence)

    async def _generate_native_fallback_full_mix(self, *, duration_ms: int) -> Tuple[str, int, str]:
        duration_ms = max(1000, int(duration_ms or 30_000))
        dur_s = max(1, int(round(duration_ms / 1000.0)))

        out_dir = Path("/tmp/df_music_native") / str(self.job_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"fallback_full_mix_{int(time.time())}.wav"

        if self._ffmpeg_available():
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=220:sample_rate=44100:duration={dur_s}",
                "-c:a",
                "pcm_s16le",
                str(out_path),
            ]
            try:
                subprocess.run(cmd, check=True)
            except Exception:
                self._write_silence_wav(path=out_path, duration_ms=duration_ms)
        else:
            self._write_silence_wav(path=out_path, duration_ms=duration_ms)

        storage = AzureStorageService.for_output() if hasattr(AzureStorageService, "for_output") else AzureStorageService()  # type: ignore
        sas_url = await _call_any_upload_method(
            storage,
            user_id=str(self.user_id),
            project_id=str(self.project_id),
            job_id=str(self.job_id),
            local_path=out_path,
            content_type="audio/wav",
            blob_filename="fallback_full_mix.wav",
        )

        return (str(sas_url), duration_ms, "audio/wav")

    async def generate_audio(self, s: MusicGraphState) -> List[GraphTrack]:
        computed = self._computed()
        mode = self._get_mode(s)

        voice_ref_url = str(computed.get("voice_ref_url") or "").strip()
        audio_url, audio_dur = _get_byo_audio(self.hints, self.input_json)

        # never treat voice_ref as song audio unless demo flag is on
        if audio_url and voice_ref_url and str(audio_url).strip() == voice_ref_url and not self._demo_use_voice_ref_as_audio():
            audio_url = None

        if not audio_url:
            audio_url = computed.get("audio_master_url") or computed.get("byo_audio_url") or computed.get("demo_audio_url")

        demo_audio_url = None
        if not audio_url and self._demo_use_voice_ref_as_audio():
            if voice_ref_url:
                demo_audio_url = voice_ref_url

        final_audio_url = str(audio_url).strip() if audio_url else (str(demo_audio_url).strip() if demo_audio_url else None)

        # BYO / already have master audio
        if mode == MusicProjectMode.byo.value or final_audio_url:
            if not final_audio_url:
                raise Exception("missing_audio_master_url")

            if not audio_dur:
                audio_dur = _coerce_int(
                    self.input_json.get("audio_master_duration_ms")
                    or self.hints.get("audio_master_duration_ms")
                    or computed.get("audio_master_duration_ms")
                    or 0,
                    30_000,
                ) or 30_000

            ct = _guess_audio_content_type(final_audio_url, "audio/mpeg")

            self._set_computed("audio_master_url", str(final_audio_url))
            self._set_computed("byo_audio_url", str(final_audio_url))
            self._set_computed("audio_master_duration_ms", int(audio_dur))
            self._set_computed("audio_duration_ms", int(audio_dur))
            self._set_computed("audio_content_type", ct)
            self._set_computed("audio_source", "demo_voice_ref_url" if demo_audio_url else "byo")

            self._set_audio_probe_from_known_duration(duration_ms=int(audio_dur))

            meta: Dict[str, Any] = {
                "audio_duration_ms": int(audio_dur),
                "url": str(final_audio_url),
                "content_type": ct,
                "audio_master_url": str(final_audio_url),
                "byo_audio_url": str(final_audio_url),
                "byo_duration_ms": int(audio_dur),
            }

            if demo_audio_url:
                meta.update({"demo_audio_url": str(demo_audio_url), "is_demo": True, "source": "byo_demo"})
            else:
                meta.update({"is_demo": False, "source": "byo"})

            return [
                GraphTrack(
                    track_type=MusicTrackType.full_mix.value,
                    duration_ms=int(audio_dur),
                    artifact_id=None,
                    media_asset_id=None,
                    meta=meta,
                )
            ]

        # AUTOPILOT / CO_CREATE
        provider = normalize_provider(
            self.hints.get("music_provider")
            or self.hints.get("provider")
            or computed.get("audio_provider")
            or getattr(settings, "MUSIC_AUTOPILOT_PROVIDER", None)
            or default_autopilot_provider()
        )

        if provider in ("fal_sonauto_v2", "sonauto_v2", "sonauto") and callable(compose_full_mix_fal_sonauto_v2):
            try:
                seed_i: Optional[int] = None
                if self.seed is not None:
                    try:
                        seed_i = int(float(self.seed))
                    except Exception:
                        seed_i = None

                res: AutopilotComposeResult = await compose_full_mix_fal_sonauto_v2(  # type: ignore
                    user_id=str(self.user_id),
                    project_id=str(self.project_id),
                    job_id=str(self.job_id),
                    language_hint=getattr(s, "language_hint", None),
                    quality=str(self.quality or "standard"),
                    seed=seed_i,
                    hints=self.hints,
                    computed=self._computed(),
                )

                sas = str(getattr(res, "sas_url", "") or "").strip() or None
                dur_ms = int(getattr(res, "duration_ms", 0) or 0) or 30_000
                ct = str(getattr(res, "content_type", "audio/mpeg") or "audio/mpeg")

                if not sas:
                    raise RuntimeError("autopilot_provider_missing_sas_url")

                self._set_computed("audio_provider", getattr(res, "provider", "fal_sonauto_v2"))
                self._set_computed("provider_request_id", getattr(res, "provider_request_id", None))
                self._set_computed("audio_master_url", sas)
                self._set_computed("byo_audio_url", sas)
                self._set_computed("audio_master_duration_ms", dur_ms)
                self._set_computed("audio_duration_ms", dur_ms)
                self._set_computed("audio_content_type", ct)
                self._set_computed("audio_source", "autopilot_provider")

                self._set_audio_probe_from_known_duration(duration_ms=int(dur_ms))

                meta2: Dict[str, Any] = {
                    "audio_duration_ms": int(dur_ms),
                    "url": str(sas),
                    "content_type": ct,
                    "source": "autopilot",
                    "is_demo": False,
                }

                return [
                    GraphTrack(
                        track_type=MusicTrackType.full_mix.value,
                        duration_ms=int(dur_ms),
                        artifact_id=None,
                        media_asset_id=None,
                        meta=meta2,
                    )
                ]
            except Exception as e:
                self._set_computed("autopilot_provider_error", str(e))

        # Always-works native fallback
        plan = _as_dict(self._computed().get("music_plan"))
        duration_ms = _coerce_int(
            plan.get("duration_ms")
            or self.input_json.get("duration_ms")
            or self.hints.get("duration_ms")
            or self._computed().get("audio_master_duration_ms")
            or 30_000,
            30_000,
        )

        fallback_url, fallback_dur_ms, ct = await self._generate_native_fallback_full_mix(duration_ms=duration_ms)

        self._set_computed("audio_provider", "native")
        self._set_computed("audio_master_url", fallback_url)
        self._set_computed("byo_audio_url", fallback_url)
        self._set_computed("audio_master_duration_ms", int(fallback_dur_ms))
        self._set_computed("audio_duration_ms", int(fallback_dur_ms))
        self._set_computed("audio_content_type", ct)
        self._set_computed("audio_source", "fallback_native")

        self._set_audio_probe_from_known_duration(duration_ms=int(fallback_dur_ms))

        return [
            GraphTrack(
                track_type=MusicTrackType.full_mix.value,
                duration_ms=int(fallback_dur_ms),
                artifact_id=None,
                media_asset_id=None,
                meta={"url": fallback_url, "content_type": ct, "source": "fallback_native", "is_demo": True},
            )
        ]

    async def align_lyrics(self, s: MusicGraphState) -> Optional[GraphTrack]:
        outputs = self._get_requested_outputs(s)
        if MusicTrackType.timed_lyrics_json.value not in outputs:
            return None

        computed = self._computed()
        lyrics_text = str(
            computed.get("lyrics_text") or self.hints.get("lyrics_text") or self.hints.get("lyrics") or ""
        ).strip()
        if not lyrics_text:
            return None

        dur = 0
        for t in getattr(s, "tracks", []) or []:
            if str(getattr(t, "track_type", "")) == MusicTrackType.full_mix.value:
                dur = int(getattr(t, "duration_ms", 0) or 0)
                break
        if dur <= 0:
            dur = _coerce_int(computed.get("audio_master_duration_ms") or computed.get("audio_duration_ms") or 0, 0)
        if dur <= 0:
            return None

        audio_url, _ = _get_byo_audio(self.hints, self.input_json)
        if not audio_url:
            audio_url = computed.get("audio_master_url") or computed.get("byo_audio_url") or computed.get("demo_audio_url")
        if not audio_url and self._demo_use_voice_ref_as_audio():
            audio_url = computed.get("voice_ref_url")

        timed: Dict[str, Any] | None = None
        try:
            if audio_url and callable(self._align_real):
                try:
                    timed = await self._align_real(
                        audio_url=audio_url,
                        lyrics_text=lyrics_text,
                        language=getattr(s, "language_hint", None),
                    )  # type: ignore
                except TypeError:
                    timed = await self._align_real(audio_url, lyrics_text, getattr(s, "language_hint", None))  # type: ignore

            if timed is None and callable(self._align_naive):
                timed = self._align_naive(lyrics_text, dur, language=getattr(s, "language_hint", None))  # type: ignore
        except Exception:
            timed = None

        if not timed:
            return None

        return GraphTrack(
            track_type=MusicTrackType.timed_lyrics_json.value,
            duration_ms=0,
            artifact_id=None,
            media_asset_id=None,
            meta={"inline_json": timed},
        )

    async def generate_performer_videos(self, s: MusicGraphState) -> Dict[str, Any]:
        """
        Product-grade performer generation via svc-fusion.

        Fixes:
          - Accepts face refs from ensure_performer_faces outputs:
              computed.performer_a_image_url
              computed.performer_b_image_url
              computed.performer_images[0]
          - Self-heals aliases:
              computed.performer_face_image_url / performer_face_artifact_id
        """
        try:
            import httpx  # type: ignore
        except Exception as e:
            self._set_computed("performer_videos_skipped", True)
            self._set_computed("performer_videos_skip_reason", "httpx_missing")
            self._set_computed("performer_videos", [])
            if _is_truthy(os.getenv("DF_REQUIRE_PERFORMER_VIDEOS", "0")):
                raise RuntimeError("performer_videos_require_httpx") from e
            return {"performer_videos": []}

        enable = _is_truthy(os.getenv("DF_ENABLE_PERFORMER_VIDEOS", "0")) or _is_truthy(self.hints.get("enable_performer_videos"))
        require = _is_truthy(os.getenv("DF_REQUIRE_PERFORMER_VIDEOS", "0")) or _is_truthy(self.hints.get("require_performer_videos"))

        if not enable:
            self._set_computed("performer_videos_skipped", True)
            self._set_computed("performer_videos_skip_reason", "disabled")
            self._set_computed("performer_videos", [])
            return {"performer_videos": []}

        computed = self._computed()

        # NEW: accept performer_images[0] as face ref
        perf_imgs = computed.get("performer_images")
        perf_imgs = perf_imgs if isinstance(perf_imgs, list) else []
        perf_img0 = str(perf_imgs[0]).strip() if perf_imgs else ""

        # Face reference (expanded fallbacks)
        face_image_url = _first_http_url(
            os.getenv("DF_PERFORMER_FACE_IMAGE_URL"),
            self.hints.get("performer_face_image_url"),
            computed.get("performer_face_image_url"),
            # fallbacks from ensure_performer_faces
            computed.get("performer_a_image_url"),
            computed.get("performer_b_image_url"),
            perf_img0,
            computed.get("performer_image_url"),
        )

        face_artifact_id = _first_nonempty(
            os.getenv("DF_PERFORMER_FACE_ARTIFACT_ID"),
            self.hints.get("performer_face_artifact_id"),
            computed.get("performer_face_artifact_id"),
            computed.get("performer_a_artifact_id"),
            computed.get("performer_b_artifact_id"),
            computed.get("performer_artifact_id"),
        )

        # Self-heal aliases for downstream code / status visibility
        if face_image_url and not str(computed.get("performer_face_image_url") or "").strip():
            computed["performer_face_image_url"] = face_image_url
        if face_artifact_id and not str(computed.get("performer_face_artifact_id") or "").strip():
            computed["performer_face_artifact_id"] = face_artifact_id
        self.input_json["computed"] = computed

        if not face_image_url and not face_artifact_id:
            self._set_computed("performer_videos_skipped", True)
            self._set_computed("performer_videos_skip_reason", "missing_face_ref")
            self._set_computed("performer_videos", [])
            if require:
                raise RuntimeError("performer_videos_missing_face_ref")
            return {"performer_videos": []}

        # Audio (song audio)
        audio_url = (
            str(computed.get("audio_master_url") or "").strip()
            or str(computed.get("byo_audio_url") or "").strip()
            or str(computed.get("demo_audio_url") or "").strip()
            or str(self.input_json.get("uploaded_audio_url") or "").strip()
        )
        if not audio_url and self._demo_use_voice_ref_as_audio():
            audio_url = str(computed.get("voice_ref_url") or "").strip()

        if not audio_url:
            self._set_computed("performer_videos_skipped", True)
            self._set_computed("performer_videos_skip_reason", "missing_audio_url")
            self._set_computed("performer_videos", [])
            if require:
                raise RuntimeError("performer_videos_missing_audio_url")
            return {"performer_videos": []}

        fusion_base = _get_fusion_base()

        # Use the music job owner (must match token subject in most setups)
        user_id = str(getattr(s, "user_id", "") or self.user_id)

        # Debug breadcrumbs (safe: do not include token)
        self._set_computed("performer_video_fusion_base", fusion_base)
        self._set_computed("performer_video_user_id", user_id)

        provider = str(os.getenv("DF_FUSION_PROVIDER") or self.hints.get("fusion_provider") or "heygen_av4")

        payload: Dict[str, Any] = {
            "voice_mode": "audio",
            "voice_audio": {"audio_url": audio_url},
            "provider": provider,
            "consent": {"external_provider_ok": True},
            "tags": {
                "source": "svc-music",
                "music_job_id": str(self.job_id),
                "music_project_id": str(self.project_id),
                "purpose": "performer_video",
            },
        }
        if face_image_url:
            payload["face_image_url"] = face_image_url
        if face_artifact_id:
            payload["face_artifact_id"] = face_artifact_id

        timeout_s = _coerce_int(os.getenv("DF_FUSION_CREATE_TIMEOUT_SECS", 60), 60)
        poll_timeout_s = _coerce_int(os.getenv("DF_FUSION_TIMEOUT_SECS", 900), 900)
        poll_every_s = float(os.getenv("DF_FUSION_POLL_SECS", "5") or 5)

        async def _run_once(*, force_refresh_token: bool = False) -> Dict[str, Any]:
            if force_refresh_token:
                auth = f"Bearer {await _get_core_access_token(force_refresh=True)}"
                src = "core_refresh_forced"
            else:
                auth, src = await _get_fusion_bearer_token()

            headers = _fusion_headers(user_id=user_id, authorization=auth)
            self._set_computed("performer_video_auth_source", src)

            create_url = f"{fusion_base}/jobs"

            async with httpx.AsyncClient(timeout=timeout_s) as client:
                r = await client.post(create_url, headers=headers, json=payload)
                self._set_computed("performer_video_create_http_status", int(r.status_code))

                if r.status_code == 401:
                    return {"_auth_401": True, "_body": (r.text or "")[:240]}

                if r.status_code >= 400:
                    return {"_error": f"fusion_create_http_{r.status_code}:{(r.text or '')[:240]}"}

                try:
                    create_json_any = r.json()
                except Exception:
                    create_json_any = {}

                create_json = create_json_any if isinstance(create_json_any, dict) else {}
                fjid = _extract_fusion_job_id(create_json)
                if not fjid:
                    return {"_error": f"fusion_create_missing_job_id keys={list(create_json.keys())[:25]}"}

                get_url = f"{fusion_base}/jobs/{fjid}"
                t0 = time.time()

                while True:
                    rr = await client.get(get_url, headers=headers)
                    if rr.status_code == 401:
                        return {"_auth_401": True, "_body": (rr.text or "")[:240], "_job_id": fjid}
                    if rr.status_code >= 400:
                        return {"_error": f"fusion_poll_http_{rr.status_code}:{(rr.text or '')[:240]}", "_job_id": fjid}

                    try:
                        j_any = rr.json()
                    except Exception:
                        j_any = {}
                    j = j_any if isinstance(j_any, dict) else {}
                    st = _extract_fusion_status(j)

                    if st in ("succeeded", "success", "completed", "done"):
                        vid = _extract_fusion_video_url(j)
                        if not vid:
                            return {"_error": "fusion_succeeded_but_missing_video_url", "_job_id": fjid}
                        return {"video_url": vid, "fusion_job_id": fjid, "provider": provider}

                    if st in ("failed", "error", "canceled", "cancelled"):
                        err = str(j.get("error_message") or j.get("error") or j.get("message") or "fusion_failed")
                        return {"_error": f"fusion_failed status={st} err={err}", "_job_id": fjid}

                    if time.time() - t0 > poll_timeout_s:
                        return {"_error": "fusion_timeout_waiting_for_video", "_job_id": fjid}

                    await __import__("asyncio").sleep(poll_every_s)

        try:
            out = await _run_once(force_refresh_token=False)

            if out.get("_auth_401"):
                self._set_computed("performer_video_auth_401_body", str(out.get("_body") or ""))
                out = await _run_once(force_refresh_token=True)

            if out.get("_error"):
                self._set_computed("performer_videos_skipped", True)
                self._set_computed("performer_videos_skip_reason", str(out["_error"]))
                self._set_computed("performer_videos", [])
                if require:
                    raise RuntimeError(str(out["_error"]))
                return {"performer_videos": []}

            item = {
                "video_url": out["video_url"],
                "provider": out.get("provider") or provider,
                "fusion_job_id": out.get("fusion_job_id"),
            }
            self._set_computed("performer_videos_skipped", False)
            self._set_computed("performer_videos_skip_reason", None)
            self._set_computed("performer_videos", [item])
            self._set_computed("performer_video_url", item["video_url"])
            self._set_computed("performer_video_job_id", item.get("fusion_job_id"))
            return {"performer_videos": [item]}

        except Exception as e:
            self._set_computed("performer_videos_skipped", True)
            self._set_computed("performer_videos_skip_reason", f"fusion_error:{e}")
            self._set_computed("performer_videos", [])
            if require:
                raise
            return {"performer_videos": []}

    async def compose_video(self, s: MusicGraphState) -> Dict[str, Any]:
        computed = self._computed()
        existing = computed.get("clip_manifest")
        if isinstance(existing, dict) and isinstance(existing.get("clips"), list) and existing["clips"]:
            self._set_computed("compose_video_skipped", True)
            return {"skipped": True, "reason": "clip_manifest_already_present"}

        self._set_computed("compose_video_skipped", True)
        return {"skipped": True, "reason": "compose_video_disabled_in_v1_use_clip_manifest_service"}

    async def qc(self, s: MusicGraphState) -> Dict[str, Any]:
        have_full = any(
            str(getattr(t, "track_type", "")) == MusicTrackType.full_mix.value for t in getattr(s, "tracks", []) or []
        )
        if not have_full:
            c = self._computed()
            if not (c.get("audio_master_url") or c.get("byo_audio_url") or c.get("demo_audio_url")):
                raise Exception("qc_failed_missing_full_mix")
        return {"ok": True}