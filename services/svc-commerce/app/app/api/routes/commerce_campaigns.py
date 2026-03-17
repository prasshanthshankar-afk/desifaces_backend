from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException

from app.db import get_pool
from app.domain.models import CommerceConfirmIn, CommerceConfirmOut

try:
    import jwt  # PyJWT
except Exception:  # pragma: no cover
    jwt = None  # type: ignore

router = APIRouter(prefix="/api/commerce", tags=["commerce"])


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _stable_hash(obj: Dict[str, Any]) -> str:
    s = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha256(s)


def _jwt_secrets() -> list[str]:
    secrets: list[str] = []
    for k in ("JWT_HMAC_SECRET", "JWT_SECRET"):
        v = (os.getenv(k) or "").strip()
        if v:
            secrets.append(v)
    return secrets


def _jwt_meta() -> tuple[Optional[str], Optional[str], str]:
    issuer = (os.getenv("JWT_ISSUER") or "").strip() or None
    audience = (os.getenv("JWT_AUDIENCE") or "").strip() or None
    alg = (os.getenv("JWT_ALG") or "HS256").strip()
    return issuer, audience, alg


def _allow_unverified_jwt() -> bool:
    v = (os.getenv("JWT_ALLOW_UNVERIFIED") or os.getenv("DF_JWT_ALLOW_UNVERIFIED") or "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


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
            if isinstance(v, str):
                v2 = json.loads(v)
                return v2 if isinstance(v2, dict) else {}
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    try:
        v = dict(x)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _pick_str(d: Dict[str, Any], key: str) -> Optional[str]:
    v = d.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _extract_stage(payload: Dict[str, Any]) -> Optional[str]:
    if "stage" in payload and payload.get("stage") is not None:
        return str(payload.get("stage"))
    comp = _as_dict(payload.get("computed"))
    if comp.get("stage") is not None:
        return str(comp.get("stage"))
    return None


async def get_current_user_id(authorization: str | None = Header(default=None)) -> UUID:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="unauthorized")

    if jwt is None:
        raise HTTPException(status_code=500, detail="PyJWT not installed in svc-commerce image")

    issuer, audience, alg = _jwt_meta()
    secrets = _jwt_secrets()

    last_err: Exception | None = None

    if secrets:
        for secret in secrets:
            try:
                options = {
                    "require": ["sub"],
                    "verify_signature": True,
                    "verify_aud": bool(audience),
                    "verify_iss": bool(issuer),
                }
                kwargs: Dict[str, Any] = {"algorithms": [alg], "options": options}
                if issuer:
                    kwargs["issuer"] = issuer
                if audience:
                    kwargs["audience"] = audience

                payload = jwt.decode(token, secret, **kwargs)  # type: ignore[misc]
                sub = payload.get("sub")
                return UUID(str(sub))
            except Exception as e:  # noqa: BLE001
                last_err = e

    if _allow_unverified_jwt():
        try:
            payload = jwt.decode(  # type: ignore[misc]
                token,
                options={"verify_signature": False, "verify_aud": False, "verify_iss": False},
            )
            sub = payload.get("sub")
            return UUID(str(sub))
        except Exception as e:  # noqa: BLE001
            last_err = e

    raise HTTPException(status_code=401, detail="unauthorized") from last_err


@router.post("/confirm", response_model=CommerceConfirmOut)
async def confirm(req: CommerceConfirmIn, user_id: UUID = Depends(get_current_user_id)) -> CommerceConfirmOut:
    pool = await get_pool()

    async with pool.acquire() as con:
        q = await con.fetchrow(
            """
            select id, request_json, response_json, total_credits, total_usd, total_inr, status, expires_at
            from public.commerce_quotes
            where id=$1 and user_id=$2
            """,
            req.quote_id,
            user_id,
        )

    if not q:
        raise HTTPException(status_code=404, detail="Quote not found")

    now = datetime.now(timezone.utc)
    if q["expires_at"] <= now:
        raise HTTPException(status_code=422, detail="Quote expired")

    status = str(q["status"] or "").lower()
    if status not in ("quoted", "approved", "confirmed"):
        raise HTTPException(status_code=422, detail=f"Quote status not valid: {q['status']}")

    quote_req = _as_dict(q["request_json"])
    quote_resp = _as_dict(q["response_json"])
    assumptions = _as_dict(quote_resp.get("assumptions"))

    mode = _pick_str(quote_req, "mode") or _pick_str(assumptions, "mode") or "platform_models"
    product_type = _pick_str(quote_req, "product_type") or _pick_str(assumptions, "product_type") or "mixed"

    idem = (req.idempotency_key or "").strip()
    idem_key = idem or str(req.quote_id)

    request_hash = _stable_hash(
        {
            "kind": "commerce_confirm",
            "user_id": str(user_id),
            "quote_id": str(req.quote_id),
            "idempotency_key": idem_key,
        }
    )

    async with pool.acquire() as con:
        existing = await con.fetchrow(
            """
            select id, meta_json
            from public.commerce_campaigns
            where user_id=$1 and quote_id=$2 and (meta_json->>'request_hash')=$3
            order by created_at desc
            limit 1
            """,
            user_id,
            req.quote_id,
            request_hash,
        )

        if existing:
            mj = _as_dict(existing["meta_json"])
            sj = mj.get("studio_job_id")
            if not sj:
                sj = await con.fetchval(
                    """
                    select id
                    from public.studio_jobs
                    where user_id=$1 and studio_type='commerce' and request_hash=$2
                    order by created_at desc
                    limit 1
                    """,
                    user_id,
                    request_hash,
                )
            if not sj:
                raise HTTPException(status_code=500, detail="Campaign exists but missing studio_job_id")

            job_status = await con.fetchval(
                """
                select status
                from public.studio_jobs
                where id=$1 and user_id=$2 and studio_type='commerce'
                """,
                UUID(str(sj)),
                user_id,
            )

            return CommerceConfirmOut(
                campaign_id=UUID(str(existing["id"])),
                studio_job_id=UUID(str(sj)),
                status=str(job_status or "queued"),
            )

    async with pool.acquire() as con:
        async with con.transaction():
            payload = {
                "quote_id": str(req.quote_id),
                "input": {
                    "quote_id": str(req.quote_id),
                    "idempotency_key": idem or None,
                },
                "quote": quote_resp,
                "quote_request": quote_req,
                "computed": {
                    "stage": "queued",
                    "request_hash": request_hash,
                    "quote_id": str(req.quote_id),
                },
            }

            meta = {
                "request_hash": request_hash,
                "request_type": "commerce_confirm",
                "quote_id": str(req.quote_id),
                "idempotency_key": idem or None,
                "totals": {"usd": float(q["total_usd"]), "inr": float(q["total_inr"])},
                "total_credits": int(q["total_credits"]),
                "expires_at": q["expires_at"].isoformat(),
                "mode": str(mode),
                "product_type": str(product_type),
            }

            studio_job_id = await con.fetchval(
                """
                insert into public.studio_jobs(studio_type, status, request_hash, payload_json, meta_json, user_id)
                values('commerce', 'queued', $1, $2::jsonb, $3::jsonb, $4)
                on conflict (user_id, studio_type, request_hash)
                do update set
                    updated_at = now(),
                    payload_json = excluded.payload_json,
                    meta_json = excluded.meta_json
                returning id
                """,
                request_hash,
                json.dumps(payload),
                json.dumps(meta),
                user_id,
            )

            campaign_id = await con.fetchval(
                """
                insert into public.commerce_campaigns(
                  user_id, mode, product_type, status, quote_id, input_json, meta_json, created_at, updated_at
                )
                values($1,$2,$3,'queued',$4,$5::jsonb,$6::jsonb,now(),now())
                returning id
                """,
                user_id,
                str(mode),
                str(product_type),
                req.quote_id,
                json.dumps(
                    {
                        "quote_id": str(req.quote_id),
                        "idempotency_key": idem or None,
                        "request_hash": request_hash,
                    }
                ),
                json.dumps(
                    {
                        **meta,
                        "studio_job_id": str(studio_job_id),
                    }
                ),
            )

            await con.execute(
                """
                update public.commerce_campaigns
                set meta_json =
                    (
                        case
                            when meta_json is null or jsonb_typeof(meta_json) <> 'object'
                                then '{}'::jsonb
                            else meta_json
                        end
                    ) || jsonb_build_object(
                        'studio_job_id', $2::text,
                        'campaign_id', $1::text,
                        'quote_id', $3::text,
                        'mode', $4::text,
                        'product_type', $5::text
                    ),
                    updated_at = now()
                where id = $1::uuid
                """,
                campaign_id,
                str(studio_job_id),
                str(req.quote_id),
                str(mode),
                str(product_type),
            )

            await con.execute(
                """
                update public.studio_jobs
                set
                  payload_json =
                    (
                        case
                            when payload_json is null or jsonb_typeof(payload_json) <> 'object'
                                then '{}'::jsonb
                            else payload_json
                        end
                    ) || jsonb_build_object(
                        'quote_id', $2::text,
                        'commerce_campaign_id', $3::text,
                        'campaign_id', $3::text
                    ),
                  meta_json =
                    (
                        case
                            when meta_json is null or jsonb_typeof(meta_json) <> 'object'
                                then '{}'::jsonb
                            else meta_json
                        end
                    ) || jsonb_build_object(
                        'quote_id', $2::text,
                        'commerce_campaign_id', $3::text,
                        'campaign_id', $3::text
                    ),
                  updated_at = now()
                where id=$1 and user_id=$4 and studio_type='commerce'
                """,
                UUID(str(studio_job_id)),
                str(req.quote_id),
                str(campaign_id),
                user_id,
            )

            await con.execute(
                """
                update public.commerce_quotes
                set status='confirmed', updated_at=now()
                where id=$1 and user_id=$2
                """,
                req.quote_id,
                user_id,
            )

    return CommerceConfirmOut(
        campaign_id=UUID(str(campaign_id)),
        studio_job_id=UUID(str(studio_job_id)),
        status="queued",
    )


@router.get("/jobs/{studio_job_id}/status")
async def job_status(studio_job_id: UUID, user_id: UUID = Depends(get_current_user_id)) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """
            select id, studio_type, status, payload_json, meta_json, error_code, error_message, updated_at
            from public.studio_jobs
            where id=$1 and user_id=$2 and studio_type='commerce'
            """,
            studio_job_id,
            user_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    payload = _as_dict(row["payload_json"])
    meta = _as_dict(row["meta_json"])
    computed = _as_dict(payload.get("computed"))
    stage = _extract_stage(payload) or _pick_str(computed, "stage")

    return {
        "studio_job_id": str(row["id"]),
        "studio_type": row["studio_type"],
        "status": row["status"],
        "stage": stage,
        "quote_id": payload.get("quote_id") or _as_dict(payload.get("input")).get("quote_id") or meta.get("quote_id"),
        "campaign_id": payload.get("campaign_id") or payload.get("commerce_campaign_id") or meta.get("campaign_id"),
        "computed": computed,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "payload_json": payload,
        "meta_json": meta,
    }