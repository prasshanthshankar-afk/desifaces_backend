# services/svc-music/app/app/services/music_tools.py
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
import shutil
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.config import settings
from app.domain.enums import MusicProjectMode, MusicTrackType
from app.services.azure_storage_service import AzureStorageService

from .music_graph import GraphTrack, MusicGraphState, MusicGraphTools

logger = logging.getLogger(__name__)

# Optional: planner lives elsewhere
try:
    from app.services.music_planning.service import MusicPlanningService  # type: ignore
except Exception:
    MusicPlanningService = None  # type: ignore

# Optional: autopilot provider (Fal Sonauto v2) lives elsewhere
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


JsonDict = Dict[str, Any]

_ALLOWED_CLIENT_TYPES = ("web", "ios", "android")


# -----------------------------
# Core auth cache (in-memory)
# -----------------------------
_CORE_TOKEN: str | None = None
_CORE_TOKEN_EXP: float = 0.0
_CORE_REFRESH: str | None = None


# -----------------------------
# Env helpers
# -----------------------------
def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _allow_fallback_audio(*, quality: str) -> bool:
    """
    DesiFaces quality policy:
      - By default, DO NOT ship fallback audio (humming / tones).
      - Fallback audio is explicitly opt-in for dev via DF_ALLOW_FALLBACK_AUDIO=1.
      - Back-compat: if MUSIC_ALLOW_NATIVE_FALLBACK is set, respect it.
    """
    # Primary switch (default OFF)
    allow = _env_bool("DF_ALLOW_FALLBACK_AUDIO", False)

    # Back-compat override only if explicitly set
    if (os.getenv("MUSIC_ALLOW_NATIVE_FALLBACK") or "").strip():
        allow = _env_bool("MUSIC_ALLOW_NATIVE_FALLBACK", allow)

    # If caller explicitly asks for "pro"/"high"/"hd", never allow fallback unless they force it
    # (i.e., DF_ALLOW_FALLBACK_AUDIO=1 still wins).
    if str(quality or "").lower() in ("pro", "high", "hd") and not _env_bool("DF_ALLOW_FALLBACK_AUDIO", False):
        return False

    return allow


def _normalize_client_type(v: str | None, *, default: str = "ios") -> str:
    s = (v or "").strip().lower()
    if s not in _ALLOWED_CLIENT_TYPES:
        return default
    return s


# -----------------------------
# Small helpers
# -----------------------------
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
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return 0.0
        payload_b64 = parts[1]
        pad = "=" * (-len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode((payload_b64 + pad).encode("utf-8"))
        obj = json.loads(payload.decode("utf-8"))
        exp = obj.get("exp")
        return float(exp) if exp is not None else 0.0
    except Exception:
        return 0.0


def _running_in_docker() -> bool:
    return os.path.exists("/.dockerenv")


def _default_fusion_base() -> str:
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


def _outputs_set(outputs: List[str]) -> set[str]:
    return {str(x).strip().lower() for x in (outputs or []) if x}


def _normalize_mode(val: Any) -> str:
    v = getattr(val, "value", val)
    s = str(v or "").strip()
    return (s or MusicProjectMode.autopilot.value).lower()


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


def _is_fallback_full_mix_url(url: str) -> bool:
    u = (url or "").lower()
    return "fallback_full_mix" in u or "fallback_native" in u


# -----------------------------
# “Pro audio” shaping helpers
# -----------------------------
def _infer_genre_family(*, hints: Dict[str, Any], plan: Dict[str, Any], computed: Dict[str, Any]) -> str:
    brief = _as_dict(plan.get("brief"))
    g = str(hints.get("genre") or hints.get("genre_hint") or brief.get("genre") or computed.get("genre") or "").strip().lower()
    preset = str(computed.get("preset_name") or "").strip().lower()
    tags = [str(t).lower() for t in _as_list(computed.get("preset_tags_used"))]
    s = " ".join([g, preset, " ".join(tags)])

    if any(k in s for k in ("bollywood", "party", "dance", "bhangra", "dhol")):
        return "bollywood_party"
    if any(k in s for k in ("devotional", "bhajan", "aarti", "qawwali", "sufi")):
        return "devotional"
    if any(k in s for k in ("ghazal", "semi-classical")):
        return "ghazal"
    if any(k in s for k in ("classical", "raag", "raga", "carnatic", "hindustani")):
        return "classical"
    if any(k in s for k in ("folk", "regional", "garba", "lavani", "baul")):
        return "folk_regional"
    if any(k in s for k in ("jazz", "swing", "bebop")):
        return "jazz"
    if any(k in s for k in ("blues", "shuffle")):
        return "blues"
    if any(k in s for k in ("hiphop", "hip-hop", "rap", "trap")):
        return "hiphop"
    if any(k in s for k in ("edm", "house", "techno", "trance")):
        return "edm"
    if any(k in s for k in ("lofi", "lo-fi", "chill")):
        return "lofi"
    if any(k in s for k in ("rock", "guitar")):
        return "rock"
    return g or "pop"


def _infer_mood(*, hints: Dict[str, Any], plan: Dict[str, Any]) -> str:
    brief = _as_dict(plan.get("brief"))
    m = str(hints.get("mood") or hints.get("vibe_hint") or brief.get("mood") or "").strip().lower()
    return m or "uplifting"


def _infer_tempo_bpm(*, hints: Dict[str, Any], plan: Dict[str, Any], genre_family: str) -> int:
    brief = _as_dict(plan.get("brief"))
    tempo = str(hints.get("tempo") or hints.get("tempo_hint") or brief.get("tempo") or "").strip().lower()

    try:
        bpm = int(float(tempo))
        return max(60, min(180, bpm))
    except Exception:
        pass

    if tempo in ("slow", "low", "calm"):
        return 72 if genre_family in ("ghazal", "devotional", "classical") else 78
    if tempo in ("fast", "high", "energetic"):
        return 132 if genre_family in ("edm", "bollywood_party") else 124
    if tempo in ("mid", "medium"):
        return 104

    if genre_family == "edm":
        return 128
    if genre_family == "hiphop":
        return 92
    if genre_family == "jazz":
        return 110
    if genre_family == "blues":
        return 96
    if genre_family == "bollywood_party":
        return 128
    return 104


def _infer_instrumentation(genre_family: str) -> List[str]:
    if genre_family == "bollywood_party":
        return ["dhol", "tabla", "tumbi", "brass stabs", "synth bass", "dance drums"]
    if genre_family == "devotional":
        return ["tabla", "harmonium", "tanpura", "soft pads", "gentle percussion"]
    if genre_family == "ghazal":
        return ["tabla", "harmonium", "sarangi", "nylon guitar", "warm strings"]
    if genre_family == "classical":
        return ["tanpura", "tabla/mridangam", "sitar/violin", "flute", "tambura"]
    if genre_family == "folk_regional":
        return ["dholak", "claps", "regional percussion", "acoustic strings", "flute"]
    if genre_family == "jazz":
        return ["upright bass", "brush drums", "piano comping", "sax lead", "warm room reverb"]
    if genre_family == "blues":
        return ["electric guitar", "bass", "shuffle drums", "harmonica (optional)"]
    if genre_family == "hiphop":
        return ["808 bass", "tight kick", "snare", "hi-hats", "atmospheric pads"]
    if genre_family == "edm":
        return ["four-on-the-floor kick", "sidechained bass", "synth leads", "risers", "wide pads"]
    if genre_family == "lofi":
        return ["lofi drums", "vinyl texture", "soft keys", "sub bass", "warm tape"]
    return ["modern drums", "bass", "pads", "melodic lead"]


def _mix_master_prompt(quality: str) -> str:
    base = (
        "professional studio production; radio-ready mix and master; clean vocals; punchy drums; "
        "tight low-end; no muddy mids; wide stereo; tasteful reverb; modern loudness"
    )
    if str(quality or "").lower() in ("pro", "high", "hd"):
        return base + "; high fidelity; polished mastering; commercial-grade"
    return base


def _build_audio_prompt(
    *, title: str, genre_family: str, mood: str, bpm: int, instruments: List[str], language: Optional[str], quality: str
) -> str:
    lang = str(language or "en").strip() or "en"
    inst = ", ".join([i for i in instruments if str(i).strip()]) or "full instrumentation"
    return (
        f"Title: {title}. Genre: {genre_family}. Mood: {mood}. Tempo: {bpm} BPM. Language: {lang}. "
        f"Instrumentation: {inst}. {_mix_master_prompt(quality)}."
    )


def _hook_optional_for_genre(genre_family: str, mood: str) -> bool:
    s = f"{genre_family} {mood}".lower()
    if any(k in s for k in ("devotional", "ghazal", "classical")):
        return False
    if any(k in s for k in ("hiphop", "rap")):
        return False
    return any(k in s for k in ("pop", "bollywood", "party", "edm", "dance", "lofi"))


# -----------------------------
# Lyrics alignment import (optional)
# -----------------------------
def _maybe_import_alignment():
    try:
        import app.services.lyrics_alignment_service as las  # type: ignore
    except Exception:
        las = None

    real = getattr(las, "align_lyrics", None) if las else None
    naive = getattr(las, "naive_timed_lyrics", None) if las else None

    if naive is None:

        def naive_timed_lyrics_fallback(lyrics_text: str, duration_ms: int, *, language: str | None = None) -> Dict[str, Any]:
            duration_ms = max(1, int(duration_ms or 1))
            lines = [ln.strip() for ln in (lyrics_text or "").splitlines() if ln.strip()]
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

            segments[-1]["end_ms"] = duration_ms
            if segments[-1]["words"]:
                segments[-1]["words"][-1]["end_ms"] = duration_ms
            return {"version": 1, "language": language, "segments": segments}

        naive = naive_timed_lyrics_fallback

    return real, naive


# -----------------------------
# Azure upload wrapper (handles method differences)
# -----------------------------
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


# -----------------------------
# Fallback planning (deterministic)
# -----------------------------
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
            {"step": "arrangement", "why": "Optional: sectioning based on style"},
            {"step": "provider_route", "why": "Choose provider"},
            {"step": "generate_audio", "why": "Produce full mix"},
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

    # -------- computed helpers --------
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

    def _demo_use_voice_ref_as_audio(self) -> bool:
        return _is_truthy(self.hints.get("demo_use_voice_ref_as_audio") or self.hints.get("demo_voice_ref_as_audio"))

    # -------- plan/brief/lyrics --------
    async def intent(self, s: MusicGraphState) -> Dict[str, Any]:
        await self.ensure_music_plan(s)
        return {"mode": getattr(s, "mode", None), "language_hint": getattr(s, "language_hint", None), "quality": self.quality, "seed": self.seed}

    async def ensure_music_plan(self, s: MusicGraphState) -> None:
        computed = self._computed()
        force = _is_truthy(self.hints.get("force_replan") or self.input_json.get("force_replan"))
        if not force and computed.get("music_plan"):
            return

        mode = self._get_mode(s)
        language = getattr(s, "language_hint", None)

        if self._planner:
            plan_out = await self._planner.build_plan(mode=mode, language=language, hints=self.hints, computed=computed)
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
        await self.ensure_music_plan(s)
        plan_summary = _as_dict(self._computed().get("music_plan")).get("summary") or self._computed().get("plan_summary")
        return {
            "title": self.hints.get("title"),
            "genre": self.hints.get("genre"),
            "mood": self.hints.get("mood"),
            "tempo": self.hints.get("tempo"),
            "style_refs": self.hints.get("style_refs"),
            "plan_summary": plan_summary,
        }

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

    def _generate_fallback_lyrics(self, s: MusicGraphState) -> str:
        plan = _as_dict(self._computed().get("music_plan"))
        brief = _as_dict(plan.get("brief"))

        title = str(self.hints.get("title") or self.input_json.get("title") or brief.get("title") or "My Song").strip()
        mood = str(self.hints.get("mood") or self.hints.get("vibe_hint") or brief.get("mood") or "uplifting").strip()
        genre_family = _infer_genre_family(hints=self.hints, plan=plan, computed=self._computed())
        want_hook = _hook_optional_for_genre(genre_family, mood)

        hook = f"{title}… {title}…\nWe move with a {mood} glow\n{title}… {title}…\nLet the whole world know"
        verse1 = (
            f"Verse 1:\nIn the {mood} night, we find our way\nOne small step, then we sway\n"
            "Hold the moment, feel it true\nA brand-new sky for me and you"
        )
        verse2 = (
            "Verse 2:\nKeep the fire, don’t let it fade\nEvery promise that we made\n"
            "From the dark into the light\nWe rise again, we’re shining bright"
        )
        bridge = "Bridge:\nBreathe in… breathe out…\nRight here, right now…"

        return f"{verse1}\n\nHook:\n{hook}\n\n{verse2}\n\n{bridge}\n\nHook:\n{hook}\n" if want_hook else f"{verse1}\n\n{verse2}\n\n{bridge}\n"

    async def lyrics(self, s: MusicGraphState) -> Dict[str, Any]:
        mode = self._get_mode(s)
        outputs = self._get_requested_outputs(s)

        provided = self.input_json.get("lyrics_text") or self.hints.get("lyrics_text") or self.hints.get("lyrics") or self._computed().get("lyrics_text")
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

    # -------- provider routing --------
    async def route_provider(self, s: MusicGraphState) -> Dict[str, Any]:
        computed = self._computed()
        byo_url, _ = _get_byo_audio(self.hints, self.input_json)

        has_audio_master = bool(byo_url or computed.get("audio_master_url") or computed.get("byo_audio_url") or computed.get("demo_audio_url"))
        has_demo_voice_ref_audio = self._demo_use_voice_ref_as_audio() and bool(computed.get("voice_ref_url"))

        mode = self._get_mode(s)
        if mode == MusicProjectMode.byo.value or has_audio_master or has_demo_voice_ref_audio:
            return {"provider": "byo"}

        provider = self.hints.get("music_provider") or self.hints.get("provider") or getattr(settings, "MUSIC_AUTOPILOT_PROVIDER", None)
        provider = normalize_provider(provider) if provider else ""

        if (not provider) and callable(compose_full_mix_fal_sonauto_v2):
            provider = "fal_sonauto_v2"

        provider = provider or default_autopilot_provider()
        return {"provider": provider or "native"}

    # -------- audio probe convenience --------
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

    # -------- native fallback audio (DEV ONLY) --------
    def _ffmpeg_available(self) -> bool:
        return bool(shutil.which("ffmpeg"))

    def _write_silence_wav(self, *, path: Path, duration_ms: int, sample_rate: int = 44100) -> None:
        duration_ms = max(1000, int(duration_ms or 30_000))
        frames = int(sample_rate * (duration_ms / 1000.0))
        silence = b"\x00\x00" * frames
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(silence)

    async def _generate_native_fallback_full_mix(self, *, duration_ms: int) -> Tuple[str, int, str]:
        """
        DEV-only fallback. This is intentionally not "real music".
        DesiFaces quality policy: should be gated by DF_ALLOW_FALLBACK_AUDIO=1.
        """
        duration_ms = max(1000, int(duration_ms or 30_000))
        dur_s = max(2, int(round(duration_ms / 1000.0)))

        out_dir = Path("/tmp/df_music_native") / str(self.job_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"fallback_full_mix_{int(time.time())}.wav"

        # If we must produce *something* for dev, keep it short and unobtrusive.
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
                f"anoisesrc=duration={dur_s}:sample_rate=44100:color=pink",
                "-filter:a",
                "lowpass=f=1400,volume=0.03",
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
        blob_name = f"fallback_full_mix_{self.job_id}_{int(time.time())}.wav"
        sas_url = await _call_any_upload_method(
            storage,
            user_id=str(self.user_id),
            project_id=str(self.project_id),
            job_id=str(self.job_id),
            local_path=out_path,
            content_type="audio/wav",
            blob_filename=blob_name,
        )
        return (str(sas_url), duration_ms, "audio/wav")

    # -------- audio generation --------
    async def generate_audio(self, s: MusicGraphState) -> List[GraphTrack]:
        """
        DesiFaces quality policy:
          - BYO uses provided audio.
          - Autopilot must produce a real track from a provider.
          - Native fallback is DEV-ONLY and OFF by default (DF_ALLOW_FALLBACK_AUDIO=1 to enable).
        """
        computed = self._computed()
        mode = self._get_mode(s)

        allow_fallback = _allow_fallback_audio(quality=self.quality)

        voice_ref_url = str(computed.get("voice_ref_url") or "").strip()
        audio_url, audio_dur = _get_byo_audio(self.hints, self.input_json)

        # never treat voice_ref as song audio unless demo flag is on
        if audio_url and voice_ref_url and str(audio_url).strip() == voice_ref_url and not self._demo_use_voice_ref_as_audio():
            audio_url = None

        if not audio_url:
            audio_url = computed.get("audio_master_url") or computed.get("byo_audio_url") or computed.get("demo_audio_url")

        demo_audio_url = None
        if not audio_url and self._demo_use_voice_ref_as_audio() and voice_ref_url:
            demo_audio_url = voice_ref_url

        final_audio_url = str(audio_url).strip() if audio_url else (str(demo_audio_url).strip() if demo_audio_url else None)

        # BYO / already have master audio
        if mode == MusicProjectMode.byo.value or final_audio_url:
            if not final_audio_url:
                raise RuntimeError("missing_audio_master_url")

            if not audio_dur:
                audio_dur = _coerce_int(
                    self.input_json.get("audio_master_duration_ms")
                    or self.hints.get("audio_master_duration_ms")
                    or computed.get("audio_master_duration_ms")
                    or 0,
                    30_000,
                ) or 30_000

            ct = _guess_audio_content_type(final_audio_url, "audio/mpeg")

            self._set_computed("audio_provider", "byo")
            self._set_computed("audio_is_fallback", False)
            self._set_computed("audio_fallback_reason", None)

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
                "is_demo": bool(demo_audio_url),
                "source": "byo_demo" if demo_audio_url else "byo",
            }

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

        # Prefer autopilot if available and provider wasn't explicitly set to native
        if provider in ("native", "") and callable(compose_full_mix_fal_sonauto_v2):
            provider = "fal_sonauto_v2"

        self._set_computed("audio_provider_requested", provider)
        self._set_computed("audio_is_fallback", False)
        self._set_computed("audio_fallback_reason", None)

        # Provider unavailable -> fail (unless dev explicitly enables fallback)
        if provider in ("native", "") and not callable(compose_full_mix_fal_sonauto_v2):
            msg = "music_provider_unavailable: autopilot provider not installed/configured"
            self._set_computed("audio_gen_error", msg)
            if not allow_fallback:
                raise RuntimeError(msg)
            # dev fallback allowed
            provider = "native"

        if provider in ("fal_sonauto_v2", "sonauto_v2", "sonauto") and callable(compose_full_mix_fal_sonauto_v2):
            try:
                seed_i: Optional[int] = None
                if self.seed is not None:
                    try:
                        seed_i = int(float(self.seed))
                    except Exception:
                        seed_i = None

                plan = _as_dict(self._computed().get("music_plan"))
                brief = _as_dict(plan.get("brief"))
                title = str(self.hints.get("title") or brief.get("title") or self.input_json.get("title") or "Untitled").strip()

                genre_family = _infer_genre_family(hints=self.hints, plan=plan, computed=self._computed())
                mood = _infer_mood(hints=self.hints, plan=plan)
                bpm = _infer_tempo_bpm(hints=self.hints, plan=plan, genre_family=genre_family)
                instruments = _infer_instrumentation(genre_family)

                audio_prompt = _build_audio_prompt(
                    title=title,
                    genre_family=genre_family,
                    mood=mood,
                    bpm=bpm,
                    instruments=instruments,
                    language=getattr(s, "language_hint", None),
                    quality=str(self.quality or "standard"),
                )

                self._set_computed("audio_genre_family", genre_family)
                self._set_computed("audio_mood", mood)
                self._set_computed("audio_bpm", bpm)
                self._set_computed("audio_instrumentation", instruments)
                self._set_computed("audio_style_prompt", audio_prompt)

                composed_hints = dict(self.hints or {})
                composed_hints["genre"] = composed_hints.get("genre") or genre_family
                composed_hints["mood"] = composed_hints.get("mood") or mood
                composed_hints["tempo_bpm"] = composed_hints.get("tempo_bpm") or bpm
                composed_hints["instrumentation"] = composed_hints.get("instrumentation") or instruments
                composed_hints["audio_prompt"] = audio_prompt
                composed_hints["music_prompt"] = audio_prompt
                composed_hints["prompt"] = composed_hints.get("prompt") or audio_prompt

                ly = str(self._computed().get("lyrics_text") or "").strip()
                if ly:
                    composed_hints.setdefault("lyrics_text", ly)

                res: AutopilotComposeResult = await compose_full_mix_fal_sonauto_v2(  # type: ignore
                    user_id=str(self.user_id),
                    project_id=str(self.project_id),
                    job_id=str(self.job_id),
                    language_hint=getattr(s, "language_hint", None),
                    quality=str(self.quality or "standard"),
                    seed=seed_i,
                    hints=composed_hints,
                    computed=self._computed(),
                )

                sas = str(getattr(res, "sas_url", "") or "").strip() or None
                dur_ms = int(getattr(res, "duration_ms", 0) or 0) or 30_000
                ct = str(getattr(res, "content_type", "audio/mpeg") or "audio/mpeg")
                provider_used = str(getattr(res, "provider", "fal_sonauto_v2") or "fal_sonauto_v2")

                if not sas:
                    raise RuntimeError("autopilot_provider_missing_sas_url")

                # Guard: autopilot returned fallback-ish audio URL (should never happen, but protect anyway)
                if _is_fallback_full_mix_url(sas) and not allow_fallback:
                    raise RuntimeError("autopilot_returned_fallback_audio_url")

                self._set_computed("audio_provider", provider_used)
                self._set_computed("provider_request_id", getattr(res, "provider_request_id", None))
                self._set_computed("audio_master_url", sas)
                self._set_computed("byo_audio_url", sas)
                self._set_computed("audio_master_duration_ms", dur_ms)
                self._set_computed("audio_duration_ms", dur_ms)
                self._set_computed("audio_content_type", ct)
                self._set_computed("audio_source", "autopilot_provider")
                self._set_computed("audio_is_fallback", False)
                self._set_computed("audio_fallback_reason", None)
                self._set_computed("audio_gen_error", None)

                self._set_audio_probe_from_known_duration(duration_ms=int(dur_ms))

                return [
                    GraphTrack(
                        track_type=MusicTrackType.full_mix.value,
                        duration_ms=int(dur_ms),
                        artifact_id=None,
                        media_asset_id=None,
                        meta={
                            "audio_duration_ms": int(dur_ms),
                            "url": str(sas),
                            "content_type": ct,
                            "source": "autopilot",
                            "provider": provider_used,
                            "is_demo": False,
                        },
                    )
                ]
            except Exception as e:
                err = f"{type(e).__name__}:{e}"
                self._set_computed("autopilot_provider_error", err)
                self._set_computed("audio_gen_error", err)
                if not allow_fallback:
                    raise RuntimeError(f"music_provider_failed:{err}") from e

        # DEV fallback (explicit opt-in)
        if not allow_fallback:
            # Should be unreachable due to guards above, but keep explicit.
            raise RuntimeError("music_provider_failed:refusing_fallback_audio")

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
        self._set_computed("audio_is_fallback", True)
        self._set_computed("audio_fallback_reason", str(self._computed().get("audio_gen_error") or self._computed().get("autopilot_provider_error") or "fallback_enabled"))

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

    # -------- lyrics alignment --------
    async def align_lyrics(self, s: MusicGraphState) -> Optional[GraphTrack]:
        outputs = self._get_requested_outputs(s)
        if MusicTrackType.timed_lyrics_json.value not in outputs:
            return None

        computed = self._computed()
        lyrics_text = str(computed.get("lyrics_text") or self.hints.get("lyrics_text") or self.hints.get("lyrics") or "").strip()
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
                    timed = await self._align_real(audio_url=audio_url, lyrics_text=lyrics_text, language=getattr(s, "language_hint", None))  # type: ignore
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

    # -------- performer videos (graph node compatibility) --------
    async def generate_performer_videos(self, s: MusicGraphState) -> Dict[str, Any]:
        """
        Performer generation via svc-fusion (per openapi.json):
          POST /jobs   (Bearer)
          GET  /jobs/{job_id}

        Quality rule:
          - If audio is fallback and fallback is NOT explicitly allowed, fail/skip (never ship rubbish).
        """
        computed = self._computed()
        allow_fallback = _allow_fallback_audio(quality=self.quality)

        # If audio is fallback and we're not in fallback-allowed dev mode, block performer path.
        if _is_truthy(computed.get("audio_is_fallback")) and not allow_fallback:
            self._set_computed("performer_videos_skipped", True)
            self._set_computed("performer_videos_skip_reason", "audio_is_fallback_refusing_performer")
            self._set_computed("performer_videos", [])
            raise RuntimeError("performer_video_blocked:fallback_audio_not_allowed")

        try:
            import httpx  # type: ignore
        except Exception as e:
            self._set_computed("performer_videos_skipped", True)
            self._set_computed("performer_videos_skip_reason", "httpx_missing")
            self._set_computed("performer_videos", [])
            if _env_bool("DF_REQUIRE_PERFORMER_VIDEOS", False):
                raise RuntimeError("performer_videos_require_httpx") from e
            return {"performer_videos": []}

        enable = _env_bool("DF_ENABLE_PERFORMER_VIDEOS", False) or _is_truthy(self.hints.get("enable_performer_videos"))
        require = _env_bool("DF_REQUIRE_PERFORMER_VIDEOS", False) or _is_truthy(self.hints.get("require_performer_videos"))

        if not enable and not require:
            self._set_computed("performer_videos_skipped", True)
            self._set_computed("performer_videos_skip_reason", "disabled")
            self._set_computed("performer_videos", [])
            return {"performer_videos": []}

        def _extract_job_id(obj: Dict[str, Any]) -> str:
            for k in ("job_id", "id"):
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            d = _as_dict(obj.get("data"))
            v2 = d.get("job_id") or d.get("id")
            return str(v2 or "").strip()

        def _extract_status(obj: Dict[str, Any]) -> str:
            s0 = str(obj.get("status") or "").strip().lower()
            if s0:
                return s0
            d = _as_dict(obj.get("data"))
            return str(d.get("status") or "").strip().lower()

        def _extract_video_url(obj: Dict[str, Any]) -> Optional[str]:
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
            d = _as_dict(obj.get("data"))
            for k in ("video_url", "final_url", "url", "preview_url", "mp4_url"):
                v = d.get(k)
                if isinstance(v, str) and v.strip().startswith("http"):
                    return v.strip()
            return None

        def _env_bearer() -> str:
            raw = (
                _env_str("DF_INTERNAL_BEARER_TOKEN")
                or _env_str("DF_FUSION_BEARER_TOKEN")
                or _env_str("DF_AUTH_TOKEN")
                or _env_str("BEARER_TOKEN")
            )
            if not raw:
                return ""
            return raw if raw.lower().startswith("bearer ") else f"Bearer {raw}"

        # ---- face ref ----
        perf_imgs = computed.get("performer_images")
        perf_imgs = perf_imgs if isinstance(perf_imgs, list) else []
        perf_img0 = str(perf_imgs[0]).strip() if perf_imgs else ""

        face_image_url = _first_http_url(
            os.getenv("DF_PERFORMER_FACE_IMAGE_URL"),
            self.hints.get("performer_face_image_url"),
            computed.get("performer_face_image_url"),
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

        # ---- audio url ----
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

        # If the audio itself is the known fallback URL, block unless explicitly allowed
        if _is_fallback_full_mix_url(audio_url) and not allow_fallback:
            self._set_computed("performer_videos_skipped", True)
            self._set_computed("performer_videos_skip_reason", "audio_url_is_fallback_refusing_performer")
            self._set_computed("performer_videos", [])
            raise RuntimeError("performer_video_blocked:fallback_audio_url")

        # ---- fusion contract ----
        fusion_base = _get_fusion_base()
        create_path = (_env_str("DF_FUSION_CREATE_PATH", "/jobs") or "/jobs").strip()
        poll_prefix = (_env_str("DF_FUSION_POLL_PATH_PREFIX", "/jobs") or "/jobs").strip()
        if not create_path.startswith("/"):
            create_path = "/" + create_path
        if not poll_prefix.startswith("/"):
            poll_prefix = "/" + poll_prefix

        provider = str(os.getenv("DF_FUSION_PROVIDER") or self.hints.get("fusion_provider") or "heygen_av4")
        user_id = str(getattr(s, "user_id", "") or self.user_id)

        payload: Dict[str, Any] = {
            "voice_mode": "audio",
            "voice_audio": {"audio_url": audio_url},
            "provider": provider,
            "consent": {"external_provider_ok": True},
            "video": {"aspect_ratio": "16:9", "motion_style": "performance", "emotion": "confident"},
            "tags": {"source": "svc-music", "music_job_id": str(self.job_id), "music_project_id": str(self.project_id), "purpose": "performer_video"},
        }
        if face_image_url:
            payload["face_image_url"] = face_image_url
        if face_artifact_id:
            payload["face_artifact_id"] = face_artifact_id

        # ---- auth (env -> core login cache) ----
        cache = getattr(self, "_fusion_token_cache", None)
        if not isinstance(cache, dict):
            cache = {"token": "", "exp": 0.0}
            setattr(self, "_fusion_token_cache", cache)

        def _token_fresh(exp: float) -> bool:
            return float(exp or 0.0) > (time.time() + 30.0)

        async def _mint_token_via_core() -> str:
            core = (_env_str("DF_CORE_URL", "http://svc-core:8000") or "http://svc-core:8000").rstrip("/")
            email = _env_str("DF_SERVICE_EMAIL", "")
            password = _env_str("DF_SERVICE_PASSWORD", "")
            if not email or not password:
                raise RuntimeError("missing_DF_SERVICE_EMAIL_or_DF_SERVICE_PASSWORD_for_fusion_auth")

            payload_login = {
                "email": email,
                "password": password,
                "device_id": (_env_str("DF_AUTH_DEVICE_ID", "svc-music-worker") or "svc-music-worker").strip(),
                # IMPORTANT: DB only allows web|ios|android; default ios to avoid internal defaults
                "client_type": _normalize_client_type(_env_str("DF_AUTH_CLIENT_TYPE", "ios") or "ios", default="ios"),
            }
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(f"{core}/api/auth/login", json=payload_login)
                if r.status_code >= 400:
                    raise RuntimeError(f"core_login_failed:{r.status_code}:{(r.text or '')[:200]}")
                j_any = r.json()
                j = j_any if isinstance(j_any, dict) else {}
                tok = str(j.get("access_token") or j.get("token") or "").strip()
                if not tok:
                    raise RuntimeError("core_login_missing_access_token")
                exp = _jwt_exp_epoch_seconds(tok) or (time.time() + 900.0)
                cache["token"] = tok
                cache["exp"] = exp
                return f"Bearer {tok}"

        bearer = _env_bearer()
        if not bearer:
            if cache.get("token") and _token_fresh(float(cache.get("exp") or 0.0)):
                bearer = f"Bearer {cache['token']}"
            else:
                bearer = await _mint_token_via_core()

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": bearer,
            "X-User-Id": user_id,
            "X-Request-Source": "svc-music",
        }

        self._set_computed("performer_video_fusion_base", fusion_base)
        self._set_computed("performer_video_create_path", create_path)
        self._set_computed("performer_video_poll_prefix", poll_prefix)

        timeout_s = _coerce_int(_env_str("DF_FUSION_CREATE_TIMEOUT_SECS", "60"), 60)
        poll_timeout_s = _coerce_int(_env_str("DF_FUSION_TIMEOUT_SECS", "900"), 900)
        poll_every_s = float(_env_str("DF_FUSION_POLL_SECS", "5") or "5")

        create_url = f"{fusion_base}{create_path}"
        poll_base = f"{fusion_base}{poll_prefix}"

        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
            r = await client.post(create_url, headers=headers, json=payload)
            if r.status_code in (401, 403) and not _env_bearer():
                bearer = await _mint_token_via_core()
                headers["Authorization"] = bearer
                r = await client.post(create_url, headers=headers, json=payload)

            if r.status_code >= 400:
                self._set_computed("performer_videos_skipped", True)
                self._set_computed("performer_videos_skip_reason", f"fusion_create_failed:{r.status_code}:{(r.text or '')[:200]}")
                self._set_computed("performer_videos", [])
                if require:
                    raise RuntimeError(f"fusion_create_failed:{r.status_code}:{(r.text or '')[:120]}")
                return {"performer_videos": []}

            created_any = r.json()
            created = created_any if isinstance(created_any, dict) else {}
            fjid = _extract_job_id(created)
            if not fjid:
                self._set_computed("performer_videos_skipped", True)
                self._set_computed("performer_videos_skip_reason", "fusion_create_missing_job_id")
                self._set_computed("performer_videos", [])
                if require:
                    raise RuntimeError("fusion_create_missing_job_id")
                return {"performer_videos": []}

            t0 = time.time()
            while True:
                if time.time() - t0 > poll_timeout_s:
                    self._set_computed("performer_videos_skipped", True)
                    self._set_computed("performer_videos_skip_reason", "fusion_timeout")
                    self._set_computed("performer_videos", [])
                    if require:
                        raise RuntimeError("fusion_timeout")
                    return {"performer_videos": []}

                rr = await client.get(f"{poll_base}/{fjid}", headers=headers)
                if rr.status_code >= 400:
                    await asyncio.sleep(poll_every_s)
                    continue

                last_any = rr.json()
                last = last_any if isinstance(last_any, dict) else {}
                st = _extract_status(last)

                if st in ("succeeded", "success", "completed", "done"):
                    vid = _extract_video_url(last)
                    if not vid:
                        self._set_computed("performer_videos_skipped", True)
                        self._set_computed("performer_videos_skip_reason", "fusion_succeeded_missing_video_url")
                        self._set_computed("performer_videos", [])
                        if require:
                            raise RuntimeError("fusion_succeeded_missing_video_url")
                        return {"performer_videos": []}

                    item = {"video_url": vid, "provider": provider, "fusion_job_id": fjid}
                    self._set_computed("performer_videos_skipped", False)
                    self._set_computed("performer_videos_skip_reason", None)
                    self._set_computed("performer_videos", [item])
                    self._set_computed("performer_video_url", vid)
                    self._set_computed("performer_video_job_id", fjid)
                    return {"performer_videos": [item]}

                    if st in ("failed", "error", "canceled", "cancelled"):
                        err = str(last.get("error_message") or last.get("error") or last.get("message") or "fusion_failed")

                        # Soft-skip credit/quota errors in dev unless performer videos are required
                        credit_signals = (
                            "INSUFFICIENT_CREDIT" in err.upper()
                            or "PAYMENT_INSUFFICIENT" in err.upper()
                            or "QUOTA" in err.upper()
                        )

                        self._set_computed("performer_videos_skipped", True)
                        self._set_computed("performer_videos_skip_reason", f"fusion_failed:{err[:240]}")
                        self._set_computed("performer_videos", [])

                        if require and not credit_signals:
                            raise RuntimeError(f"fusion_failed:{err}")

                        # If not required OR it's a credit/quota issue, continue pipeline
                        if require and credit_signals:
                            # still record as skipped but don't fail the job
                            return {"performer_videos": []}

                        return {"performer_videos": []}

                await asyncio.sleep(poll_every_s)

    # -------- compose_video + qc --------
    async def compose_video(self, s: MusicGraphState) -> Dict[str, Any]:
        computed = self._computed()
        existing = computed.get("clip_manifest")
        if isinstance(existing, dict) and isinstance(existing.get("clips"), list) and existing["clips"]:
            self._set_computed("compose_video_skipped", True)
            return {"skipped": True, "reason": "clip_manifest_already_present"}
        self._set_computed("compose_video_skipped", True)
        return {"skipped": True, "reason": "compose_video_disabled_in_v1_use_clip_manifest_service"}

    async def qc(self, s: MusicGraphState) -> Dict[str, Any]:
        """
        DesiFaces quality gate:
          - MUST have a real full_mix track.
          - MUST NOT be fallback audio unless DF_ALLOW_FALLBACK_AUDIO=1 (dev only).
        """
        allow_fallback = _allow_fallback_audio(quality=self.quality)

        tracks = getattr(s, "tracks", []) or []
        full_mix_url = ""
        have_full = False
        for t in tracks:
            if str(getattr(t, "track_type", "")) == MusicTrackType.full_mix.value:
                have_full = True
                meta = getattr(t, "meta", None)
                md = _as_dict(meta)
                full_mix_url = str(md.get("url") or md.get("sas_url") or md.get("storage_ref") or "").strip()
                break

        if not have_full:
            c = self._computed()
            if not (c.get("audio_master_url") or c.get("byo_audio_url") or c.get("demo_audio_url")):
                raise RuntimeError("qc_failed_missing_full_mix")

        c2 = self._computed()
        # If computed says it's fallback, block unless dev explicitly allows
        if _is_truthy(c2.get("audio_is_fallback")) and not allow_fallback:
            raise RuntimeError("qc_failed_audio_is_fallback")

        if full_mix_url and _is_fallback_full_mix_url(full_mix_url) and not allow_fallback:
            raise RuntimeError("qc_failed_full_mix_is_fallback")

        return {"ok": True}