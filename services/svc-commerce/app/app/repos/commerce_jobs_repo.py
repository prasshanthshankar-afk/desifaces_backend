from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from app.db import get_pool

_TABLE = "public.studio_jobs"


def _normalize_payload(x: Any) -> Dict[str, Any]:
    # studio_jobs.payload_json is JSONB, but sometimes contains a JSON-string.
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        try:
            y = json.loads(x)
            if isinstance(y, str):
                y2 = json.loads(y)
                return y2 if isinstance(y2, dict) else {}
            return y if isinstance(y, dict) else {}
        except Exception:
            return {}
    return {}


class CommerceJobsRepo:
    """
    IMPORTANT:
    - We do NOT use a commerce_jobs table (not present in your DB).
    - Commerce jobs are rows in public.studio_jobs with studio_type='commerce'.
    """

    async def create(
        self,
        *,
        user_id: UUID,
        campaign_id: UUID,
        job_type: str,
        input_json: Dict[str, Any],
    ) -> UUID:
        jid = uuid4()
        pool = await get_pool()

        payload = {
            "campaign_id": str(campaign_id),
            "job_type": job_type,
            "stage": "queued",
            "input": input_json or {},
            "computed": {},
            "error": None,
        }

        await pool.execute(
            f"""
            insert into {_TABLE}(id, user_id, studio_type, status, payload_json, created_at, updated_at)
            values($1, $2, 'commerce', 'queued', $3::jsonb, now(), now())
            """,
            jid,
            user_id,
            json.dumps(payload),
        )
        return jid

    async def get(self, *, user_id: UUID, job_id: UUID) -> Optional[dict]:
        pool = await get_pool()
        row = await pool.fetchrow(
            f"""
            select id, user_id, studio_type, status, payload_json, created_at, updated_at
            from {_TABLE}
            where id=$1 and user_id=$2 and studio_type='commerce'
            """,
            job_id,
            user_id,
        )
        if not row:
            return None
        d = dict(row)
        d["payload"] = _normalize_payload(d.get("payload_json"))
        return d

    async def list_queued(self, *, limit: int = 5) -> List[dict]:
        pool = await get_pool()
        rows = await pool.fetch(
            f"""
            select id, user_id, studio_type, status, payload_json, created_at, updated_at
            from {_TABLE}
            where studio_type='commerce' and status='queued'
            order by created_at asc
            limit $1
            """,
            limit,
        )
        out: List[dict] = []
        for r in rows:
            d = dict(r)
            d["payload"] = _normalize_payload(d.get("payload_json"))
            out.append(d)
        return out

    async def set_running(self, *, job_id: UUID, stage: str, payload: Dict[str, Any]) -> None:
        payload = dict(payload or {})
        payload["stage"] = stage

        pool = await get_pool()
        await pool.execute(
            f"""
            update {_TABLE}
            set status='running', payload_json=$2::jsonb, updated_at=$3
            where id=$1 and studio_type='commerce'
            """,
            job_id,
            json.dumps(payload),
            datetime.now(timezone.utc),
        )

    async def set_succeeded(self, *, job_id: UUID, payload: Dict[str, Any]) -> None:
        payload = dict(payload or {})
        payload["stage"] = "succeeded"
        payload["error"] = None

        pool = await get_pool()
        await pool.execute(
            f"""
            update {_TABLE}
            set status='succeeded', payload_json=$2::jsonb, updated_at=$3
            where id=$1 and studio_type='commerce'
            """,
            job_id,
            json.dumps(payload),
            datetime.now(timezone.utc),
        )

    async def set_failed(self, *, job_id: UUID, payload: Dict[str, Any], error_text: str) -> None:
        payload = dict(payload or {})
        payload["stage"] = "failed"
        payload["error"] = error_text

        pool = await get_pool()
        await pool.execute(
            f"""
            update {_TABLE}
            set status='failed', payload_json=$2::jsonb, updated_at=$3
            where id=$1 and studio_type='commerce'
            """,
            job_id,
            json.dumps(payload),
            datetime.now(timezone.utc),
        )