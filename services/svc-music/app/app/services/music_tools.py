from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import wave
import logging
from pathlib import Path

from typing import Optional, Any, Dict, List, Tuple
from uuid import UUID

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
        # If the external provider module isn't installed, do NOT advertise fal provider here.
        return "native"

    compose_full_mix_fal_sonauto_v2 = None  # type: ignore


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

def _normalize_mode(val: Any) -> str:
    # allow enum inputs without producing "MusicProjectMode.autopilot"
    v = getattr(val, "value", val)
    s = str(v or "").strip()
    if not s:
        return MusicProjectMode.autopilot.value
    return s.lower()

def _outputs_set(outputs: List[str]) -> set[str]:
    return {str(x).strip().lower() for x in (outputs or []) if x}

def _get_byo_audio(hints: Dict[str, Any], input_json: Dict[str, Any] | None = None) -> Tuple[Optional[str], Optional[int]]:
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


class ConcreteMusicTools(MusicGraphTools):
    """
    Updated behavior to match modes:

    - autopilot/co_create:
        lyrics default generate (user doesn't need to type)
    - byo:
        lyrics default none (unless user uploads lyrics or timed_lyrics_json requested)
    - timed_lyrics_json requested:
        if lyrics missing, we auto-generate to allow alignment to succeed
    """

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

    def _computed(self) -> Dict[str, Any]:
        return _as_dict(self.input_json.get("computed"))

    def _set_computed(self, key: str, value: Any) -> None:
        c = _as_dict(self.input_json.get("computed"))
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

        plan_payload: Any = None
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
        # Pure-python WAV fallback if ffmpeg is not available.
        duration_ms = max(1000, int(duration_ms or 30_000))
        frames = int(sample_rate * (duration_ms / 1000.0))
        silence = b"\x00\x00" * frames  # 16-bit mono silence
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(silence)

    async def _generate_native_fallback_full_mix(self, *, duration_ms: int) -> Tuple[str, int, str]:
        """
        Generate deterministic fallback audio and upload to Azure music-output.
        Prefers ffmpeg if available, otherwise uses pure-python wav writer.
        Returns (sas_url, duration_ms, content_type).
        """
        duration_ms = max(1000, int(duration_ms or 30_000))
        dur_s = max(1, int(round(duration_ms / 1000.0)))

        out_dir = Path("/tmp/df_music_native")
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

        storage = AzureStorageService.for_output()
        sas_url = await storage.upload_music_fallback_audio_and_get_sas_url(
            user_id=str(self.user_id),
            project_id=str(self.project_id),
            job_id=str(self.job_id),
            local_path=out_path,
            content_type="audio/wav",
            blob_filename="fallback_full_mix.wav",
        )

        return (str(sas_url), duration_ms, "audio/wav")

    async def generate_audio(self, s: MusicGraphState) -> List[GraphTrack]:
        """
        Contract: MUST produce at least one 'full_mix' track (or raise).

        Separation:
          - audio_master_url/byo_audio_url = actual song audio
          - voice_ref_url = voice reference only
          - demo_use_voice_ref_as_audio=true can optionally use voice_ref_url as audio for demos
        """
        computed = self._computed()
        mode = self._get_mode(s)

        audio_url, audio_dur = _get_byo_audio(self.hints, self.input_json)

        if not audio_url:
            audio_url = computed.get("audio_master_url") or computed.get("byo_audio_url") or computed.get("demo_audio_url")

        demo_audio_url = None
        if not audio_url and self._demo_use_voice_ref_as_audio():
            vr = computed.get("voice_ref_url")
            if vr:
                demo_audio_url = str(vr)

        final_audio_url = audio_url or demo_audio_url

        # ---- BYO (or already have master audio) ----
        if mode == MusicProjectMode.byo.value or final_audio_url:
            if not final_audio_url:
                raise Exception("missing_audio_master_url")

            if not audio_dur:
                audio_dur = int(
                    self.input_json.get("audio_master_duration_ms")
                    or self.hints.get("audio_master_duration_ms")
                    or computed.get("audio_master_duration_ms")
                    or 0
                ) or 30_000

            ct = _guess_audio_content_type(str(final_audio_url) if final_audio_url else None, "audio/mpeg")

            meta: Dict[str, Any] = {
                "audio_duration_ms": int(audio_dur),
                "url": str(final_audio_url),
                "content_type": ct,
                "audio_master_url": str(final_audio_url),
                "byo_audio_url": str(final_audio_url),
                "byo_duration_ms": int(audio_dur),
            }

            if demo_audio_url:
                meta.update(
                    {
                        "demo_audio_url": str(demo_audio_url),
                        "audio_source": "demo_voice_ref_url",
                        "is_demo": True,
                    }
                )
            else:
                meta.update(
                    {
                        "audio_source": "hints" if _get_byo_audio(self.hints, self.input_json)[0] else "computed",
                        "source": "byo",
                    }
                )

            return [
                GraphTrack(
                    track_type=MusicTrackType.full_mix.value,
                    duration_ms=int(audio_dur),
                    artifact_id=None,
                    media_asset_id=None,
                    meta=meta,
                )
            ]

        # ---- AUTOPILOT / CO_CREATE ----
        provider = normalize_provider(
            self.hints.get("music_provider")
            or self.hints.get("provider")
            or computed.get("audio_provider")
            or getattr(settings, "MUSIC_AUTOPILOT_PROVIDER", None)
            or default_autopilot_provider()
        )

        # 1) Try Fal Sonauto v2 if available/configured
        if provider in ("fal_sonauto_v2", "sonauto_v2", "sonauto") and callable(compose_full_mix_fal_sonauto_v2):
            try:
                seed_i: Optional[int] = None
                if self.seed is not None:
                    seed_i = int(float(self.seed))

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

                # Persist for downstream publish/status consistency
                self._set_computed("audio_provider", getattr(res, "provider", "fal_sonauto_v2"))
                self._set_computed("provider_request_id", getattr(res, "provider_request_id", None))
                self._set_computed("audio_master_url", getattr(res, "sas_url", None))
                self._set_computed("byo_audio_url", getattr(res, "sas_url", None))
                self._set_computed("audio_master_duration_ms", int(getattr(res, "duration_ms", 0) or 0))

                prov_lyrics = getattr(res, "lyrics", None)
                if prov_lyrics and not str(self._computed().get("lyrics_text") or "").strip():
                    self._set_computed("lyrics_text", str(prov_lyrics))
                    self._set_computed("lyrics_source_effective", "generate")

                meta2: Dict[str, Any] = {
                    "audio_duration_ms": int(getattr(res, "duration_ms", 0) or 0),
                    "url": str(getattr(res, "sas_url", "")),
                    "content_type": str(getattr(res, "content_type", "audio/mpeg") or "audio/mpeg"),
                    "audio_master_url": str(getattr(res, "sas_url", "")),
                    "byo_audio_url": str(getattr(res, "sas_url", "")),
                    "byo_duration_ms": int(getattr(res, "duration_ms", 0) or 0),
                    "audio_source": "autopilot_provider",
                    "provider": str(getattr(res, "provider", "fal_sonauto_v2")),
                    "provider_request_id": getattr(res, "provider_request_id", None),
                    "provider_seed": int(getattr(res, "provider_seed", 0) or 0),
                    "provider_tags": list(getattr(res, "tags", []) or []),
                    "source_url": getattr(res, "source_url", None),
                    "is_demo": False,
                    "source": "autopilot",
                }

                dur_ms = int(getattr(res, "duration_ms", 0) or 0) or 30_000
                return [
                    GraphTrack(
                        track_type=MusicTrackType.full_mix.value,
                        duration_ms=dur_ms,
                        artifact_id=None,
                        media_asset_id=None,
                        meta=meta2,
                    )
                ]
            except Exception as e:
                self._set_computed("autopilot_provider_error", str(e))

        # 2) Always-works native fallback so pipeline never fails
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

        meta2: Dict[str, Any] = {
            "audio_duration_ms": int(fallback_dur_ms),
            "url": fallback_url,
            "content_type": ct,
            "audio_master_url": fallback_url,
            "byo_audio_url": fallback_url,
            "byo_duration_ms": int(fallback_dur_ms),
            "audio_source": "fallback_native",
            "provider": "native",
            "source": "autopilot",
            "is_demo": True,
        }

        return [
            GraphTrack(
                track_type=MusicTrackType.full_mix.value,
                duration_ms=int(fallback_dur_ms),
                artifact_id=None,
                media_asset_id=None,
                meta=meta2,
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
        # IMPORTANT: avoid bool("false") == True
        render_video = _is_truthy(self.hints.get("render_video") or self.hints.get("generate_video"))
        if not render_video:
            return {"skipped": True}
        return {"skipped": True, "reason": "performer_video_not_implemented"}

    async def compose_video(self, s: MusicGraphState) -> Dict[str, Any]:
        """
        Compose step (Phase-1): build clip manifest for svc-fusion-extension.
        Does NOT render video yet — just produces deterministic manifest + persists to computed.
        """
        render_video = _is_truthy(self.hints.get("render_video") or self.hints.get("generate_video"))
        if not render_video:
            # Persist skip reason so status/debug can explain why no manifest exists
            self._set_computed("compose_video_skipped", True)
            s.computed = self._computed()
            return {"skipped": True}

        from app.services.video_directory import build_clip_manifest, validate_manifest, validate_music_plan

        # Read computed safely (but persist via _set_computed)
        computed = self._computed()

        # Accept dict or JSON-string; tolerate missing keys
        music_plan_raw = computed.get("music_plan") or computed.get("plan") or {}
        music_plan = _as_dict(music_plan_raw)

        mode = self._get_mode(s) or str(self.hints.get("mode") or computed.get("mode") or "")
        language_hint = (
            self.hints.get("language_hint")
            or self.hints.get("language")
            or getattr(s, "language_hint", None)
            or computed.get("language_hint")
            or computed.get("language")
        )
        duet_layout = str(self.hints.get("duet_layout") or computed.get("duet_layout") or "split_screen")
        quality = str(self.hints.get("quality") or computed.get("quality") or "standard")

        seed_raw = self.hints.get("seed") if "seed" in self.hints else computed.get("seed")
        seed: Optional[int] = None
        try:
            if seed_raw is not None:
                seed = int(float(seed_raw))
        except Exception:
            seed = None

        exports = (
            self.hints.get("exports")
            or self.hints.get("export_aspects")
            or self.hints.get("aspects")
            or computed.get("exports")
            or computed.get("export_aspects")
        )

        audio_duration_ms = (
            computed.get("audio_duration_ms")
            or computed.get("song_duration_ms")
            or computed.get("track_duration_ms")
            or computed.get("audio_master_duration_ms")
        )
        try:
            audio_duration_ms = int(float(audio_duration_ms)) if audio_duration_ms is not None else None
        except Exception:
            audio_duration_ms = None

        no_face = _is_truthy(self.hints.get("no_face") or self.hints.get("no_lip_sync") or self.hints.get("faceless_video"))

        clip_manifest = build_clip_manifest(
            music_plan=music_plan,
            mode=str(mode or ""),
            language_hint=str(language_hint) if language_hint else None,
            duet_layout=duet_layout,
            quality=quality,
            seed=seed,
            exports=exports,
            audio_duration_ms=audio_duration_ms,
            no_face=no_face,
        )

        # warn-only validation (never block UX)
        plan_warnings = validate_music_plan(music_plan)
        if plan_warnings:
            logger.warning("music_plan warnings: %s", plan_warnings[:50])

        manifest_warnings = validate_manifest(clip_manifest)
        if manifest_warnings:
            logger.warning("clip_manifest warnings: %s", manifest_warnings[:50])

        # ✅ CRITICAL: persist into DB-backed computed (not just s.computed)
        self._set_computed("clip_manifest", clip_manifest)
        self._set_computed("compose_video_skipped", False)
        self._set_computed("compose_video_warnings", {"plan": plan_warnings, "manifest": manifest_warnings})

        # keep state in sync too
        s.computed = self._computed()

        return {"skipped": False, "clip_manifest": clip_manifest}



    async def qc(self, s: MusicGraphState) -> Dict[str, Any]:
        have_full = any(
            str(getattr(t, "track_type", "")) == MusicTrackType.full_mix.value for t in getattr(s, "tracks", []) or []
        )
        if not have_full:
            raise Exception("qc_failed_missing_full_mix")
        return {"ok": True}