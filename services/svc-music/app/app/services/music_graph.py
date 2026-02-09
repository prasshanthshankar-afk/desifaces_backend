from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Protocol, Tuple
from uuid import UUID

from app.domain.enums import MusicJobStage, MusicProjectMode, MusicTrackType


# -----------------------------
# Types (ports)
# -----------------------------
class JobsPort(Protocol):
    async def set_video_job_progress(self, *, job_id: UUID, progress: int) -> None: ...


class StepsPort(Protocol):
    async def upsert_step(
        self,
        *,
        job_id: UUID,
        step_code: str,
        status: str,
        meta_json: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
    ) -> None: ...


# -----------------------------
# Data model used by the pipeline
# -----------------------------
def _as_str(x: Any) -> str:
    return "" if x is None else str(x)


def _norm(s: Any) -> str:
    return _as_str(s).strip().lower()


def _safe_jsonable(x: Any) -> Any:
    """
    Ensure meta_json payloads are JSON-serializable (best effort).
    Never raise from here.
    """
    try:
        json.dumps(x)
        return x
    except Exception:
        try:
            return json.loads(json.dumps(x, default=str))
        except Exception:
            return {"_non_jsonable": True, "repr": repr(x)}


def _output_set(outputs: Iterable[Any]) -> set[str]:
    """
    Normalize outputs into a set of lowercase strings.
    Supports either ["timed_lyrics_json"] or [Enum] (Enum.value).
    """
    out: set[str] = set()
    for x in outputs or []:
        if x is None:
            continue
        v = getattr(x, "value", x)  # Enum -> value
        v = _norm(v)
        if v:
            out.add(v)
    return out


def _ensure_outputs(requested_outputs: List[Any]) -> List[str]:
    outs = list(requested_outputs or [])
    out_set = _output_set(outs)
    if MusicTrackType.full_mix.value not in out_set:
        outs.append(MusicTrackType.full_mix.value)
    # de-dupe while preserving order
    seen: set[str] = set()
    result: List[str] = []
    for x in outs:
        v = _norm(getattr(x, "value", x))
        if not v or v in seen:
            continue
        seen.add(v)
        result.append(v)
    return result


def _normalize_mode(mode: Any) -> str:
    m = _norm(getattr(mode, "value", mode))
    if m in (MusicProjectMode.autopilot.value, MusicProjectMode.co_create.value, MusicProjectMode.byo.value):
        return m
    # default safe behavior
    return MusicProjectMode.autopilot.value


@dataclass
class GraphTrack:
    # Keep this as str for runtime flexibility (DB + JSON); enforce via values from MusicTrackType.*
    track_type: str
    duration_ms: int
    artifact_id: Optional[UUID] = None
    media_asset_id: Optional[UUID] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def track_type_norm(self) -> str:
        return _norm(self.track_type)


@dataclass
class MusicGraphState:
    job_id: UUID
    project_id: UUID
    user_id: UUID

    # project-level intent
    mode: str  # normalized string (autopilot|co_create|byo)
    duet_layout: str
    language_hint: Optional[str] = None
    scene_pack_id: Optional[str] = None
    camera_edit: str = "beat_cut"
    band_pack: List[str] = field(default_factory=list)

    # sources (mode-aware defaults applied in _normalize_sources)
    # track_source: "autopilot" | "byo" | "library"
    track_source: Optional[str] = None
    # lyrics_source: "generate" | "upload" | "none"
    lyrics_source: Optional[str] = None

    # optional inline lyrics if provided by client or preloaded
    lyrics_text: Optional[str] = None

    # optional audio pointers for BYO/library workflows
    input_audio_artifact_id: Optional[UUID] = None
    input_audio_media_asset_id: Optional[UUID] = None
    input_audio_url: Optional[str] = None  # SAS if caller already has it

    # requested outputs (strings, MusicTrackType values)
    requested_outputs: List[str] = field(default_factory=list)

    # execution state
    stage: str = MusicJobStage.intent.value
    progress: int = 0
    tracks: List[GraphTrack] = field(default_factory=list)

    # optional video outputs
    performer_a_video_asset_id: Optional[UUID] = None
    performer_b_video_asset_id: Optional[UUID] = None
    preview_video_asset_id: Optional[UUID] = None
    final_video_asset_id: Optional[UUID] = None

    # pipeline meta: effective decisions, routing, diagnostics (always JSONable)
    meta: Dict[str, Any] = field(default_factory=dict)


class MusicGraphTools(Protocol):
    async def intent(self, s: MusicGraphState) -> Dict[str, Any]: ...
    async def creative_brief(self, s: MusicGraphState) -> Dict[str, Any]: ...
    async def lyrics(self, s: MusicGraphState) -> Dict[str, Any]: ...
    async def arrangement(self, s: MusicGraphState) -> Dict[str, Any]: ...
    async def route_provider(self, s: MusicGraphState) -> Dict[str, Any]: ...
    async def generate_audio(self, s: MusicGraphState) -> List[GraphTrack]: ...
    async def align_lyrics(self, s: MusicGraphState) -> Optional[GraphTrack]: ...
    async def generate_performer_videos(self, s: MusicGraphState) -> Dict[str, Any]: ...
    async def compose_video(self, s: MusicGraphState) -> Dict[str, Any]: ...
    async def qc(self, s: MusicGraphState) -> Dict[str, Any]: ...


# -----------------------------
# Pipeline logic
# -----------------------------
def _bump(s: MusicGraphState, stage: str, progress: int) -> None:
    s.stage = stage
    s.progress = max(0, min(100, int(progress)))


def _normalize_sources(s: MusicGraphState) -> None:
    """
    Apply mode-aware defaults once per run so tools & pipeline are deterministic.

    AUTOPILOT:
      track_source=autopilot
      lyrics_source=generate (unless lyrics_text provided => upload)

    CO_CREATE:
      track_source=autopilot
      lyrics_source=generate by default (lyrics_text => upload)

    BYO:
      track_source=byo
      lyrics_source=none by default (lyrics_text => upload)
    """
    mode = _normalize_mode(s.mode)
    s.mode = mode

    if not s.track_source:
        s.track_source = "byo" if mode == MusicProjectMode.byo.value else "autopilot"
    else:
        s.track_source = _norm(s.track_source) or ("byo" if mode == MusicProjectMode.byo.value else "autopilot")

    if not s.lyrics_source:
        if mode == MusicProjectMode.byo.value:
            s.lyrics_source = "upload" if (_as_str(s.lyrics_text).strip()) else "none"
        else:
            s.lyrics_source = "upload" if (_as_str(s.lyrics_text).strip()) else "generate"
    else:
        s.lyrics_source = _norm(s.lyrics_source) or ("generate" if mode != MusicProjectMode.byo.value else "none")


def _resolve_lyrics_effective(s: MusicGraphState, outputs: set[str]) -> Tuple[str, bool, bool]:
    """
    Returns:
      (effective_lyrics_source, run_lyrics_step, run_align_step)

    Rules:
    - If timed_lyrics_json requested, we MUST have lyrics. If user said "none", override to "generate".
    - Otherwise:
        * "none" => skip lyrics and skip align
        * "upload"/"generate" => run lyrics, align only if requested
    """
    requested_timed = MusicTrackType.timed_lyrics_json.value in outputs
    src = _norm(s.lyrics_source)

    if requested_timed and src == "none":
        src = "generate"

    run_lyrics = requested_timed or (src in ("generate", "upload"))
    run_align = requested_timed and (src != "none")
    return src, run_lyrics, run_align


def _has_track(s: MusicGraphState, track_type: str) -> bool:
    tt = _norm(track_type)
    return any(t.track_type_norm() == tt for t in (s.tracks or []))


async def run_video_pipeline(
    state: MusicGraphState,
    tools: MusicGraphTools,
    *,
    jobs: JobsPort,
    steps: StepsPort,
) -> MusicGraphState:
    """
    Deterministic, production-grade pipeline runner.

    Design goals:
      - Idempotent-ish: re-runs don't duplicate tracks (tools should upsert downstream).
      - Strong observability: every step writes a step row with status + meta_json.
      - Mode-aware: BYO vs AUTOPILOT/CO_CREATE behavior controlled via lyrics/track sources.
      - Safe meta: no non-JSONable payloads in DB.
      - Easy future swap to LangGraph: this file already models a state-machine w/ node steps.
    """
    _normalize_sources(state)

    # Normalize outputs and store in state (strings)
    state.requested_outputs = _ensure_outputs(state.requested_outputs)
    outputs = _output_set(state.requested_outputs)

    effective_lyrics_source, run_lyrics_step, run_align_step = _resolve_lyrics_effective(state, outputs)
    state.meta.update(
        _safe_jsonable(
            {
                "effective_lyrics_source": effective_lyrics_source,
                "effective_track_source": state.track_source,
                "requested_outputs": list(sorted(outputs)),
            }
        )
    )

    async def _safe_step_upsert(
        *, step_code: str, status: str, meta: Optional[Dict[str, Any]] = None, err: Optional[str] = None
    ) -> None:
        try:
            await steps.upsert_step(
                job_id=state.job_id,
                step_code=step_code,
                status=status,
                meta_json=_safe_jsonable(meta or {"progress": state.progress}),
                error_message=err,
            )
        except Exception:
            # Observability should never break the pipeline itself.
            return None

    async def _safe_progress_update() -> None:
        try:
            await jobs.set_video_job_progress(job_id=state.job_id, progress=state.progress)
        except Exception:
            return None

    async def mark(stage: str, progress: int) -> None:
        _bump(state, stage, progress)
        await _safe_progress_update()
        await _safe_step_upsert(step_code=stage, status="running", meta={"progress": state.progress})

    async def succeed(stage: str, payload: Any = None) -> None:
        meta: Dict[str, Any] = {"progress": state.progress}
        if payload is not None:
            meta["output"] = payload
        await _safe_step_upsert(step_code=stage, status="succeeded", meta=meta)

    async def skip(stage: str, reason: str) -> None:
        await _safe_step_upsert(step_code=stage, status="skipped", meta={"progress": state.progress, "reason": reason})

    async def fail(stage: str, err: Exception) -> None:
        await _safe_step_upsert(
            step_code=stage,
            status="failed",
            meta={"progress": state.progress, "error": str(err)},
            err=str(err),
        )

    async def run_step(
        stage: str,
        progress: int,
        fn: Callable[[], Awaitable[Any]],
        *,
        should_run: bool = True,
        skip_reason: str = "condition_not_met",
    ) -> Any:
        await mark(stage, progress)
        if not should_run:
            await skip(stage, skip_reason)
            return None
        try:
            result = await fn()
        except Exception as e:
            await fail(stage, e)
            raise
        else:
            await succeed(stage, payload=_safe_jsonable(result))
            return result

    # ---- Step plan (world-class: deterministic progress + guarded skips) ----
    await run_step(MusicJobStage.intent.value, 5, lambda: tools.intent(state))
    await run_step(MusicJobStage.creative_brief.value, 15, lambda: tools.creative_brief(state))

    await run_step(
        MusicJobStage.lyrics.value,
        30,
        lambda: tools.lyrics(state),
        should_run=run_lyrics_step,
        skip_reason="lyrics_not_required",
    )

    await run_step(MusicJobStage.arrangement.value, 40, lambda: tools.arrangement(state))
    await run_step(MusicJobStage.provider_route.value, 50, lambda: tools.route_provider(state))

    async def _gen_audio() -> Dict[str, Any]:
        # Avoid duplicate generation if a re-run already has full_mix in-memory
        if _has_track(state, MusicTrackType.full_mix.value):
            return {"skipped": True, "reason": "full_mix_already_present"}
        tracks = await tools.generate_audio(state)
        for t in tracks or []:
            if not t or not _norm(getattr(t, "track_type", "")):
                continue
            state.tracks.append(t)
        return {"tracks_added": len(tracks or [])}

    await run_step(MusicJobStage.generate_audio.value, 70, _gen_audio)

    async def _align() -> Dict[str, Any]:
        if not run_align_step:
            return {"skipped": True, "reason": "timed_lyrics_not_requested"}
        if _has_track(state, MusicTrackType.timed_lyrics_json.value):
            return {"skipped": True, "reason": "timed_lyrics_already_present"}
        t = await tools.align_lyrics(state)
        if t:
            state.tracks.append(t)
            return {"generated": True}
        return {"generated": False}

    await run_step(
        MusicJobStage.align_lyrics.value,
        80,
        _align,
        should_run=run_align_step,
        skip_reason="align_not_required",
    )

    await run_step(MusicJobStage.generate_performer_videos.value, 88, lambda: tools.generate_performer_videos(state))
    await run_step(MusicJobStage.compose_video.value, 95, lambda: tools.compose_video(state))
    await run_step(MusicJobStage.qc.value, 98, lambda: tools.qc(state))

    # publish marker
    await mark(MusicJobStage.publish.value, 100)
    await _safe_step_upsert(step_code=MusicJobStage.publish.value, status="succeeded", meta={"progress": 100})
    return state