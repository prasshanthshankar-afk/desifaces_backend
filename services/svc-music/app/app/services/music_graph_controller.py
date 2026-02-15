from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional, Tuple, List
from uuid import UUID, uuid4

from langgraph.graph import StateGraph, END

from app.db import get_pool
from app.domain.enums import MusicProjectMode, MusicTrackType, MusicJobStage
from app.repos.music_tracks_repo import MusicTracksRepo
from app.repos.provider_runs_repo import ProviderRunsRepo


STAGE = Literal[
    # common
    "intent",
    "plan",
    # autopilot/co-create
    "lyrics_fanout",
    "lyrics_fanin",
    "arrangement",
    "provider_route",
    "audio_fanout",
    "audio_fanin",
    "align_lyrics",
    "video_fanout",
    "video_fanin",
    "compose_video",
    "qc_video",
    "publish_ready",
    # BYO
    "ingest_audio",
    "byo_analysis_fanout",
    "byo_analysis_fanin",
]

_STAGE_SET = {
    "intent",
    "plan",
    "lyrics_fanout",
    "lyrics_fanin",
    "arrangement",
    "provider_route",
    "audio_fanout",
    "audio_fanin",
    "align_lyrics",
    "video_fanout",
    "video_fanin",
    "compose_video",
    "qc_video",
    "publish_ready",
    "ingest_audio",
    "byo_analysis_fanout",
    "byo_analysis_fanin",
}

_COL_CACHE: Dict[tuple[str, str], set[str]] = {}


# -----------------------------
# Utilities
# -----------------------------
def _as_dict(x: Any) -> Dict[str, Any]:
    if isinstance(x, dict):
        return x
    if isinstance(x, str) and x.strip():
        try:
            v = json.loads(x)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    return {}


def _as_list(x: Any) -> List[Any]:
    if isinstance(x, list):
        return x
    if isinstance(x, str) and x.strip():
        try:
            v = json.loads(x)
            return v if isinstance(v, list) else []
        except Exception:
            return []
    return []


def _idem(*, job_id: UUID, run_type: str, key: str) -> str:
    s = f"svc-music:{job_id}:{run_type}:{key}"
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _is_hitl_mode(mode: str) -> bool:
    m = str(mode or "").strip().lower()
    return m == MusicProjectMode.co_create.value


def _is_byo_mode(mode: str) -> bool:
    m = str(mode or "").strip().lower()
    return m == MusicProjectMode.byo.value


def _terminal_candidate_status(s: str) -> bool:
    return str(s or "").strip().lower() in ("succeeded", "failed", "discarded", "chosen", "abandoned")


def _progress_for(stage: str) -> int:
    m = {
        "intent": 5,
        "plan": 15,
        "ingest_audio": 18,
        "lyrics_fanout": 22,
        "lyrics_fanin": 30,
        "arrangement": 38,
        "provider_route": 45,
        "audio_fanout": 55,
        "audio_fanin": 70,
        "byo_analysis_fanout": 40,
        "byo_analysis_fanin": 55,
        "align_lyrics": 78,
        "video_fanout": 85,
        "video_fanin": 92,
        "compose_video": 95,
        "qc_video": 98,
        "publish_ready": 100,
    }
    return int(m.get(stage, 0))


def _safe_overall(score_json: Any) -> float:
    sj = score_json if isinstance(score_json, dict) else _as_dict(score_json)
    try:
        return float(sj.get("overall", 0.0))
    except Exception:
        return 0.0


def _outputs_set(input_json: Dict[str, Any]) -> set[str]:
    outs = _as_list(input_json.get("outputs"))
    s: set[str] = set()
    for x in outs:
        v = str(x or "").strip().lower()
        if v:
            s.add(v)
    if not s:
        s.add(MusicTrackType.full_mix.value)
    return s


def _merge_dict(a: Any, b: Any) -> Any:
    """
    Deep merge dicts with REPLACE semantics for empty dict.
    - If b is {}, returns {} (clears)
    - If b is None, sets None
    - If both dict, merges recursively
    - Otherwise b overwrites
    """
    if b is None:
        return None
    if isinstance(b, dict):
        if b == {}:
            return {}
        if not isinstance(a, dict):
            a = {}
        out = dict(a)
        for k, v in b.items():
            out[k] = _merge_dict(out.get(k), v)
        return out
    return deepcopy(b)


def _coerce_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -----------------------------
# DB helpers: generic
# -----------------------------
async def _get_table_columns(*, pool, schema: str, table: str) -> set[str]:
    key = (schema, table)
    if key in _COL_CACHE:
        return _COL_CACHE[key]
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
        cols = {str(r["column_name"]) for r in (rows or []) if r and r.get("column_name")}
        _COL_CACHE[key] = cols
        return cols
    except Exception:
        return set()


async def _load_input_json(*, job_id: UUID) -> Dict[str, Any]:
    pool = await get_pool()
    row = await pool.fetchrow("select input_json from public.music_video_jobs where id=$1", job_id)
    return _as_dict(row["input_json"]) if row else {}


async def _write_computed(*, job_id: UUID, computed: Dict[str, Any]) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        update public.music_video_jobs
        set input_json = jsonb_set(
            coalesce(input_json,'{}'::jsonb),
            '{computed}',
            $2::jsonb,
            true
        ),
        updated_at=now()
        where id=$1
        """,
        job_id,
        json.dumps(computed or {}),
    )


async def _patch_computed(job_id: UUID, patch: Dict[str, Any]) -> None:
    """
    IMPORTANT: Deep-merge in Python so we don't clobber nested maps like computed.candidates.
    """
    ij = await _load_input_json(job_id=job_id)
    cur = _as_dict(ij.get("computed"))
    merged = _merge_dict(cur, patch or {})
    await _write_computed(job_id=job_id, computed=merged)


async def _set_job_progress_status_best_effort(
    *,
    job_id: UUID,
    progress: int | None = None,
    status: str | None = None,
) -> None:
    pool = await get_pool()
    sets = []
    params: List[Any] = [job_id]
    if progress is not None:
        params.append(int(max(0, min(100, progress))))
        sets.append(f"progress=${len(params)}")
    if status is not None:
        params.append(str(status))
        sets.append(f"status=${len(params)}")
    if not sets:
        return
    sets.append("updated_at=now()")
    try:
        await pool.execute(
            f"update public.music_video_jobs set {', '.join(sets)} where id=$1",
            *params,
        )
    except Exception:
        return


async def _upsert_step(
    job_id: UUID,
    step_code: str,
    status: str,
    meta: Dict[str, Any] | None = None,
    err: str | None = None,
) -> None:
    pool = await get_pool()
    meta = meta or {}
    try:
        await pool.execute(
            """
            insert into public.studio_job_steps(job_id, step_code, status, attempt, meta_json, error_message)
            values($1,$2,$3,0,$4::jsonb,$5)
            on conflict (job_id, step_code) do update
              set status=excluded.status,
                  meta_json=excluded.meta_json,
                  error_message=excluded.error_message,
                  updated_at=now()
            """,
            job_id,
            step_code,
            status,
            json.dumps(meta),
            err,
        )
    except Exception:
        return


async def _enqueue_provider_run(
    *,
    job_id: UUID,
    provider: str,
    idempotency_key: str,
    request_json: Dict[str, Any],
    meta_json: Dict[str, Any],
) -> UUID:
    runs = ProviderRunsRepo()
    return await runs.enqueue(
        job_id=job_id,
        provider=provider,
        idempotency_key=idempotency_key,
        request_json=request_json,
        meta_json=meta_json,
    )


# -----------------------------
# DB helpers: candidates + runs
# -----------------------------
async def _insert_candidate_row(
    *,
    job_id: UUID,
    candidate_id: UUID,
    candidate_type: str,
    provider: str,
    group_id: UUID,
    variant_index: int,
    attempt: int,
) -> None:
    pool = await get_pool()
    cols = await _get_table_columns(pool=pool, schema="public", table="music_candidates")
    if not cols:
        raise ValueError("music_candidates_missing")

    row = await pool.fetchrow(
        """
        select mvj.project_id as project_id, mp.user_id as user_id
        from public.music_video_jobs mvj
        join public.music_projects mp on mp.id = mvj.project_id
        where mvj.id=$1
        """,
        job_id,
    )
    if not row:
        raise ValueError("job_or_project_missing")

    values: Dict[str, Any] = {}
    if "id" in cols:
        values["id"] = candidate_id
    if "job_id" in cols:
        values["job_id"] = job_id
    if "project_id" in cols:
        values["project_id"] = UUID(str(row["project_id"]))
    if "user_id" in cols:
        values["user_id"] = UUID(str(row["user_id"]))
    if "candidate_type" in cols:
        values["candidate_type"] = candidate_type
    if "status" in cols:
        values["status"] = "queued"
    if "provider" in cols:
        values["provider"] = provider
    if "group_id" in cols:
        values["group_id"] = group_id
    if "variant_index" in cols:
        values["variant_index"] = int(variant_index)
    if "attempt" in cols:
        values["attempt"] = int(attempt)
    if "meta_json" in cols:
        values["meta_json"] = {
            "group_id": str(group_id),
            "variant_index": int(variant_index),
            "attempt": int(attempt),
            "provider": provider,
        }

    col_list: List[str] = []
    param_list: List[str] = []
    args: List[Any] = []
    for k, v in values.items():
        col_list.append(k)
        args.append(v)
        param_list.append(f"${len(args)}")

    if not col_list:
        raise ValueError("music_candidates_insert_no_columns")

    sql = f"""
    insert into public.music_candidates({", ".join(col_list)})
    values({", ".join(param_list)})
    on conflict (id) do nothing
    """
    await pool.execute(sql, *args)


async def _list_candidates(
    *,
    job_id: UUID,
    candidate_type: str,
    group_id: UUID,
    attempt: int,
) -> List[Dict[str, Any]]:
    pool = await get_pool()
    cols = await _get_table_columns(pool=pool, schema="public", table="music_candidates")

    order = "order by coalesce(variant_index,0) asc"
    if "created_at" in cols:
        order += ", created_at asc"
    else:
        order += ", id asc"

    rows = await pool.fetch(
        f"""
        select id, status, score_json, content_json, artifact_id, media_asset_id, duration_ms, meta_json
        from public.music_candidates
        where job_id=$1 and candidate_type=$2 and group_id=$3 and attempt=$4
        {order}
        """,
        job_id,
        candidate_type,
        group_id,
        int(attempt),
    )
    return [dict(r) for r in (rows or [])]


async def _mark_candidate_chosen(
    *,
    candidate_id: UUID,
    group_id: UUID,
    attempt: int,
    job_id: UUID,
    candidate_type: str,
) -> None:
    pool = await get_pool()
    try:
        cols = await _get_table_columns(pool=pool, schema="public", table="music_candidates")
        sets = ["status='chosen'", "updated_at=now()"]
        if "chosen_at" in cols:
            sets.insert(1, "chosen_at=now()")
        await pool.execute(
            f"update public.music_candidates set {', '.join(sets)} where id=$1",
            candidate_id,
        )
        await pool.execute(
            """
            update public.music_candidates
            set status='discarded', updated_at=now()
            where job_id=$1 and candidate_type=$2 and group_id=$3 and attempt=$4 and id<>$5
              and status not in ('chosen','discarded')
            """,
            job_id,
            candidate_type,
            group_id,
            int(attempt),
            candidate_id,
        )
    except Exception:
        return


async def _find_run_status_for_group(
    *,
    job_id: UUID,
    group_id: UUID,
    run_type: str,
) -> Tuple[bool, bool, Optional[Dict[str, Any]]]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        select provider_status, response_json
        from public.provider_runs
        where job_id=$1
          and meta_json->>'group_id' = $2
          and meta_json->>'run_type' = $3
        order by created_at asc
        """,
        job_id,
        str(group_id),
        run_type,
    )
    if not rows:
        return (False, False, None)

    sts = [str(r["provider_status"] or "").lower() for r in rows]
    all_terminal = all(s in ("succeeded", "failed", "abandoned") for s in sts)
    any_succeeded = any(s == "succeeded" for s in sts)

    merged: Optional[Dict[str, Any]] = None
    if any_succeeded:
        for r in rows:
            if str(r["provider_status"] or "").lower() == "succeeded":
                merged = _as_dict(r.get("response_json"))
                break
    return (all_terminal, any_succeeded, merged)


async def _promote_chosen_lyrics_into_computed(*, job_id: UUID, candidate_row: Dict[str, Any]) -> None:
    txt = _as_dict(candidate_row.get("content_json")).get("lyrics_text")
    if txt and str(txt).strip():
        await _patch_computed(job_id, {"lyrics_text": str(txt)})


async def _promote_chosen_audio_into_tracks_and_computed(*, job_id: UUID, candidate_row: Dict[str, Any]) -> None:
    pool = await get_pool()
    row = await pool.fetchrow("select project_id from public.music_video_jobs where id=$1", job_id)
    if not row:
        return
    project_id = UUID(str(row["project_id"]))

    def _maybe_uuid(x: Any) -> UUID | None:
        if not x:
            return None
        try:
            return x if isinstance(x, UUID) else UUID(str(x))
        except Exception:
            return None

    artifact_id = _maybe_uuid(candidate_row.get("artifact_id"))
    media_asset_id = _maybe_uuid(candidate_row.get("media_asset_id"))
    dur = int(candidate_row.get("duration_ms") or 0)
    meta = _as_dict(candidate_row.get("meta_json"))

    try:
        tracks = MusicTracksRepo()
        await tracks.upsert_track(
            project_id=project_id,
            track_type=MusicTrackType.full_mix.value,
            duration_ms=max(1, dur),
            artifact_id=artifact_id,
            media_asset_id=media_asset_id,
            meta_json=(meta if isinstance(meta, dict) else None),
        )
    except Exception:
        pass

    audio_meta = _as_dict(meta.get("audio_meta"))
    am = audio_meta.get("audio_master_url") or audio_meta.get("byo_audio_url") or audio_meta.get("demo_audio_url")

    patch: Dict[str, Any] = {
        "audio_master_duration_ms": max(1, dur) if dur else None,
        "audio_master_artifact_id": str(artifact_id) if artifact_id else None,
        "audio_master_media_asset_id": str(media_asset_id) if media_asset_id else None,
    }
    if am:
        patch["audio_master_url"] = am
        patch["byo_audio_url"] = am

    await _patch_computed(job_id, patch)


async def _promote_chosen_video_into_job(*, job_id: UUID, candidate_row: Dict[str, Any]) -> None:
    meta = _as_dict(candidate_row.get("meta_json"))
    chosen = candidate_row.get("media_asset_id")

    preview = meta.get("preview_video_asset_id") or chosen
    final = meta.get("final_video_asset_id") or chosen

    pool = await get_pool()
    try:
        await pool.execute(
            """
            update public.music_video_jobs
            set preview_video_asset_id=$2,
                final_video_asset_id=$3,
                updated_at=now()
            where id=$1
            """,
            job_id,
            UUID(str(preview)) if preview else None,
            UUID(str(final)) if final else None,
        )
    except Exception:
        return


# -----------------------------
# Controller
# -----------------------------
class MusicGraphController:
    """
    LangGraph-driven persistent controller.

    Source of truth:
      public.music_video_jobs.input_json.computed.graph

    Fan-out: create provider_runs rows (durable)
    Fan-in: wait by checking music_candidates / provider_runs completion
    HITL: set computed.required_action and stop
    """

    def __init__(self) -> None:
        self._app = self._build().compile()

    def _build(self) -> StateGraph:
        g: StateGraph = StateGraph(dict)

        async def paused(state: Dict[str, Any]) -> Dict[str, Any]:
            state["stop_reason"] = "action_required"
            return state

        async def route(state: Dict[str, Any]) -> Dict[str, Any]:
            computed = _as_dict(state.get("computed"))

            if computed.get("required_action"):
                state["route"] = "paused"
                return state

            stage = str(state.get("stage") or "intent").strip()
            if stage not in _STAGE_SET:
                stage = "intent"
            state["route"] = stage
            return state

        # ---- nodes ----

        async def intent(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, MusicJobStage.intent.value, "running", {"stage": "intent"})
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("intent"), status="running")
            state["stage"] = "plan"
            await _upsert_step(job_id, MusicJobStage.intent.value, "succeeded")
            return state

        async def plan(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            mode = str(state.get("mode") or "").lower()
            language_hint = str(state.get("language_hint") or "").strip() or None

            await _upsert_step(job_id, MusicJobStage.creative_brief.value, "running", {"stage": "plan"})
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("plan"), status="running")

            ij = await _load_input_json(job_id=job_id)
            computed = _as_dict(ij.get("computed"))

            patch: Dict[str, Any] = {}
            if language_hint and not computed.get("language_hint"):
                patch["language_hint"] = language_hint

            if not computed.get("music_plan"):
                patch["music_plan"] = {
                    "version": 1,
                    "source": "controller_fallback",
                    "mode": mode,
                    "summary": "Auto plan (fallback)",
                }

            if patch:
                await _patch_computed(job_id, patch)

            state["stage"] = "ingest_audio" if _is_byo_mode(mode) else "lyrics_fanout"
            await _upsert_step(job_id, MusicJobStage.creative_brief.value, "succeeded")
            return state

        async def ingest_audio(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, "ingest_audio", "running")
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("ingest_audio"), status="running")

            ij = await _load_input_json(job_id=job_id)
            computed = _as_dict(ij.get("computed"))
            hints = _as_dict(ij.get("provider_hints"))

            audio_url = (
                computed.get("audio_master_url")
                or computed.get("byo_audio_url")
                or ij.get("uploaded_audio_url")
                or hints.get("uploaded_audio_url")
                or hints.get("audio_master_url")
                or hints.get("byo_audio_url")
            )
            duration_ms_raw = (
                computed.get("audio_master_duration_ms")
                or ij.get("uploaded_audio_duration_ms")
                or hints.get("audio_master_duration_ms")
            )

            if not audio_url:
                ra = {"type": "upload_audio", "message": "BYO mode requires an uploaded song audio URL"}
                await _patch_computed(job_id, {"required_action": ra})
                state["stop_reason"] = "action_required"
                await _upsert_step(job_id, "ingest_audio", "action_required", {"required_action": ra})
                return state

            patch: Dict[str, Any] = {"byo_audio_url": audio_url, "audio_master_url": audio_url}
            d = _coerce_int(duration_ms_raw, 0)
            if d > 0:
                patch["audio_master_duration_ms"] = d
            await _patch_computed(job_id, patch)

            await _upsert_step(job_id, "ingest_audio", "succeeded")
            state["stage"] = "byo_analysis_fanout"
            return state

        async def byo_analysis_fanout(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, "byo_analysis", "waiting_parallel", {"fanout": ["bpm_detect", "segment_detect"]})
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("byo_analysis_fanout"), status="running")

            ij = await _load_input_json(job_id=job_id)
            computed = _as_dict(ij.get("computed"))

            analysis = _as_dict(computed.get("byo_analysis"))
            gid_str = analysis.get("group_id")
            if gid_str:
                state["stage"] = "byo_analysis_fanin"
                return state

            gid = uuid4()
            audio_url = computed.get("audio_master_url") or computed.get("byo_audio_url")
            if not audio_url:
                ra = {"type": "upload_audio", "message": "BYO analysis requires audio_master_url/byo_audio_url"}
                await _patch_computed(job_id, {"required_action": ra})
                await _upsert_step(job_id, "byo_analysis", "action_required", {"required_action": ra})
                state["stop_reason"] = "action_required"
                return state

            dur = _coerce_int(computed.get("audio_master_duration_ms"), 0)
            duration_ms = dur if dur > 0 else None  # send null if unknown (worker can probe)

            for rt in ("byo_bpm_detect", "byo_segment_detect"):
                idem = _idem(job_id=job_id, run_type=rt, key=str(gid))
                await _enqueue_provider_run(
                    job_id=job_id,
                    provider="native",
                    idempotency_key=idem,
                    request_json={"audio_url": audio_url, "duration_ms": duration_ms},
                    meta_json={"svc": "svc-music", "run_type": rt, "group_id": str(gid)},
                )

            await _patch_computed(job_id, {"byo_analysis": {"group_id": str(gid)}})
            state["stage"] = "byo_analysis_fanin"
            return state

        async def byo_analysis_fanin(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, "byo_analysis", "running", {"fanin": True})
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("byo_analysis_fanin"), status="running")

            ij = await _load_input_json(job_id=job_id)
            computed = _as_dict(ij.get("computed"))
            gid_str = _as_dict(computed.get("byo_analysis")).get("group_id")
            if not gid_str:
                state["stage"] = "byo_analysis_fanout"
                return state
            gid = UUID(str(gid_str))

            all_bpm, ok_bpm, bpm_resp = await _find_run_status_for_group(job_id=job_id, group_id=gid, run_type="byo_bpm_detect")
            all_seg, ok_seg, seg_resp = await _find_run_status_for_group(job_id=job_id, group_id=gid, run_type="byo_segment_detect")

            if not (all_bpm and all_seg):
                await _upsert_step(job_id, "byo_analysis", "waiting_parallel", {"group_id": str(gid)})
                state["stop_reason"] = "waiting_parallel"
                return state

            patch: Dict[str, Any] = {}
            if ok_bpm and bpm_resp:
                patch["bpm"] = bpm_resp.get("bpm")
                patch["beat_grid"] = bpm_resp.get("beat_grid")
            if ok_seg and seg_resp:
                patch["segments"] = seg_resp.get("segments")
                patch["segment_plan"] = seg_resp.get("segment_plan")

            if "segment_plan" not in patch:
                dur = _coerce_int(computed.get("audio_master_duration_ms"), 0)
                end = min(dur, 30_000) if dur > 0 else 30_000
                patch["segment_plan"] = {"kind": "fallback_first_30s", "start_ms": 0, "end_ms": end}

            await _patch_computed(job_id, patch)
            await _upsert_step(job_id, "byo_analysis", "succeeded", {"group_id": str(gid)})

            state["stage"] = "align_lyrics"
            return state

        async def lyrics_fanout(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, MusicJobStage.lyrics.value, "waiting_parallel", {"fanout": 3})
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("lyrics_fanout"), status="running")

            ij = await _load_input_json(job_id=job_id)
            computed = _as_dict(ij.get("computed"))
            info = _as_dict(_as_dict(computed.get("candidates")).get("lyrics"))
            if info.get("group_id"):
                state["stage"] = "lyrics_fanin"
                return state

            gid = uuid4()
            attempt = _coerce_int(_as_dict(computed.get("attempts")).get("lyrics", 1), 1)
            providers = _as_list(computed.get("lyrics_providers")) or ["native"]

            for i in range(3):
                cid = uuid4()
                provider = str(providers[i % len(providers)]).strip() or "native"
                await _insert_candidate_row(
                    job_id=job_id,
                    candidate_id=cid,
                    candidate_type="lyrics",
                    provider=provider,
                    group_id=gid,
                    variant_index=i,
                    attempt=attempt,
                )
                idem = _idem(job_id=job_id, run_type="lyrics_candidate", key=f"{cid}:{attempt}")
                await _enqueue_provider_run(
                    job_id=job_id,
                    provider=provider,
                    idempotency_key=idem,
                    request_json={"candidate_type": "lyrics", "group_id": str(gid), "variant_index": i, "attempt": attempt},
                    meta_json={
                        "svc": "svc-music",
                        "run_type": "lyrics_candidate",
                        "candidate_id": str(cid),
                        "group_id": str(gid),
                        "variant_index": i,
                        "attempt": attempt,
                    },
                )

            await _patch_computed(job_id, {"candidates": {"lyrics": {"group_id": str(gid), "attempt": attempt}}})
            state["stage"] = "lyrics_fanin"
            return state

        async def lyrics_fanin(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, MusicJobStage.lyrics.value, "running", {"fanin": True})
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("lyrics_fanin"), status="running")

            ij = await _load_input_json(job_id=job_id)
            computed = _as_dict(ij.get("computed"))
            info = _as_dict(_as_dict(_as_dict(computed.get("candidates")).get("lyrics")))
            gid_str = info.get("group_id")
            attempt = _coerce_int(info.get("attempt") or 1, 1)
            if not gid_str:
                state["stage"] = "lyrics_fanout"
                return state
            gid = UUID(str(gid_str))

            rows = await _list_candidates(job_id=job_id, candidate_type="lyrics", group_id=gid, attempt=attempt)
            statuses = [str(r.get("status") or "") for r in rows]
            if not rows or not all(_terminal_candidate_status(s) for s in statuses):
                await _upsert_step(job_id, MusicJobStage.lyrics.value, "waiting_parallel", {"group_id": str(gid), "attempt": attempt})
                state["stop_reason"] = "waiting_parallel"
                return state

            succeeded = [r for r in rows if str(r.get("status") or "").lower() == "succeeded"]
            if not succeeded:
                await _patch_computed(job_id, {"attempts": {"lyrics": attempt + 1}, "candidates": {"lyrics": {}}})
                state["stage"] = "lyrics_fanout"
                return state

            hitl = bool(state.get("hitl", False))
            if hitl:
                ra = {"type": "select_lyrics", "group_id": str(gid), "candidate_type": "lyrics", "min_select": 1, "max_select": 1}
                await _patch_computed(job_id, {"required_action": ra})
                await _upsert_step(job_id, MusicJobStage.lyrics.value, "action_required", {"group_id": str(gid), "attempt": attempt})
                state["stop_reason"] = "action_required"
                return state

            best = sorted(succeeded, key=lambda r: _safe_overall(r.get("score_json")), reverse=True)[0]
            await _mark_candidate_chosen(
                candidate_id=UUID(str(best["id"])),
                group_id=gid,
                attempt=attempt,
                job_id=job_id,
                candidate_type="lyrics",
            )
            await _promote_chosen_lyrics_into_computed(job_id=job_id, candidate_row=best)

            await _patch_computed(job_id, {"chosen_lyrics_candidate_id": str(best["id"]), "required_action": None})
            await _upsert_step(job_id, MusicJobStage.lyrics.value, "succeeded", {"chosen": str(best["id"])})
            state["stage"] = "arrangement"
            return state

        async def arrangement(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, MusicJobStage.arrangement.value, "running")
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("arrangement"), status="running")
            await _upsert_step(job_id, MusicJobStage.arrangement.value, "succeeded")
            state["stage"] = "provider_route"
            return state

        async def provider_route(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, MusicJobStage.provider_route.value, "running")
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("provider_route"), status="running")

            ij = await _load_input_json(job_id=job_id)
            computed = _as_dict(ij.get("computed"))
            patch: Dict[str, Any] = {}
            if not computed.get("audio_providers"):
                patch["audio_providers"] = ["native"]
            if not computed.get("video_providers"):
                patch["video_providers"] = ["native"]
            if patch:
                await _patch_computed(job_id, patch)

            await _upsert_step(job_id, MusicJobStage.provider_route.value, "succeeded")
            state["stage"] = "audio_fanout"
            return state

        async def audio_fanout(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, MusicJobStage.generate_audio.value, "waiting_parallel", {"fanout": 2})
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("audio_fanout"), status="running")

            ij = await _load_input_json(job_id=job_id)
            computed = _as_dict(ij.get("computed"))
            info = _as_dict(_as_dict(computed.get("candidates")).get("audio"))
            if info.get("group_id"):
                state["stage"] = "audio_fanin"
                return state

            gid = uuid4()
            attempt = _coerce_int(_as_dict(computed.get("attempts")).get("audio", 1), 1)
            providers = _as_list(computed.get("audio_providers")) or ["native"]
            n = _coerce_int(computed.get("audio_candidates_n") or 2, 2)
            n = max(1, min(3, n))

            for i in range(n):
                cid = uuid4()
                provider = str(providers[i % len(providers)]).strip() or "native"
                await _insert_candidate_row(
                    job_id=job_id,
                    candidate_id=cid,
                    candidate_type="audio",
                    provider=provider,
                    group_id=gid,
                    variant_index=i,
                    attempt=attempt,
                )
                idem = _idem(job_id=job_id, run_type="audio_candidate", key=f"{cid}:{attempt}")
                await _enqueue_provider_run(
                    job_id=job_id,
                    provider=provider,
                    idempotency_key=idem,
                    request_json={"candidate_type": "audio", "group_id": str(gid), "variant_index": i, "attempt": attempt},
                    meta_json={
                        "svc": "svc-music",
                        "run_type": "audio_candidate",
                        "candidate_id": str(cid),
                        "group_id": str(gid),
                        "variant_index": i,
                        "attempt": attempt,
                    },
                )

            await _patch_computed(job_id, {"candidates": {"audio": {"group_id": str(gid), "attempt": attempt}}})
            state["stage"] = "audio_fanin"
            return state

        async def audio_fanin(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, MusicJobStage.generate_audio.value, "running", {"fanin": True})
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("audio_fanin"), status="running")

            ij = await _load_input_json(job_id=job_id)
            computed = _as_dict(ij.get("computed"))
            info = _as_dict(_as_dict(_as_dict(computed.get("candidates")).get("audio")))
            gid_str = info.get("group_id")
            attempt = _coerce_int(info.get("attempt") or 1, 1)
            if not gid_str:
                state["stage"] = "audio_fanout"
                return state
            gid = UUID(str(gid_str))

            rows = await _list_candidates(job_id=job_id, candidate_type="audio", group_id=gid, attempt=attempt)
            statuses = [str(r.get("status") or "") for r in rows]
            if not rows or not all(_terminal_candidate_status(s) for s in statuses):
                await _upsert_step(job_id, MusicJobStage.generate_audio.value, "waiting_parallel", {"group_id": str(gid), "attempt": attempt})
                state["stop_reason"] = "waiting_parallel"
                return state

            succeeded = [r for r in rows if str(r.get("status") or "").lower() == "succeeded"]
            if not succeeded:
                await _patch_computed(job_id, {"attempts": {"audio": attempt + 1}, "candidates": {"audio": {}}})
                state["stage"] = "audio_fanout"
                return state

            hitl = bool(state.get("hitl", False))
            if hitl:
                ra = {"type": "select_audio", "group_id": str(gid), "candidate_type": "audio", "min_select": 1, "max_select": 1}
                await _patch_computed(job_id, {"required_action": ra})
                await _upsert_step(job_id, MusicJobStage.generate_audio.value, "action_required", {"group_id": str(gid), "attempt": attempt})
                state["stop_reason"] = "action_required"
                return state

            best = sorted(succeeded, key=lambda r: _safe_overall(r.get("score_json")), reverse=True)[0]
            await _mark_candidate_chosen(
                candidate_id=UUID(str(best["id"])),
                group_id=gid,
                attempt=attempt,
                job_id=job_id,
                candidate_type="audio",
            )
            await _promote_chosen_audio_into_tracks_and_computed(job_id=job_id, candidate_row=best)

            await _patch_computed(job_id, {"chosen_audio_candidate_id": str(best["id"]), "required_action": None})
            await _upsert_step(job_id, MusicJobStage.generate_audio.value, "succeeded", {"chosen": str(best["id"])})
            state["stage"] = "align_lyrics"
            return state

        async def align_lyrics(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, MusicJobStage.align_lyrics.value, "running")
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("align_lyrics"), status="running")

            ij = await _load_input_json(job_id=job_id)
            computed = _as_dict(ij.get("computed"))

            outs = _outputs_set(ij)
            wants_timed = MusicTrackType.timed_lyrics_json.value in outs
            if not wants_timed:
                await _upsert_step(job_id, MusicJobStage.align_lyrics.value, "skipped", {"reason": "timed_lyrics_not_requested"})
                state["stage"] = "video_fanout"
                return state

            if computed.get("timed_lyrics_json"):
                await _upsert_step(job_id, MusicJobStage.align_lyrics.value, "succeeded", {"source": "computed"})
                state["stage"] = "video_fanout"
                return state

            lyrics_text = str(computed.get("lyrics_text") or "").strip()
            if not lyrics_text:
                ra = {"type": "provide_lyrics", "message": "Timed lyrics requested but lyrics_text is empty"}
                await _patch_computed(job_id, {"required_action": ra})
                await _upsert_step(job_id, MusicJobStage.align_lyrics.value, "action_required", {"required_action": ra})
                state["stop_reason"] = "action_required"
                return state

            align_info = _as_dict(computed.get("align_lyrics"))
            run_gid_str = align_info.get("group_id")

            audio_url = computed.get("audio_master_url") or computed.get("byo_audio_url")
            if not audio_url:
                ra = {"type": "upload_audio", "message": "Timed lyrics alignment requires audio_master_url/byo_audio_url"}
                await _patch_computed(job_id, {"required_action": ra})
                await _upsert_step(job_id, MusicJobStage.align_lyrics.value, "action_required", {"required_action": ra})
                state["stop_reason"] = "action_required"
                return state

            dur = _coerce_int(computed.get("audio_master_duration_ms"), 0)
            duration_ms = dur if dur > 0 else None  # worker can probe

            language = (
                str(computed.get("language_hint") or "").strip()
                or str(state.get("language_hint") or "").strip()
                or "en-IN"
            )

            if not run_gid_str:
                gid = uuid4()
                idem = _idem(job_id=job_id, run_type="align_lyrics", key=str(gid))
                await _enqueue_provider_run(
                    job_id=job_id,
                    provider="native",
                    idempotency_key=idem,
                    request_json={
                        "audio_url": audio_url,
                        "duration_ms": duration_ms,
                        "lyrics_text": lyrics_text,
                        "language": language,
                    },
                    meta_json={"svc": "svc-music", "run_type": "align_lyrics", "group_id": str(gid)},
                )
                await _patch_computed(job_id, {"align_lyrics": {"group_id": str(gid)}})
                await _upsert_step(job_id, MusicJobStage.align_lyrics.value, "waiting_parallel", {"group_id": str(gid)})
                state["stop_reason"] = "waiting_parallel"
                return state

            gid = UUID(str(run_gid_str))
            all_term, ok, resp = await _find_run_status_for_group(job_id=job_id, group_id=gid, run_type="align_lyrics")
            if not all_term:
                await _upsert_step(job_id, MusicJobStage.align_lyrics.value, "waiting_parallel", {"group_id": str(gid)})
                state["stop_reason"] = "waiting_parallel"
                return state

            if ok and resp and resp.get("timed_lyrics_json") is not None:
                await _patch_computed(job_id, {"timed_lyrics_json": resp.get("timed_lyrics_json")})
                await _upsert_step(job_id, MusicJobStage.align_lyrics.value, "succeeded", {"source": "provider_run"})
            else:
                await _upsert_step(job_id, MusicJobStage.align_lyrics.value, "failed", {"reason": "align_failed"}, err="align_failed")

            state["stage"] = "video_fanout"
            return state

        async def video_fanout(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, MusicJobStage.generate_performer_videos.value, "waiting_parallel", {"fanout": 3})
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("video_fanout"), status="running")

            ij = await _load_input_json(job_id=job_id)
            computed = _as_dict(ij.get("computed"))
            info = _as_dict(_as_dict(computed.get("candidates")).get("video"))
            if info.get("group_id"):
                state["stage"] = "video_fanin"
                return state

            gid = uuid4()
            attempt = _coerce_int(_as_dict(computed.get("attempts")).get("video", 1), 1)
            providers = _as_list(computed.get("video_providers")) or ["native"]
            n = _coerce_int(computed.get("video_candidates_n") or 3, 3)
            n = max(1, min(4, n))

            for i in range(n):
                cid = uuid4()
                provider = str(providers[i % len(providers)]).strip() or "native"
                await _insert_candidate_row(
                    job_id=job_id,
                    candidate_id=cid,
                    candidate_type="video",
                    provider=provider,
                    group_id=gid,
                    variant_index=i,
                    attempt=attempt,
                )
                idem = _idem(job_id=job_id, run_type="video_candidate", key=f"{cid}:{attempt}")
                await _enqueue_provider_run(
                    job_id=job_id,
                    provider=provider,
                    idempotency_key=idem,
                    request_json={"candidate_type": "video", "group_id": str(gid), "variant_index": i, "attempt": attempt},
                    meta_json={
                        "svc": "svc-music",
                        "run_type": "video_candidate",
                        "candidate_id": str(cid),
                        "group_id": str(gid),
                        "variant_index": i,
                        "attempt": attempt,
                    },
                )

            await _patch_computed(job_id, {"candidates": {"video": {"group_id": str(gid), "attempt": attempt}}})
            state["stage"] = "video_fanin"
            return state

        async def video_fanin(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, MusicJobStage.generate_performer_videos.value, "running", {"fanin": True})
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("video_fanin"), status="running")

            ij = await _load_input_json(job_id=job_id)
            computed = _as_dict(ij.get("computed"))
            info = _as_dict(_as_dict(_as_dict(computed.get("candidates")).get("video")))
            gid_str = info.get("group_id")
            attempt = _coerce_int(info.get("attempt") or 1, 1)
            if not gid_str:
                state["stage"] = "video_fanout"
                return state
            gid = UUID(str(gid_str))

            rows = await _list_candidates(job_id=job_id, candidate_type="video", group_id=gid, attempt=attempt)
            statuses = [str(r.get("status") or "") for r in rows]
            if not rows or not all(_terminal_candidate_status(s) for s in statuses):
                await _upsert_step(job_id, MusicJobStage.generate_performer_videos.value, "waiting_parallel", {"group_id": str(gid), "attempt": attempt})
                state["stop_reason"] = "waiting_parallel"
                return state

            succeeded = [r for r in rows if str(r.get("status") or "").lower() == "succeeded"]
            if not succeeded:
                await _patch_computed(job_id, {"attempts": {"video": attempt + 1}, "candidates": {"video": {}}})
                state["stage"] = "video_fanout"
                return state

            hitl = bool(state.get("hitl", False))
            if hitl and bool(computed.get("hitl_video_selection", False)):
                ra = {"type": "select_video", "group_id": str(gid), "candidate_type": "video", "min_select": 1, "max_select": 1}
                await _patch_computed(job_id, {"required_action": ra})
                await _upsert_step(job_id, MusicJobStage.generate_performer_videos.value, "action_required", {"group_id": str(gid), "attempt": attempt})
                state["stop_reason"] = "action_required"
                return state

            best = sorted(succeeded, key=lambda r: _safe_overall(r.get("score_json")), reverse=True)[0]
            await _mark_candidate_chosen(
                candidate_id=UUID(str(best["id"])),
                group_id=gid,
                attempt=attempt,
                job_id=job_id,
                candidate_type="video",
            )
            await _promote_chosen_video_into_job(job_id=job_id, candidate_row=best)

            await _patch_computed(job_id, {"chosen_video_candidate_id": str(best["id"]), "required_action": None})
            await _upsert_step(job_id, MusicJobStage.generate_performer_videos.value, "succeeded", {"chosen": str(best["id"])})
            state["stage"] = "compose_video"
            return state

        async def compose_video(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, MusicJobStage.compose_video.value, "running")
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("compose_video"), status="running")
            await _upsert_step(job_id, MusicJobStage.compose_video.value, "succeeded")
            state["stage"] = "qc_video"
            return state

        async def qc_video(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, MusicJobStage.qc.value, "running")
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("qc_video"), status="running")
            await _upsert_step(job_id, MusicJobStage.qc.value, "succeeded", {"qc": "pass"})
            state["stage"] = "publish_ready"
            return state

        async def publish_ready(state: Dict[str, Any]) -> Dict[str, Any]:
            job_id = UUID(state["job_id"])
            await _upsert_step(job_id, MusicJobStage.publish.value, "running")
            await _set_job_progress_status_best_effort(job_id=job_id, progress=_progress_for("publish_ready"), status="succeeded")
            await _upsert_step(job_id, MusicJobStage.publish.value, "succeeded")
            state["stage"] = "publish_ready"
            state["stop_reason"] = "done"
            return state

        # ---- graph wiring ----
        g.add_node("paused", paused)
        g.add_node("route", route)

        g.add_node("intent", intent)
        g.add_node("plan", plan)
        g.add_node("ingest_audio", ingest_audio)
        g.add_node("byo_analysis_fanout", byo_analysis_fanout)
        g.add_node("byo_analysis_fanin", byo_analysis_fanin)

        g.add_node("lyrics_fanout", lyrics_fanout)
        g.add_node("lyrics_fanin", lyrics_fanin)
        g.add_node("arrangement", arrangement)
        g.add_node("provider_route", provider_route)
        g.add_node("audio_fanout", audio_fanout)
        g.add_node("audio_fanin", audio_fanin)
        g.add_node("align_lyrics", align_lyrics)
        g.add_node("video_fanout", video_fanout)
        g.add_node("video_fanin", video_fanin)
        g.add_node("compose_video", compose_video)
        g.add_node("qc_video", qc_video)
        g.add_node("publish_ready", publish_ready)

        g.set_entry_point("route")
        g.add_conditional_edges(
            "route",
            lambda s: s.get("route"),
            {
                "paused": "paused",
                "intent": "intent",
                "plan": "plan",
                "ingest_audio": "ingest_audio",
                "byo_analysis_fanout": "byo_analysis_fanout",
                "byo_analysis_fanin": "byo_analysis_fanin",
                "lyrics_fanout": "lyrics_fanout",
                "lyrics_fanin": "lyrics_fanin",
                "arrangement": "arrangement",
                "provider_route": "provider_route",
                "audio_fanout": "audio_fanout",
                "audio_fanin": "audio_fanin",
                "align_lyrics": "align_lyrics",
                "video_fanout": "video_fanout",
                "video_fanin": "video_fanin",
                "compose_video": "compose_video",
                "qc_video": "qc_video",
                "publish_ready": "publish_ready",
            },
        )

        for n in (
            "paused",
            "intent",
            "plan",
            "ingest_audio",
            "byo_analysis_fanout",
            "byo_analysis_fanin",
            "lyrics_fanout",
            "lyrics_fanin",
            "arrangement",
            "provider_route",
            "audio_fanout",
            "audio_fanin",
            "align_lyrics",
            "video_fanout",
            "video_fanin",
            "compose_video",
            "qc_video",
            "publish_ready",
        ):
            g.add_edge(n, END)

        return g

    async def tick(self, *, job_id: UUID, trigger: str = "system") -> Dict[str, Any]:
        pool = await get_pool()
        row = await pool.fetchrow(
            """
            select mvj.input_json, mp.mode, mp.language_hint
            from public.music_video_jobs mvj
            join public.music_projects mp on mp.id = mvj.project_id
            where mvj.id=$1
            """,
            job_id,
        )
        if not row:
            raise ValueError("job_not_found")

        ij = _as_dict(row["input_json"])
        computed = _as_dict(ij.get("computed"))
        graph = _as_dict(computed.get("graph"))

        mode = str(row.get("mode") or MusicProjectMode.autopilot.value).strip().lower()
        hitl = _is_hitl_mode(mode)

        stage = str(graph.get("stage") or "intent").strip()
        if stage not in _STAGE_SET:
            stage = "intent"

        language_hint = str(row.get("language_hint") or "").strip() or None

        state = {
            "job_id": str(job_id),
            "stage": stage,
            "mode": mode,
            "hitl": hitl,
            "language_hint": language_hint,
            "computed": computed,
            "trigger": trigger,
        }

        out = await self._app.ainvoke(state)

        stage_out = str(out.get("stage", stage) or stage).strip()
        if stage_out not in _STAGE_SET:
            stage_out = stage

        graph_patch: Dict[str, Any] = {
            "stage": stage_out,
            "stop_reason": out.get("stop_reason") or None,
            "last_tick_at": _now_iso(),
            "last_trigger": trigger,
            "last_stage_in": stage,
            "last_stage_out": stage_out,
        }
        await _patch_computed(job_id, {"graph": graph_patch})

        return out