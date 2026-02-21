from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Set
from uuid import UUID

from app.db import get_pool
from app.services.music_orchestrator_common import _stable_json

JsonDict = Dict[str, Any]


async def _table_exists(*, pool, regclass_text: str) -> bool:
    try:
        v = await pool.fetchval("select to_regclass($1)", str(regclass_text))
        return v is not None
    except Exception:
        return False


async def _get_table_columns(*, pool, schema: str, table: str) -> Set[str]:
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


def _studio_request_hash(*, user_id: UUID, studio_type: str, job_id: UUID, payload_json: JsonDict) -> str:
    base = {
        "user_id": str(user_id),
        "studio_type": str(studio_type),
        "job_id": str(job_id),
        "payload": payload_json or {},
    }
    return hashlib.sha256(_stable_json(base).encode("utf-8")).hexdigest()


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


async def ensure_studio_job_envelope(
    *,
    pool,
    job_id: UUID,
    user_id: UUID,
    project_id: UUID | None,
    status: str,
    input_json: JsonDict | None = None,
    meta_json: JsonDict | None = None,
) -> None:
    """
    Guarantees a studio_jobs row exists for the same UUID as music_video_jobs.
    Forces studio_type='music' so legacy code paths can find the job.
    """
    if not await _table_exists(pool=pool, regclass_text="public.studio_jobs"):
        return

    cols = await _get_table_columns(pool=pool, schema="public", table="studio_jobs")
    required = {"id", "studio_type", "status", "request_hash", "payload_json", "meta_json", "user_id"}
    if not required.issubset(cols):
        return

    try:
        r = await pool.fetchrow("select id, studio_type from public.studio_jobs where id=$1 limit 1", job_id)
        if r:
            st = str(r.get("studio_type") or "").strip().lower()
            if st != "music" and "studio_type" in cols:
                try:
                    await pool.execute(
                        "update public.studio_jobs set studio_type='music', updated_at=now() where id=$1",
                        job_id,
                    )
                except Exception:
                    pass
            return
    except Exception:
        pass

    payload = input_json if isinstance(input_json, dict) else {}
    meta: JsonDict = dict(meta_json or {})
    meta.setdefault("source", "svc-music")
    meta.setdefault("request_type", "music_video")
    if project_id:
        meta.setdefault("music_project_id", str(project_id))

    stype = "music"
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


async def update_studio_job_status_best_effort(
    *,
    pool,
    job_id: UUID,
    status: str,
    error_message: str | None = None,
    meta_patch: JsonDict | None = None,
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
            sets.append(f"{col}=${len(params) + 1}")  # id is $1

        if "studio_type" in cols:
            sets.append("studio_type='music'")

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


async def persist_studio_payload_best_effort(*, job_id: UUID, payload_json: JsonDict) -> None:
    """
    Merge into studio_jobs.payload_json (do NOT overwrite).
    """
    pool = await get_pool()
    if not await _table_exists(pool=pool, regclass_text="public.studio_jobs"):
        return

    cols = await _get_table_columns(pool=pool, schema="public", table="studio_jobs")
    if "payload_json" not in cols:
        return

    patch = payload_json if isinstance(payload_json, dict) else {}
    try:
        await pool.execute(
            """
            update public.studio_jobs
            set payload_json =
                (
                  case
                    when payload_json is null then '{}'::jsonb
                    when jsonb_typeof(payload_json) = 'object' then payload_json
                    when jsonb_typeof(payload_json) = 'string'
                     and left(payload_json #>> '{}', 1) in ('{','[')
                    then (payload_json #>> '{}')::jsonb
                    else '{}'::jsonb
                  end
                ) || $2::jsonb,
                updated_at=now()
            where id=$1
            """,
            job_id,
            patch,
        )
    except Exception:
        return


async def persist_fusion_payload_best_effort(*, job_id: UUID, fusion_payload: JsonDict) -> None:
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
            json.dumps(fusion_payload, ensure_ascii=False),
        )
    except Exception:
        return