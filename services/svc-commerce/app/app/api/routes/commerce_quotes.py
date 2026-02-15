from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_user
from app.db import get_pool
from app.domain.models import (
    CommerceConfirmIn,
    CommerceConfirmOut,
    CommerceQuoteIn,
    CommerceQuoteOut,
)
from app.services.pricing_client import PricingClient

router = APIRouter(prefix="/api/commerce", tags=["commerce"])


def _stable_hash(obj: Dict[str, Any]) -> str:
    s = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@router.post("/quote", response_model=CommerceQuoteOut)
async def quote(req: CommerceQuoteIn, user_id: UUID = Depends(require_user)) -> CommerceQuoteOut:
    pc = PricingClient()
    out = await pc.quote(user_id=user_id, req=req)

    pool = await get_pool()
    req_json = req.model_dump(mode="json")
    out_json = out.model_dump(mode="json")

    total_usd = float(out.totals.get("usd", 0.0))
    total_inr = float(out.totals.get("inr", 0.0))

    await pool.execute(
        """
        insert into public.commerce_quotes(
            id, user_id, scope, request_json, response_json,
            total_credits, total_usd, total_inr, status, expires_at, created_at, updated_at
        )
        values(
            $1, $2, 'commerce', $3::jsonb, $4::jsonb,
            $5, $6, $7, 'quoted', $8, now(), now()
        )
        on conflict (id) do update
          set request_json = excluded.request_json,
              response_json = excluded.response_json,
              total_credits = excluded.total_credits,
              total_usd = excluded.total_usd,
              total_inr = excluded.total_inr,
              status = excluded.status,
              expires_at = excluded.expires_at,
              updated_at = now()
        """,
        out.quote_id,
        user_id,
        json.dumps(req_json),
        json.dumps(out_json),
        int(out.total_credits),
        total_usd,
        total_inr,
        out.expires_at,
    )

    return out


@router.post("/confirm", response_model=CommerceConfirmOut)
async def confirm(req: CommerceConfirmIn, user_id: UUID = Depends(require_user)) -> CommerceConfirmOut:
    pool = await get_pool()
    now = datetime.now(timezone.utc)

    # IMPORTANT: studio_jobs has FK to core.users(id). Validate early.
    user_exists = await pool.fetchval("select 1 from core.users where id = $1", user_id)
    if not user_exists:
        raise HTTPException(
            status_code=401,
            detail="unknown_user_in_core_users (token sub not present in core.users; use a token issued by svc-core login/signup)",
        )

    q = await pool.fetchrow(
        """
        select id, user_id, status, expires_at, request_json
        from public.commerce_quotes
        where id = $1 and user_id = $2
        """,
        req.quote_id,
        user_id,
    )
    if not q:
        raise HTTPException(status_code=404, detail="Quote not found")

    if q["expires_at"] <= now:
        raise HTTPException(status_code=422, detail="Quote expired")

    if q["status"] not in ("quoted", "confirmed"):
        raise HTTPException(status_code=422, detail=f"Quote not confirmable: {q['status']}")

    try:
        request_json: Dict[str, Any] = dict(q["request_json"] or {})
        mode = str(request_json.get("mode") or "platform_models")
        product_type = str(request_json.get("product_type") or "mixed")

        existing_campaign = await pool.fetchrow(
            """
            select id
            from public.commerce_campaigns
            where user_id = $1 and quote_id = $2
            order by created_at desc
            limit 1
            """,
            user_id,
            req.quote_id,
        )

        if existing_campaign:
            campaign_id = UUID(str(existing_campaign["id"]))
        else:
            campaign_id = uuid4()
            await pool.execute(
                """
                insert into public.commerce_campaigns(
                    id, user_id, mode, product_type, status, quote_id, input_json, meta_json, created_at, updated_at
                )
                values(
                    $1, $2, $3, $4, 'queued', $5, $6::jsonb, $7::jsonb, now(), now()
                )
                """,
                campaign_id,
                user_id,
                mode,
                product_type,
                req.quote_id,
                json.dumps(request_json),
                json.dumps({"source": "confirm", "idempotency_key": req.idempotency_key}),
            )

        idem = req.idempotency_key or str(req.quote_id)
        request_hash = _stable_hash({"quote_id": str(req.quote_id), "idempotency_key": idem, "kind": "commerce_confirm"})

        payload = {"quote_id": str(req.quote_id), "campaign_id": str(campaign_id), "request": request_json}
        meta = {"request_type": "commerce_confirm", "idempotency_key": req.idempotency_key, "campaign_id": str(campaign_id)}

        row = await pool.fetchrow(
            """
            insert into public.studio_jobs(
                studio_type, status, request_hash, payload_json, meta_json, user_id, created_at, updated_at, next_run_at
            )
            values('commerce', 'queued', $1, $2::jsonb, $3::jsonb, $4, now(), now(), now())
            on conflict (user_id, studio_type, request_hash)
            do update set updated_at = now()
            returning id
            """,
            request_hash,
            json.dumps(payload),
            json.dumps(meta),
            user_id,
        )

        studio_job_id = UUID(str(row["id"]))

        await pool.execute(
            "update public.commerce_quotes set status='confirmed', updated_at=now() where id=$1 and user_id=$2",
            req.quote_id,
            user_id,
        )

        return CommerceConfirmOut(campaign_id=campaign_id, studio_job_id=studio_job_id, status="queued")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("confirm_failed")
        # This will now be JSON (so jq won't blow up)
        raise HTTPException(status_code=500, detail=f"confirm_failed: {type(e).__name__}: {e}")



@router.get("/jobs/{studio_job_id}/status")
async def job_status(studio_job_id: UUID, user_id: UUID = Depends(require_user)) -> Dict[str, Any]:
    pool = await get_pool()
    j = await pool.fetchrow(
        """
        select id, status, error_code, error_message, payload_json, updated_at
        from public.studio_jobs
        where id = $1 and user_id = $2 and studio_type = 'commerce'
        """,
        studio_job_id,
        user_id,
    )
    if not j:
        raise HTTPException(status_code=404, detail="job_not_found")

    payload = dict(j["payload_json"] or {})
    return {
        "studio_job_id": str(j["id"]),
        "status": j["status"],
        "campaign_id": payload.get("campaign_id"),
        "quote_id": payload.get("quote_id"),
        "updated_at": j["updated_at"].isoformat() if j["updated_at"] else None,
        "error_code": j["error_code"],
        "error_message": j["error_message"],
    }