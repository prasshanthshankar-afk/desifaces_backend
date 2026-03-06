# services/svc-marketing/app/app/repos/marketing_runs_repo.py
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

import asyncpg

from app.domain.enums import MarketingRunMode, MarketingRunStatus, RecipeKind


def _stable_u32(s: str) -> int:
    # Deterministic 32-bit-ish seed from any string (run_id).
    return int(hashlib.sha256(s.encode("utf-8")).hexdigest()[:8], 16)


def _json_dumps(obj: Any) -> str:
    """
    IMPORTANT: default=str prevents UUID/Enum serialization crashes.
    Keep ensure_ascii=False so Indian languages remain readable.
    """
    return json.dumps(obj, ensure_ascii=False, default=str)


def _as_json_obj(x: Any) -> Dict[str, Any]:
    """
    Normalize anything into a JSON OBJECT (dict) suitable for jsonb columns.

    Key property: never return a scalar (string/number/etc). This prevents
    jsonb_object_keys() failures and avoids 'jsonb string containing json text'.
    """
    if x is None:
        return {}
    if isinstance(x, dict):
        return x

    # asyncpg.Record / Mapping-like
    try:
        if hasattr(x, "items"):
            return dict(x.items())  # type: ignore[arg-type]
    except Exception:
        pass

    if isinstance(x, str):
        # If someone already pre-dumped JSON, parse it once.
        try:
            v = json.loads(x)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}

    return {}


class MarketingRunsRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create_run(
        self,
        run_as_user_id: UUID,
        bearer_token: Optional[str],
        mode: MarketingRunMode,
        recipe: RecipeKind,
        cost_bucket: str,
        cost_category: str,
        input_json: Dict[str, Any],
    ) -> UUID:
        run_id = uuid4()

        # Normalize + inject per-run diversity controls (seed/nonce).
        inp = _as_json_obj(input_json)
        inp.setdefault("request_nonce", str(run_id))
        inp.setdefault("seed", _stable_u32(str(run_id)))

        q = """
        insert into marketing_runs (
          run_id, status, stage, mode, recipe,
          run_as_user_id, bearer_token,
          cost_bucket, cost_category,
          input_json, planning_json, output_json
        ) values (
          $1, $2, $3, $4, $5,
          $6, $7,
          $8, $9,
          $10::jsonb, '{}'::jsonb, '{}'::jsonb
        )
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                q,
                run_id,
                MarketingRunStatus.queued.value,
                "queued",
                mode.value,
                recipe.value,
                run_as_user_id,
                bearer_token,
                cost_bucket,
                cost_category,
                # Always dump exactly once from a dict; allow UUID/Enum safely.
                _json_dumps(inp),
            )
        return run_id

    async def get_run_row(self, run_id: UUID) -> Optional[asyncpg.Record]:
        q = "select * from marketing_runs where run_id=$1"
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(q, run_id)

    async def update_stage(self, run_id: UUID, stage: str) -> None:
        q = "update marketing_runs set stage=$2, updated_at=now() where run_id=$1"
        async with self.pool.acquire() as conn:
            await conn.execute(q, run_id, stage)

    async def set_input_json(self, run_id: UUID, input_json: Any) -> None:
        inp = _as_json_obj(input_json)
        q = "update marketing_runs set input_json=$2::jsonb, updated_at=now() where run_id=$1"
        async with self.pool.acquire() as conn:
            await conn.execute(q, run_id, _json_dumps(inp))

    async def set_planning_json(self, run_id: UUID, planning_json: Any) -> None:
        plan = _as_json_obj(planning_json)
        q = "update marketing_runs set planning_json=$2::jsonb, updated_at=now() where run_id=$1"
        async with self.pool.acquire() as conn:
            await conn.execute(q, run_id, _json_dumps(plan))

    async def set_output_json(self, run_id: UUID, output_json: Any) -> None:
        out = _as_json_obj(output_json)
        q = "update marketing_runs set output_json=$2::jsonb, updated_at=now() where run_id=$1"
        async with self.pool.acquire() as conn:
            await conn.execute(q, run_id, _json_dumps(out))

    async def mark_failed(self, run_id: UUID, stage: str, error_code: str, error_message: str) -> None:
        q = """
        update marketing_runs
        set status=$2,
            stage=$3,
            error_code=$4,
            error_message=$5,
            finished_at=now(),
            updated_at=now(),
            locked_by=null,
            heartbeat_at=null,
            lease_expires_at=null
        where run_id=$1
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, run_id, MarketingRunStatus.failed.value, stage, error_code, error_message)

    async def mark_succeeded(self, run_id: UUID) -> None:
        q = """
        update marketing_runs
        set status=$2,
            stage='done',
            finished_at=now(),
            updated_at=now(),
            locked_by=null,
            heartbeat_at=null,
            lease_expires_at=null
        where run_id=$1
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, run_id, MarketingRunStatus.succeeded.value)

    async def claim_next_run(self, worker_id: str, lease_seconds: int = 60) -> Optional[UUID]:
        q = """
        with cte as (
          select run_id
          from marketing_runs
          where status='queued'
          order by created_at asc
          for update skip locked
          limit 1
        )
        update marketing_runs r
        set status='running',
            stage='planning',
            started_at=coalesce(r.started_at, now()),
            updated_at=now(),
            locked_by=$1,
            heartbeat_at=now(),
            lease_expires_at=now() + ($2::int * interval '1 second')
        from cte
        where r.run_id = cte.run_id
        returning r.run_id
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q, worker_id, int(lease_seconds))
            return row["run_id"] if row else None

    async def heartbeat(self, run_id: UUID, worker_id: str, lease_seconds: int = 60) -> None:
        q = """
        update marketing_runs
        set updated_at=now(),
            heartbeat_at=now(),
            lease_expires_at=now() + ($3::int * interval '1 second')
        where run_id=$1
          and status='running'
          and locked_by=$2
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, run_id, worker_id, int(lease_seconds))

    async def reap_stuck_runs(self, stale_after_seconds: int, limit: int = 200) -> int:
        msg = f"reaped: stale>{int(stale_after_seconds)}s"
        q = """
        with c as (
          select run_id
          from marketing_runs
          where status='running'
            and (
              (lease_expires_at is not null and lease_expires_at < now())
              or (
                lease_expires_at is null
                and coalesce(heartbeat_at, updated_at) < now() - ($1::int * interval '1 second')
              )
            )
          order by created_at asc
          limit $3::int
          for update skip locked
        )
        update marketing_runs r
        set status='failed',
            error_code='REAPED',
            error_message=$2,
            finished_at=now(),
            updated_at=now(),
            locked_by=null,
            heartbeat_at=null,
            lease_expires_at=null
        from c
        where r.run_id = c.run_id
        returning r.run_id
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q, int(stale_after_seconds), msg, int(limit))
            return len(rows)