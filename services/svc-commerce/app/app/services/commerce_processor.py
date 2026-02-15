from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict
from uuid import UUID

from app.db import get_pool

# This is intentionally "safe + deterministic" for now:
# It marks the related commerce_campaign as running/succeeded and stores useful meta.
# Later you can replace the stub body with the real pipeline (catalog -> images -> promo video).


def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, (bytes, bytearray)):
        x = x.decode("utf-8", errors="ignore")
    if isinstance(x, str):
        try:
            v = json.loads(x)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    try:
        v = dict(x)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _merge_meta(old: Any, add: Dict[str, Any]) -> Dict[str, Any]:
    base = _as_dict(old)
    base.update(add)
    return base


async def process_commerce_job(*, job_id: UUID, payload: Dict[str, Any], meta: Dict[str, Any], user_id: UUID) -> None:
    """
    Worker entrypoint.

    Required contract:
    - raise Exception => worker marks studio_job failed
    - return normally => worker marks studio_job succeeded
    """
    # Extract quote_id (present in payload.input.quote_id and meta.quote_id)
    quote_id_s = (
        _as_dict(payload.get("input")).get("quote_id")
        or meta.get("quote_id")
        or _as_dict(meta).get("quote_id")
    )
    if not quote_id_s:
        raise RuntimeError("commerce_processor: missing quote_id in payload/meta")

    try:
        quote_id = UUID(str(quote_id_s))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"commerce_processor: invalid quote_id={quote_id_s}") from e

    now = datetime.now(timezone.utc).isoformat()

    pool = await get_pool()
    async with pool.acquire() as con:
        # Find the newest campaign for this user+quote (created by /confirm)
        camp = await con.fetchrow(
            """
            select id, status, meta_json
            from public.commerce_campaigns
            where user_id=$1 and quote_id=$2
            order by created_at desc
            limit 1
            """,
            user_id,
            quote_id,
        )
        if not camp:
            raise RuntimeError(f"commerce_processor: commerce_campaign not found for quote_id={quote_id}")

        campaign_id = UUID(str(camp["id"]))

        # Mark campaign running (idempotent)
        meta_add = {
            "studio_job_id": str(job_id),
            "quote_id": str(quote_id),
            "processor": "stub_v1",
            "started_at": now,
            "request_type": meta.get("request_type") or "commerce_confirm",
        }
        merged = _merge_meta(camp["meta_json"], meta_add)

        await con.execute(
            """
            update public.commerce_campaigns
            set status='running', meta_json=$2::jsonb, updated_at=now()
            where id=$1
            """,
            campaign_id,
            json.dumps(merged),
        )

        # -----------------------------
        # TODO (real pipeline goes here):
        # - create product shots via svc-face (T2I/I2I)
        # - create promo clips via svc-fusion / svc-fusion-extension
        # - write outputs into media_assets + link to campaign meta_json
        # -----------------------------

        # Mark campaign succeeded (idempotent)
        merged2 = _merge_meta(merged, {"finished_at": datetime.now(timezone.utc).isoformat(), "status": "succeeded"})
        await con.execute(
            """
            update public.commerce_campaigns
            set status='succeeded', meta_json=$2::jsonb, updated_at=now()
            where id=$1
            """,
            campaign_id,
            json.dumps(merged2),
        )