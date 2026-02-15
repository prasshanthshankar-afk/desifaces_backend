from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID, uuid4

from app.db import get_pool


@dataclass
class CandidateRow:
    id: UUID
    job_id: UUID
    project_id: UUID
    user_id: UUID
    candidate_type: str
    group_id: UUID
    variant_index: int
    attempt: int
    status: str
    provider: Optional[str]
    seed: Optional[int]
    content_json: Optional[Dict[str, Any]]
    score_json: Optional[Dict[str, Any]]
    artifact_id: Optional[UUID]
    media_asset_id: Optional[UUID]
    duration_ms: Optional[int]
    meta_json: Dict[str, Any]
    provider_run_id: Optional[UUID]


class MusicCandidatesRepo:
    _T = "public.music_candidates"
    _cols_cache: Optional[set[str]] = None

    async def _get_cols(self, *, conn=None) -> set[str]:
        """
        Cache columns to safely support optional columns like chosen_at across DB versions.
        """
        if self._cols_cache is not None:
            return self._cols_cache

        pool = await get_pool()
        if conn is None:
            async with pool.acquire() as c:
                rows = await c.fetch(
                    """
                    select column_name
                    from information_schema.columns
                    where table_schema='public' and table_name='music_candidates'
                    """
                )
        else:
            rows = await conn.fetch(
                """
                select column_name
                from information_schema.columns
                where table_schema='public' and table_name='music_candidates'
                """
            )

        self._cols_cache = {str(r["column_name"]) for r in (rows or []) if r and r.get("column_name")}
        return self._cols_cache

    async def create_group(
        self,
        *,
        job_id: UUID,
        project_id: UUID,
        user_id: UUID,
        candidate_type: str,
        count: int,
        attempt: int,
        provider: Optional[str] = None,
        seeds: Optional[Sequence[int]] = None,
        meta_patch: Optional[Dict[str, Any]] = None,
        group_id: Optional[UUID] = None,
    ) -> UUID:
        pool = await get_pool()
        gid = group_id or uuid4()
        count = max(1, int(count or 1))
        seeds = list(seeds or [])
        meta_patch = meta_patch or {}

        rows: List[tuple[UUID, UUID, int, Optional[int]]] = []
        for i in range(count):
            cid = uuid4()
            seed = seeds[i] if i < len(seeds) else None
            rows.append((cid, gid, i, seed))

        async with pool.acquire() as conn:
            async with conn.transaction():
                for (cid, gid0, idx, seed) in rows:
                    meta = {
                        "group_id": str(gid0),
                        "variant_index": int(idx),
                        "attempt": int(attempt),
                        "candidate_type": candidate_type,
                        "provider": provider,
                    }
                    meta.update(meta_patch)

                    await conn.execute(
                        f"""
                        insert into {self._T}(
                            id, job_id, project_id, user_id,
                            candidate_type, group_id, variant_index, attempt,
                            status, provider, seed,
                            meta_json
                        )
                        values($1,$2,$3,$4,$5,$6,$7,$8,'queued',$9,$10,$11::jsonb)
                        on conflict do nothing
                        """,
                        cid,
                        job_id,
                        project_id,
                        user_id,
                        candidate_type,
                        gid0,
                        int(idx),
                        int(attempt),
                        provider,
                        seed,
                        json.dumps(meta),
                    )

        return gid

    async def list(
        self,
        *,
        job_id: UUID,
        candidate_type: Optional[str] = None,
        group_id: Optional[UUID] = None,
        attempt: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        pool = await get_pool()
        where = ["job_id=$1"]
        params: List[Any] = [job_id]

        if candidate_type:
            params.append(candidate_type)
            where.append(f"candidate_type=${len(params)}")
        if group_id:
            params.append(group_id)
            where.append(f"group_id=${len(params)}")
        if attempt is not None:
            params.append(int(attempt))
            where.append(f"attempt=${len(params)}")

        q = f"""
        select
          id, job_id, project_id, user_id,
          candidate_type, group_id, variant_index, attempt,
          status, provider, seed,
          content_json, score_json,
          artifact_id, media_asset_id, duration_ms,
          meta_json, provider_run_id
        from {self._T}
        where {" and ".join(where)}
        order by group_id desc, attempt desc, variant_index asc
        """
        rows = await pool.fetch(q, *params)
        return [dict(r) for r in (rows or [])]

    async def update_candidate(
        self,
        *,
        candidate_id: UUID,
        status: Optional[str] = None,
        provider: Optional[str] = None,
        provider_run_id: Optional[UUID] = None,
        content_json: Optional[Dict[str, Any]] = None,
        score_json: Optional[Dict[str, Any]] = None,
        artifact_id: Optional[UUID] = None,
        media_asset_id: Optional[UUID] = None,
        duration_ms: Optional[int] = None,
        meta_patch: Optional[Dict[str, Any]] = None,
    ) -> None:
        pool = await get_pool()
        sets = ["updated_at=now()"]
        params: List[Any] = [candidate_id]
        idx = 2

        def add(col: str, val: Any, cast: str = "") -> None:
            nonlocal idx
            params.append(val)
            sets.append(f"{col}=${idx}{cast}")
            idx += 1

        if status is not None:
            add("status", status)
        if provider is not None:
            add("provider", provider)
        if provider_run_id is not None:
            add("provider_run_id", provider_run_id)
        if content_json is not None:
            add("content_json", json.dumps(content_json), "::jsonb")
        if score_json is not None:
            add("score_json", json.dumps(score_json), "::jsonb")
        if artifact_id is not None:
            add("artifact_id", artifact_id)
        if media_asset_id is not None:
            add("media_asset_id", media_asset_id)
        if duration_ms is not None:
            add("duration_ms", int(duration_ms))

        if meta_patch:
            add("meta_json", json.dumps(meta_patch), "::jsonb")
            # rewrite last assignment into jsonb merge
            sets[-1] = f"meta_json=coalesce(meta_json,'{{}}'::jsonb) || {sets[-1].split('=',1)[1]}"

        if len(sets) == 1:
            return

        await pool.execute(
            f"update {self._T} set {', '.join(sets)} where id=$1",
            *params,
        )

    async def choose_candidate(
        self,
        *,
        job_id: UUID,
        candidate_id: UUID,
    ) -> Optional[Dict[str, Any]]:
        """
        Atomically mark candidate chosen and others in same group/attempt discarded.
        Robust to DBs without chosen_at column.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                cols = await self._get_cols(conn=conn)

                row = await conn.fetchrow(
                    f"""
                    select id, group_id, candidate_type, attempt, status
                    from {self._T}
                    where id=$1 and job_id=$2
                    for update
                    """,
                    candidate_id,
                    job_id,
                )
                if not row:
                    return None

                gid = row["group_id"]
                ctype = row["candidate_type"]
                attempt = int(row["attempt"])

                # discard others
                await conn.execute(
                    f"""
                    update {self._T}
                    set status='discarded', updated_at=now()
                    where job_id=$1 and candidate_type=$2 and group_id=$3 and attempt=$4 and id <> $5
                      and status not in ('discarded','chosen')
                    """,
                    job_id,
                    ctype,
                    gid,
                    attempt,
                    candidate_id,
                )

                # choose this one (conditionally include chosen_at)
                if "chosen_at" in cols:
                    await conn.execute(
                        f"""
                        update {self._T}
                        set status='chosen', chosen_at=now(), updated_at=now()
                        where id=$1
                        """,
                        candidate_id,
                    )
                else:
                    await conn.execute(
                        f"""
                        update {self._T}
                        set status='chosen', updated_at=now()
                        where id=$1
                        """,
                        candidate_id,
                    )

                out = await conn.fetchrow(
                    f"select * from {self._T} where id=$1",
                    candidate_id,
                )
                return dict(out) if out else None