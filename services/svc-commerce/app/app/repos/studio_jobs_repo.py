from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from app.db import get_pool

_TABLE = "public.studio_jobs"


def _as_dict(x: Any) -> Dict[str, Any]:
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


class StudioJobsRepo:
    async def create_commerce_job(
        self,
        *,
        user_id: UUID,
        campaign_id: UUID,
        quote_id: UUID,
        payload_json: Dict[str, Any],
        meta_json: Optional[Dict[str, Any]] = None,
    ) -> UUID:
        jid = uuid4()
        pool = await get_pool()

        payload = _as_dict(payload_json)
        payload.setdefault("commerce_campaign_id", str(campaign_id))
        payload.setdefault("quote_id", str(quote_id))
        payload.setdefault("stage", "queued")
        payload.setdefault("computed", {})
        payload.setdefault("error", None)

        meta = _as_dict(meta_json)
        meta.setdefault("commerce_campaign_id", str(campaign_id))
        meta.setdefault("quote_id", str(quote_id))
        if payload.get("idempotency_key"):
            meta.setdefault("idempotency_key", payload.get("idempotency_key"))
        meta.setdefault("request_type", meta.get("request_type") or "commerce_confirm")

        await pool.execute(
            f"""
            insert into {_TABLE}(id, user_id, studio_type, status, payload_json, meta_json, created_at, updated_at)
            values($1,$2,'commerce','queued',$3::jsonb,$4::jsonb,now(),now())
            """,
            jid,
            user_id,
            json.dumps(payload),
            json.dumps(meta),
        )
        return jid

    async def get_latest_commerce_job_for_campaign(self, *, user_id: UUID, campaign_id: UUID) -> Optional[dict]:
        pool = await get_pool()
        row = await pool.fetchrow(
            f"""
            select id, user_id, status, payload_json, meta_json, created_at, updated_at
            from {_TABLE}
            where user_id=$1 and studio_type='commerce'
              and ((payload_json #>> '{{}}')::jsonb ->> 'commerce_campaign_id') = $2
            order by created_at desc
            limit 1
            """,
            user_id,
            str(campaign_id),
        )
        return dict(row) if row else None

    async def update_payload_json(self, *, job_id: UUID, payload_json: Dict[str, Any]) -> None:
        pool = await get_pool()
        payload = _as_dict(payload_json)
        await pool.execute(
            f"""
            update {_TABLE}
            set payload_json=$2::jsonb, updated_at=$3
            where id=$1 and studio_type='commerce'
            """,
            job_id,
            json.dumps(payload),
            datetime.now(timezone.utc),
        )