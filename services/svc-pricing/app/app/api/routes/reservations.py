from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import PoolDep

router = APIRouter(tags=["pricing-reservations"])


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------
def _as_dict_loose(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            v = json.loads(s)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    try:
        return dict(x)
    except Exception:
        return {}


def _to_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(Decimal(str(value)))
    except Exception:
        return default


def _to_units_str(value: Any, default: str = "0") -> str:
    d = _to_decimal(value, default=default)
    if d == d.to_integral():
        return str(int(d))
    return format(d.normalize(), "f")


def _money_for_units(units: Any, rate: str = "1.00") -> str:
    u = _to_decimal(units, default="0")
    r = _to_decimal(rate, default="1.00")
    amt = (u * r).quantize(Decimal("0.01"))
    return format(amt, "f")


def _clean_bearer(token: str) -> str:
    value = (token or "").strip()
    if not value:
        return ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


class PricingCallerContext(BaseModel):
    user_id: str
    service_name: str
    auth_mode: str = "internal_bearer"


def _require_internal_pricing_caller(
    authorization: Optional[str],
    x_user_id: Optional[str],
    x_service_name: Optional[str],
) -> PricingCallerContext:
    expected = _clean_bearer(
        os.getenv("DF_PRICING_INTERNAL_BEARER")
        or os.getenv("SVC_TO_SVC_BEARER")
        or ""
    )
    provided = _clean_bearer(authorization or "")

    if not expected:
        raise HTTPException(status_code=500, detail="pricing_internal_auth_misconfigured")

    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="unauthorized")

    user_id = (x_user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="missing_x_user_id")

    service_name = (x_service_name or "").strip() or "unknown"

    allowed = {
        s.strip()
        for s in (os.getenv("DF_PRICING_ALLOWED_SERVICES", "svc-face,svc-commerce,svc-music,svc-marketing")).split(",")
        if s.strip()
    }
    if allowed and service_name not in allowed:
        raise HTTPException(status_code=403, detail="service_not_allowed")

    # validate UUID early
    try:
        UUID(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_x_user_id")

    return PricingCallerContext(
        user_id=user_id,
        service_name=service_name,
        auth_mode="internal_bearer",
    )


# ---------------------------------------------------------------------
# request / response models
# ---------------------------------------------------------------------
class PricingReserveRequest(BaseModel):
    user_id: str
    service_name: str
    service_action: str
    sku_code: str
    units: str
    external_ref_type: str = "studio_job"
    external_ref_id: str
    idempotency_key: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class PricingReserveResponse(BaseModel):
    status: str = "reserved"
    reservation_id: Optional[str] = None
    quote_id: Optional[str] = None
    reserved_units: Optional[str] = None
    billed_units: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    ledger_entry_id: Optional[str] = None
    message: Optional[str] = None


class PricingCommitRequest(BaseModel):
    user_id: str
    reservation_id: str
    actual_units: str
    external_ref_type: str = "studio_job"
    external_ref_id: str
    idempotency_key: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class PricingCommitResponse(BaseModel):
    status: str = "committed"
    reservation_id: Optional[str] = None
    billed_units: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    ledger_entry_id: Optional[str] = None
    message: Optional[str] = None


class PricingReleaseRequest(BaseModel):
    user_id: str
    reservation_id: str
    reason: str
    external_ref_type: str = "studio_job"
    external_ref_id: str
    idempotency_key: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class PricingReleaseResponse(BaseModel):
    status: str = "released"
    reservation_id: Optional[str] = None
    released_units: Optional[str] = None
    message: Optional[str] = None


class ReservationOut(BaseModel):
    reservation_id: UUID
    status: str
    expires_at: str
    reserved_credits: int
    currency: str
    estimated_money: str = "0"
    channel: str = ""
    country_code: str = ""
    variant_code: str = ""
    category: str = ""
    billing_mode_snapshot: str = ""
    hold_applied: bool = False
    quote_breakdown: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------
# reserve
# ---------------------------------------------------------------------
@router.post("/api/pricing/reservations/reserve", response_model=PricingReserveResponse)
async def reserve_reservation(
    req: PricingReserveRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_service_name: Optional[str] = Header(default=None, alias="X-Service-Name"),
    pool=PoolDep,
) -> PricingReserveResponse:
    caller = _require_internal_pricing_caller(authorization, x_user_id, x_service_name)

    if str(req.user_id) != str(caller.user_id):
        raise HTTPException(status_code=403, detail="user_mismatch")

    reservation_id = uuid4()
    quote_id = uuid4()
    reserved_units = _to_units_str(req.units, "1")
    reserved_credits = max(1, _to_int(req.units, 1))
    currency = "USD"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                """
                SELECT id, status, reserved_credits, currency, estimated_money, quote_json
                FROM pricing_credit_reservations
                WHERE user_id = $1 AND idempotency_key = $2
                LIMIT 1
                """,
                UUID(str(req.user_id)),
                req.idempotency_key,
            )
            if existing:
                q = _as_dict_loose(existing["quote_json"])
                return PricingReserveResponse(
                    status=str(existing["status"] or "reserved"),
                    reservation_id=str(existing["id"]),
                    quote_id=str(q.get("quote_id") or ""),
                    reserved_units=str(existing["reserved_credits"] or reserved_credits),
                    currency=str(existing["currency"] or currency),
                    amount=str(existing["estimated_money"] or ""),
                    message="Reservation already exists (idempotent replay)",
                )

            quote_json = {
                "quote_id": str(quote_id),
                "variant_code": req.sku_code,
                "category": req.service_name,
                "billing_mode_snapshot": "reservation",
                "hold_applied": True,
                "service_name": req.service_name,
                "service_action": req.service_action,
                "external_ref_type": req.external_ref_type,
                "external_ref_id": req.external_ref_id,
                "idempotency_key": req.idempotency_key,
                "units": reserved_units,
                "currency": currency,
                "caller_service_name": caller.service_name,
                **(req.meta or {}),
            }

            await conn.execute(
                """
                INSERT INTO pricing_credit_reservations (
                  id,
                  user_id,
                  status,
                  idempotency_key,
                  reserved_credits,
                  expires_at,
                  currency,
                  estimated_money,
                  channel,
                  country_code,
                  quote_json
                )
                VALUES (
                  $1, $2, 'reserved', $3, $4, $5, $6, $7, $8, $9, $10::jsonb
                )
                """,
                reservation_id,
                UUID(str(req.user_id)),
                req.idempotency_key,
                reserved_credits,
                expires_at,
                currency,
                _money_for_units(reserved_units, "1.00"),
                str((req.meta or {}).get("channel") or ""),
                str((req.meta or {}).get("country_code") or ""),
                json.dumps(quote_json),
            )

    return PricingReserveResponse(
        status="reserved",
        reservation_id=str(reservation_id),
        quote_id=str(quote_id),
        reserved_units=reserved_units,
        currency=currency,
        message="Reservation created",
    )


# ---------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------
@router.post("/api/pricing/reservations/commit", response_model=PricingCommitResponse)
async def commit_reservation(
    req: PricingCommitRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_service_name: Optional[str] = Header(default=None, alias="X-Service-Name"),
    pool=PoolDep,
) -> PricingCommitResponse:
    caller = _require_internal_pricing_caller(authorization, x_user_id, x_service_name)

    if str(req.user_id) != str(caller.user_id):
        raise HTTPException(status_code=403, detail="user_mismatch")

    billed_units = _to_units_str(req.actual_units, "1")
    ledger_entry_id = str(uuid4())

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, status, quote_json
                FROM pricing_credit_reservations
                WHERE id = $1 AND user_id = $2
                FOR UPDATE
                """,
                UUID(str(req.reservation_id)),
                UUID(str(req.user_id)),
            )
            if not row:
                raise HTTPException(status_code=404, detail="PRICING_RESERVATION_NOT_FOUND")

            current_status = str(row["status"] or "")
            if current_status == "committed":
                quote = _as_dict_loose(row["quote_json"])
                return PricingCommitResponse(
                    status="committed",
                    reservation_id=req.reservation_id,
                    billed_units=str(quote.get("actual_units") or billed_units),
                    amount=_money_for_units(quote.get("actual_units") or billed_units, "1.00"),
                    currency="USD",
                    ledger_entry_id=str(quote.get("ledger_entry_id") or ""),
                    message="Reservation already committed (idempotent replay)",
                )

            if current_status == "released":
                raise HTTPException(status_code=409, detail="PRICING_RESERVATION_ALREADY_RELEASED")

            await conn.execute(
                """
                UPDATE pricing_credit_reservations
                SET
                  status = 'committed',
                  estimated_money = $3,
                  quote_json = COALESCE(quote_json, '{}'::jsonb)
                               || jsonb_build_object(
                                    'committed_at', $4::text,
                                    'actual_units', $5::text,
                                    'ledger_entry_id', $6::text,
                                    'commit_idempotency_key', $7::text,
                                    'commit_service_name', $8::text
                                  )
                WHERE id = $1 AND user_id = $2
                """,
                UUID(str(req.reservation_id)),
                UUID(str(req.user_id)),
                _money_for_units(billed_units, "1.00"),
                datetime.now(timezone.utc).isoformat(),
                billed_units,
                ledger_entry_id,
                req.idempotency_key,
                caller.service_name,
            )

    return PricingCommitResponse(
        status="committed",
        reservation_id=req.reservation_id,
        billed_units=billed_units,
        amount=_money_for_units(billed_units, "1.00"),
        currency="USD",
        ledger_entry_id=ledger_entry_id,
        message="Reservation committed",
    )


# ---------------------------------------------------------------------
# release
# ---------------------------------------------------------------------
@router.post("/api/pricing/reservations/release", response_model=PricingReleaseResponse)
async def release_reservation(
    req: PricingReleaseRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_service_name: Optional[str] = Header(default=None, alias="X-Service-Name"),
    pool=PoolDep,
) -> PricingReleaseResponse:
    caller = _require_internal_pricing_caller(authorization, x_user_id, x_service_name)

    if str(req.user_id) != str(caller.user_id):
        raise HTTPException(status_code=403, detail="user_mismatch")

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, status
                FROM pricing_credit_reservations
                WHERE id = $1 AND user_id = $2
                FOR UPDATE
                """,
                UUID(str(req.reservation_id)),
                UUID(str(req.user_id)),
            )
            if not row:
                raise HTTPException(status_code=404, detail="PRICING_RESERVATION_NOT_FOUND")

            current_status = str(row["status"] or "")
            if current_status == "released":
                return PricingReleaseResponse(
                    status="released",
                    reservation_id=req.reservation_id,
                    released_units=None,
                    message="Reservation already released (idempotent replay)",
                )

            if current_status == "committed":
                raise HTTPException(status_code=409, detail="PRICING_RESERVATION_ALREADY_COMMITTED")

            await conn.execute(
                """
                UPDATE pricing_credit_reservations
                SET
                  status = 'released',
                  quote_json = COALESCE(quote_json, '{}'::jsonb)
                               || jsonb_build_object(
                                    'released_at', $3::text,
                                    'release_reason', $4::text,
                                    'release_idempotency_key', $5::text,
                                    'release_service_name', $6::text
                                  )
                WHERE id = $1 AND user_id = $2
                """,
                UUID(str(req.reservation_id)),
                UUID(str(req.user_id)),
                datetime.now(timezone.utc).isoformat(),
                req.reason,
                req.idempotency_key,
                caller.service_name,
            )

    return PricingReleaseResponse(
        status="released",
        reservation_id=req.reservation_id,
        released_units=None,
        message=f"Reservation released: {req.reason}",
    )


# ---------------------------------------------------------------------
# read
# ---------------------------------------------------------------------
@router.get("/api/pricing/reservations/{reservation_id}", response_model=ReservationOut)
async def get_reservation(
    reservation_id: UUID,
    include_quote: bool = Query(default=False),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_service_name: Optional[str] = Header(default=None, alias="X-Service-Name"),
    pool=PoolDep,
) -> ReservationOut:
    caller = _require_internal_pricing_caller(authorization, x_user_id, x_service_name)

    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            SELECT
              id,
              status,
              reserved_credits,
              expires_at,
              currency,
              estimated_money,
              channel,
              country_code,
              quote_json
            FROM pricing_credit_reservations
            WHERE user_id = $1 AND id = $2
            """,
            UUID(str(caller.user_id)),
            reservation_id,
        )
        if not r:
            raise HTTPException(status_code=404, detail="PRICING_RESERVATION_NOT_FOUND")

        quote = _as_dict_loose(r["quote_json"])
        qb = quote if include_quote else {}

        return ReservationOut(
            reservation_id=UUID(str(r["id"])),
            status=str(r["status"]),
            expires_at=r["expires_at"].isoformat() if r["expires_at"] else "",
            reserved_credits=int(r["reserved_credits"] or 0),
            currency=str(r["currency"] or quote.get("currency") or ""),
            estimated_money=str(r["estimated_money"] or quote.get("total_money") or "0"),
            channel=str(r["channel"] or quote.get("channel") or ""),
            country_code=str(r["country_code"] or quote.get("country_code") or ""),
            variant_code=str(quote.get("variant_code") or ""),
            category=str(quote.get("category") or ""),
            billing_mode_snapshot=str(quote.get("billing_mode_snapshot") or ""),
            hold_applied=bool(quote.get("hold_applied", False)),
            quote_breakdown=qb,
        )