
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg

from app.config import settings
from app.repo.billing_entitlements_repo import BillingEntitlementsRepo
from app.services.engine.pricing_engine import QuoteResult, quote_variant

logger = logging.getLogger(__name__)

_BILLING_ENTITLEMENTS_REPO = BillingEntitlementsRepo()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict_loose(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}

    if isinstance(x, dict):
        return x

    if isinstance(x, (list, tuple)):
        merged: Dict[str, Any] = {}
        for item in x:
            if isinstance(item, dict):
                merged.update(item)
                continue
            if isinstance(item, str):
                s = item.strip()
                if not s:
                    continue
                try:
                    v = json.loads(s)
                except Exception:
                    continue
                if isinstance(v, dict):
                    merged.update(v)
                elif isinstance(v, (list, tuple)):
                    merged.update(_as_dict_loose(v))
        return merged

    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            v = json.loads(s)
        except Exception:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, (list, tuple)):
            return _as_dict_loose(v)
        return {}

    try:
        return dict(x)
    except Exception:
        return {}


def _as_decimal(x: Any, default: str = "0") -> Decimal:
    if x is None:
        return Decimal(default)
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


def _as_int(x: Any, default: int = 0) -> int:
    try:
        return int(Decimal(str(x)))
    except Exception:
        return default


def _as_bool(x: Any, default: bool = False) -> bool:
    if x is None:
        return default
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float, Decimal)):
        return bool(x)
    s = str(x).strip().lower()
    if s in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "f", "no", "n", "off", ""}:
        return False
    return default


def _as_uuid_or_none(x: Any) -> Optional[UUID]:
    if x is None:
        return None
    try:
        s = str(x).strip()
        if not s or s.lower() == "none":
            return None
        return UUID(s)
    except Exception:
        return None


def _clean_text(x: Any, *, default: str = "") -> str:
    if x is None:
        return default
    s = str(x).strip()
    return s if s else default


def _clean_currency(x: Any, *, default: str = "") -> str:
    s = _clean_text(x, default=default)
    return s.upper() if s else default


def _clean_country_code(x: Any, *, default: str = "") -> str:
    # Keep it lenient: preserve non-empty user/system values, just trim + uppercase.
    s = _clean_text(x, default=default)
    return s.upper() if s else default


def _derive_tier_code(
    tier_code: Any,
    *,
    billing_account_id: Optional[UUID],
    settlement_mode: Optional[str],
) -> str:
    explicit = _clean_text(tier_code)
    if explicit:
        return explicit

    normalized_settlement_mode = _normalize_settlement_mode(settlement_mode)
    if billing_account_id and normalized_settlement_mode == "postpaid":
        return "enterprise"
    if billing_account_id and normalized_settlement_mode == "hybrid":
        return "business"
    return "free"


def _derive_entitlement_source(
    entitlement_source: Any,
    *,
    billing_account_id: Optional[UUID],
    settlement_mode: Optional[str],
) -> str:
    explicit = _clean_text(entitlement_source)
    if explicit:
        return explicit

    normalized_settlement_mode = _normalize_settlement_mode(settlement_mode)
    if billing_account_id and normalized_settlement_mode == "postpaid":
        return "credit_account"
    if billing_account_id:
        return "billing_account"
    return "module_gate_fallback"


def _normalize_settlement_mode(x: Any) -> str:
    s = str(x or "").strip().lower()
    if s in {"postpaid", "invoice", "bill", "billed"}:
        return "postpaid"
    if s in {"prepaid", "credit", "credits", "wallet", "payg"}:
        return "prepaid"
    if s in {"hybrid", "mixed"}:
        return "hybrid"
    return ""


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        pass
    getter = getattr(row, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:
            return default
    return default


def _q_money(x: Decimal) -> Decimal:
    q = Decimal("1").scaleb(-settings.MONEY_DECIMALS)
    return x.quantize(q, rounding=ROUND_HALF_UP)


def _q_pct(x: Decimal) -> Decimal:
    q = Decimal("1").scaleb(-4)
    return x.quantize(q, rounding=ROUND_HALF_UP)


def _held_credits_from_reservation(row_reserved_credits: int, quote: Dict[str, Any]) -> int:
    explicit = quote.get("reserved_hold_credits")
    if explicit not in (None, ""):
        return max(0, _as_int(explicit, 0))

    if "hold_applied" in quote:
        return max(0, row_reserved_credits) if _as_bool(quote.get("hold_applied"), False) else 0

    snapshot_mode = str(quote.get("billing_mode_snapshot") or quote.get("billing_mode") or "").strip().lower()
    settlement_mode = _normalize_settlement_mode(quote.get("settlement_mode")) or "prepaid"
    return max(0, row_reserved_credits) if (snapshot_mode == "bill" and settlement_mode == "prepaid" and row_reserved_credits > 0) else 0


def _ledger_metadata_with_service_job_hints(
    metadata: Optional[dict],
    *,
    service_name: Optional[str] = None,
    service_action: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    studio_job_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    md = _as_dict_loose(metadata)
    p = _as_dict_loose(params)

    service_job_id = (
        md.get("service_job_id")
        or p.get("service_job_id")
        or p.get("longform_job_id")
    )
    service_job_table = (
        md.get("service_job_table")
        or p.get("service_job_table")
    )
    pricing_entity_kind = (
        md.get("pricing_entity_kind")
        or p.get("pricing_entity_kind")
    )
    omit_studio_job_id = (
        md.get("omit_studio_job_id")
        if md.get("omit_studio_job_id") is not None
        else p.get("omit_studio_job_id")
    )

    effective_service_name = _clean_text(service_name) or _clean_text(md.get("service_name")) or _clean_text(p.get("service_name"))
    effective_service_action = _clean_text(service_action) or _clean_text(md.get("service_action")) or _clean_text(p.get("service_action"))

    is_longform_service = (
        effective_service_name == "svc-fusion-extension"
        or effective_service_action.startswith("fusion.longform.")
        or _clean_text(p.get("external_ref_type")) == "longform_run"
        or str(p.get("service_job_table") or "").strip() == "longform_jobs"
    )

    if is_longform_service:
        if not service_job_id and studio_job_id:
            service_job_id = str(studio_job_id)
        if not service_job_table:
            service_job_table = "longform_jobs"
        if pricing_entity_kind in ("", None):
            pricing_entity_kind = "service_job"
        if omit_studio_job_id is None:
            omit_studio_job_id = True

    if service_job_id:
        md["service_job_id"] = str(service_job_id)
    if service_job_table:
        md["service_job_table"] = str(service_job_table)
    if pricing_entity_kind:
        md["pricing_entity_kind"] = str(pricing_entity_kind)
    if omit_studio_job_id is not None:
        md["omit_studio_job_id"] = _as_bool(omit_studio_job_id)

    return md


async def _normalize_ledger_target(
    conn: asyncpg.Connection,
    *,
    studio_job_id: Optional[UUID],
    metadata: Optional[dict],
    service_name: Optional[str] = None,
    service_action: Optional[str] = None,
) -> tuple[Optional[UUID], Dict[str, Any]]:
    md = _as_dict_loose(metadata)
    effective_service_name = _clean_text(service_name) or _clean_text(md.get("service_name"))
    effective_service_action = _clean_text(service_action) or _clean_text(md.get("service_action"))

    omit_studio_job_id = _as_bool(md.get("omit_studio_job_id"), False)
    pricing_entity_kind = _clean_text(md.get("pricing_entity_kind")).lower()
    service_job_table = _clean_text(md.get("service_job_table"))
    service_job_id = _clean_text(md.get("service_job_id"))

    if (
        omit_studio_job_id
        or pricing_entity_kind == "service_job"
        or (service_job_table and service_job_table != "studio_jobs")
    ):
        if studio_job_id and not service_job_id:
            md["service_job_id"] = str(studio_job_id)
        if not service_job_table:
            if effective_service_name == "svc-fusion-extension" or effective_service_action.startswith("fusion.longform."):
                md["service_job_table"] = "longform_jobs"
            else:
                md["service_job_table"] = "external_or_service_job"
        md["pricing_entity_kind"] = "service_job"
        md["omit_studio_job_id"] = True
        return None, md

    if studio_job_id is not None:
        try:
            exists = await conn.fetchval(
                "select 1 from public.studio_jobs where id = $1::uuid",
                studio_job_id,
            )
        except Exception:
            exists = 1
        if not exists:
            if not service_job_id:
                md["service_job_id"] = str(studio_job_id)
            if not service_job_table:
                if effective_service_name == "svc-fusion-extension" or effective_service_action.startswith("fusion.longform."):
                    md["service_job_table"] = "longform_jobs"
                else:
                    md["service_job_table"] = "external_or_service_job"
            md["pricing_entity_kind"] = "service_job"
            md["omit_studio_job_id"] = True
            return None, md

    return studio_job_id, md


def _ledger_leaf_sku_code_from_quote(quote: Dict[str, Any]) -> Optional[str]:
    """
    pricing_credit_ledger_events.sku_code has an FK to public.pricing_skus(code).

    Top-level request codes like:
      - face.creator.generate.t2i
      - face.creator.generate.i2i
    are often pricing variants / service actions, not leaf sku rows.

    For ledger rows, derive a true leaf sku from quote lines.
    If exactly one distinct line sku_code exists, use it.
    Otherwise return None and keep the requested code in metadata.
    """
    try:
        line_skus: list[str] = []
        for ln in (quote.get("lines") or []):
            d = _as_dict_loose(ln)
            s = str(d.get("sku_code") or "").strip()
            if s:
                line_skus.append(s)

        uniq = sorted(set(line_skus))
        if len(uniq) == 1:
            return uniq[0]
        return None
    except Exception:
        return None


@dataclass(frozen=True)
class BalanceView:
    balance_credits: int
    reserved_credits: int
    available_credits: int


@dataclass(frozen=True)
class ReservationView:
    reservation_id: UUID
    status: str
    reserved_credits: int
    expires_at: datetime
    currency: str
    estimated_money: Decimal
    quote: dict


@dataclass(frozen=True)
class FinalizeReceipt:
    reservation_id: UUID
    status: str
    charged_credits: int
    charged_money: Decimal
    balance_before: int
    reserved_before: int
    balance_after: int
    reserved_after: int
    available_after: int



async def _table_exists(conn: asyncpg.Connection, regclass_name: str) -> bool:
    try:
        row = await conn.fetchrow("select to_regclass($1) as reg", regclass_name)
        return bool(row and row.get("reg"))
    except Exception:
        return False


async def _ensure_account_row(conn: asyncpg.Connection, user_id: UUID) -> None:
    await conn.execute(
        """
        insert into pricing_credit_accounts(user_id, balance_credits, reserved_credits, updated_at)
        values($1, 0, 0, now())
        on conflict (user_id) do nothing
        """,
        user_id,
    )


async def _fetch_active_entitlement_row(conn: asyncpg.Connection, user_id: UUID):
    try:
        return await conn.fetchrow(
            """
            select
              user_id,
              tier_code,
              plan_code,
              billing_mode,
              settlement_mode,
              included_credits_total,
              included_credits_remaining,
              effective_from,
              effective_to,
              updated_at,
              source
            from billing_entitlements
            where user_id = $1
              and effective_from <= now()
              and (effective_to is null or effective_to > now())
            order by effective_from desc nulls last, updated_at desc nulls last
            limit 1
            """,
            user_id,
        )
    except Exception:
        return None


def _lot_bucket_priority(bucket_type: str) -> int:
    bucket = str(bucket_type or "").strip().lower()
    if bucket == "included":
        return 0
    if bucket == "promo":
        return 1
    if bucket == "purchased":
        return 2
    return 9


async def _has_credit_lots(conn: asyncpg.Connection, user_id: UUID) -> bool:
    if not await _table_exists(conn, "public.pricing_credit_lots"):
        return False
    row = await conn.fetchrow(
        "select exists(select 1 from pricing_credit_lots where user_id = $1) as present",
        user_id,
    )
    return bool(row and row.get("present"))


async def _backfill_legacy_lots_if_needed(conn: asyncpg.Connection, user_id: UUID) -> bool:
    if not await _table_exists(conn, "public.pricing_credit_lots"):
        return False

    if await _has_credit_lots(conn, user_id):
        return False

    ent = await _fetch_active_entitlement_row(conn, user_id)
    acc = await conn.fetchrow(
        """
        select user_id, billing_account_id, balance_credits, reserved_credits, settlement_mode, updated_at
        from pricing_credit_accounts
        where user_id = $1
        """,
        user_id,
    )
    if not acc:
        return False

    account_settlement_mode = _normalize_settlement_mode(_row_get(acc, "settlement_mode"))
    entitlement_settlement_mode = _normalize_settlement_mode(_row_get(ent, "settlement_mode"))
    effective_settlement_mode = account_settlement_mode or entitlement_settlement_mode or "prepaid"
    if effective_settlement_mode == "postpaid":
        return False

    balance_credits = max(0, _as_int(_row_get(acc, "balance_credits"), 0))
    reserved_credits = max(0, _as_int(_row_get(acc, "reserved_credits"), 0))

    included_remaining = max(0, _as_int(_row_get(ent, "included_credits_remaining"), 0))
    included_total = max(0, _as_int(_row_get(ent, "included_credits_total"), 0))
    included_amount = 0
    if included_remaining > 0:
        included_amount = min(balance_credits, included_remaining) if balance_credits > 0 else included_remaining
    elif included_total > 0 and balance_credits > 0:
        # Defensive fallback for partially inconsistent rows where included total exists but remaining drifted to zero.
        included_amount = min(balance_credits, included_total)

    purchased_amount = max(0, balance_credits - included_amount)

    billing_account_id = _as_uuid_or_none(_row_get(acc, "billing_account_id"))
    plan_code = _clean_text(_row_get(ent, "plan_code"), default="legacy")
    if included_amount > 0:
        await conn.execute(
            """
            insert into pricing_credit_lots(
              user_id, billing_account_id, bucket_type, source_type, source_ref, plan_code_at_grant,
              granted_amount, remaining_amount, reserved_amount, granted_at, expires_at, status,
              metadata_json, created_at, updated_at
            )
            values(
              $1, $2, 'included', 'migration', 'legacy_balance_backfill', $3,
              $4, $4, 0, now(), null, 'active',
              $5::jsonb, now(), now()
            )
            """,
            user_id,
            billing_account_id,
            plan_code or "legacy",
            Decimal(str(included_amount)),
            json.dumps(
                {
                    "reason": "legacy_balance_backfill",
                    "legacy_balance_credits": balance_credits,
                    "legacy_reserved_credits": reserved_credits,
                    "legacy_settlement_mode": _row_get(acc, "settlement_mode"),
                    "legacy_updated_at": _row_get(acc, "updated_at"),
                },
                default=str,
            ),
        )

    if purchased_amount > 0 or (balance_credits == 0 and reserved_credits > 0):
        await conn.execute(
            """
            insert into pricing_credit_lots(
              user_id, billing_account_id, bucket_type, source_type, source_ref, plan_code_at_grant,
              granted_amount, remaining_amount, reserved_amount, granted_at, expires_at, status,
              metadata_json, created_at, updated_at
            )
            values(
              $1, $2, 'purchased', 'migration', 'legacy_balance_backfill', $3,
              $4, $4, 0, now(), null, 'active',
              $5::jsonb, now(), now()
            )
            """,
            user_id,
            billing_account_id,
            plan_code or "legacy",
            Decimal(str(max(0, purchased_amount))),
            json.dumps(
                {
                    "reason": "legacy_balance_backfill",
                    "legacy_balance_credits": balance_credits,
                    "legacy_reserved_credits": reserved_credits,
                    "legacy_settlement_mode": _row_get(acc, "settlement_mode"),
                    "legacy_updated_at": _row_get(acc, "updated_at"),
                },
                default=str,
            ),
        )

    return True


async def _fetch_active_lots(conn: asyncpg.Connection, user_id: UUID) -> list[dict]:
    if not await _table_exists(conn, "public.pricing_credit_lots"):
        return []
    rows = await conn.fetch(
        """
        select
          id,
          user_id,
          billing_account_id,
          bucket_type,
          source_type,
          source_ref,
          plan_code_at_grant,
          granted_amount,
          remaining_amount,
          reserved_amount,
          granted_at,
          expires_at,
          status,
          metadata_json,
          created_at,
          updated_at
        from pricing_credit_lots
        where user_id = $1
          and status = 'active'
          and (expires_at is null or expires_at > now())
        order by
          case bucket_type
            when 'included' then 0
            when 'promo' then 1
            when 'purchased' then 2
            else 9
          end,
          expires_at nulls last,
          granted_at asc,
          created_at asc
        """,
        user_id,
    )
    return [dict(r) for r in rows]


def _summarize_lots(lots: list[dict]) -> dict:
    summary = {
        "included_available": Decimal("0"),
        "included_reserved": Decimal("0"),
        "purchased_available": Decimal("0"),
        "purchased_reserved": Decimal("0"),
        "promo_available": Decimal("0"),
        "promo_reserved": Decimal("0"),
        "balance_total": Decimal("0"),
        "reserved_total": Decimal("0"),
        "available_total": Decimal("0"),
    }
    for lot in lots:
        bucket = str(lot.get("bucket_type") or "").strip().lower()
        remaining = _as_decimal(lot.get("remaining_amount"), "0")
        reserved = _as_decimal(lot.get("reserved_amount"), "0")
        available = max(Decimal("0"), remaining - reserved)
        summary["balance_total"] += remaining
        summary["reserved_total"] += reserved
        summary["available_total"] += available
        if bucket == "included":
            summary["included_available"] += available
            summary["included_reserved"] += reserved
        elif bucket == "purchased":
            summary["purchased_available"] += available
            summary["purchased_reserved"] += reserved
        elif bucket == "promo":
            summary["promo_available"] += available
            summary["promo_reserved"] += reserved
    return summary


async def _sync_legacy_credit_account_summary(conn: asyncpg.Connection, user_id: UUID) -> None:
    await _ensure_account_row(conn, user_id)
    if not await _table_exists(conn, "public.pricing_credit_lots"):
        return
    lots = await _fetch_active_lots(conn, user_id)
    if not lots:
        return
    summary = _summarize_lots(lots)
    await conn.execute(
        """
        update pricing_credit_accounts
        set
          balance_credits = $2,
          reserved_credits = $3,
          updated_at = now()
        where user_id = $1
        """,
        user_id,
        int(summary["balance_total"]),
        int(summary["reserved_total"]),
    )


async def get_balance(conn: asyncpg.Connection, user_id: UUID) -> BalanceView:
    await _ensure_account_row(conn, user_id)
    await _backfill_legacy_lots_if_needed(conn, user_id)
    lots = await _fetch_active_lots(conn, user_id)
    if lots:
        summary = _summarize_lots(lots)
        return BalanceView(
            balance_credits=int(summary["balance_total"]),
            reserved_credits=int(summary["reserved_total"]),
            available_credits=int(summary["available_total"]),
        )

    r = await conn.fetchrow(
        "select balance_credits, reserved_credits from pricing_credit_accounts where user_id = $1",
        user_id,
    )
    bal = int(r["balance_credits"])
    res = int(r["reserved_credits"])
    return BalanceView(balance_credits=bal, reserved_credits=res, available_credits=max(0, bal - res))


def _allocations_summary(allocations: list[dict]) -> dict:
    out = {
        "included": 0,
        "promo": 0,
        "purchased": 0,
        "total": 0,
    }
    for item in allocations:
        bucket = str(item.get("bucket_type") or "").strip().lower()
        amt = _as_int(item.get("amount"), 0)
        if bucket in out:
            out[bucket] += amt
        out["total"] += amt
    return out


async def _reserve_lot_allocations(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    requested_credits: int,
) -> list[dict]:
    lots = await _fetch_active_lots(conn, user_id)
    remaining_need = max(0, int(requested_credits))
    allocations: list[dict] = []
    if remaining_need <= 0:
        return allocations

    for lot in lots:
        if remaining_need <= 0:
            break
        remaining = _as_decimal(lot.get("remaining_amount"), "0")
        reserved = _as_decimal(lot.get("reserved_amount"), "0")
        available = int(max(Decimal("0"), remaining - reserved))
        if available <= 0:
            continue
        take = min(available, remaining_need)
        if take <= 0:
            continue
        await conn.execute(
            """
            update pricing_credit_lots
            set reserved_amount = reserved_amount + $2,
                updated_at = now()
            where id = $1
            """,
            lot["id"],
            Decimal(str(take)),
        )
        allocations.append(
            {
                "lot_id": str(lot["id"]),
                "bucket_type": str(lot.get("bucket_type") or ""),
                "amount": take,
            }
        )
        remaining_need -= take

    if remaining_need > 0:
        raise ValueError("PRICING_INSUFFICIENT_CREDITS")
    return allocations


async def _release_lot_allocations(
    conn: asyncpg.Connection,
    *,
    allocations: list[dict],
) -> None:
    for item in allocations:
        lot_id = _as_uuid_or_none(item.get("lot_id"))
        amt = max(0, _as_int(item.get("amount"), 0))
        if not lot_id or amt <= 0:
            continue
        await conn.execute(
            """
            update pricing_credit_lots
            set reserved_amount = greatest(0, reserved_amount - $2),
                updated_at = now()
            where id = $1
            """,
            lot_id,
            Decimal(str(amt)),
        )


async def _commit_lot_allocations(
    conn: asyncpg.Connection,
    *,
    allocations: list[dict],
    charged_credits: int,
) -> dict:
    remaining_to_commit = max(0, int(charged_credits))
    consumed: list[dict] = []
    released: list[dict] = []

    for item in allocations:
        lot_id = _as_uuid_or_none(item.get("lot_id"))
        bucket_type = str(item.get("bucket_type") or "")
        amt = max(0, _as_int(item.get("amount"), 0))
        if not lot_id or amt <= 0:
            continue

        charge_here = min(amt, remaining_to_commit)
        release_here = max(0, amt - charge_here)

        if charge_here > 0:
            await conn.execute(
                """
                update pricing_credit_lots
                set
                  reserved_amount = greatest(0, reserved_amount - $2),
                  remaining_amount = greatest(0, remaining_amount - $2),
                  updated_at = now()
                where id = $1
                """,
                lot_id,
                Decimal(str(charge_here)),
            )
            consumed.append(
                {
                    "lot_id": str(lot_id),
                    "bucket_type": bucket_type,
                    "amount": charge_here,
                }
            )
            remaining_to_commit -= charge_here

        if release_here > 0:
            await conn.execute(
                """
                update pricing_credit_lots
                set
                  reserved_amount = greatest(0, reserved_amount - $2),
                  updated_at = now()
                where id = $1
                """,
                lot_id,
                Decimal(str(release_here)),
            )
            released.append(
                {
                    "lot_id": str(lot_id),
                    "bucket_type": bucket_type,
                    "amount": release_here,
                }
            )

    if remaining_to_commit > 0:
        raise ValueError("PRICING_INSUFFICIENT_CREDITS_OVERAGE")

    return {
        "consumed": consumed,
        "released": released,
        "consumed_summary": _allocations_summary(consumed),
        "released_summary": _allocations_summary(released),
    }


async def _ledger_event(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    event_type: str,
    credits_delta: int,
    idempotency_key: str,
    sku_code: Optional[str] = None,
    quantity: Optional[Decimal] = None,
    unit_credits: Optional[int] = None,
    country_code: Optional[str] = None,
    currency: Optional[str] = None,
    money_amount: Optional[Decimal] = None,
    channel: Optional[str] = None,
    metadata: Optional[dict] = None,
    billing_account_id: Optional[UUID] = None,
    settlement_mode: Optional[str] = None,
    reservation_id: Optional[UUID] = None,
    studio_job_id: Optional[UUID] = None,
    service_name: Optional[str] = None,
    service_action: Optional[str] = None,
) -> None:
    md = _as_dict_loose(metadata)
    normalized_settlement_mode = _normalize_settlement_mode(settlement_mode) or None
    normalized_country_code = _clean_country_code(country_code)
    normalized_currency = _clean_currency(currency, default="USD")
    normalized_channel = _clean_text(channel, default="service")

    normalized_studio_job_id, md = await _normalize_ledger_target(
        conn,
        studio_job_id=studio_job_id,
        metadata=md,
        service_name=service_name,
        service_action=service_action,
    )

    # First attempt: new schema with billing-account / invoice-ready fields.
    try:
        async with conn.transaction():
            await conn.execute(
                """
                insert into pricing_credit_ledger_events
                  (
                    id, user_id, billing_account_id, settlement_mode, reservation_id, studio_job_id,
                    event_type, credits_delta, sku_code, quantity, unit_credits,
                    idempotency_key, country_code, currency, money_amount, channel,
                    service_name, service_action, metadata_json, created_at
                  )
                values
                  (
                    gen_random_uuid(), $1, $2, $3, $4, $5,
                    $6, $7, $8, $9, $10,
                    $11, $12, $13, $14, $15,
                    $16, $17, $18::jsonb, now()
                  )
                on conflict (user_id, idempotency_key) do nothing
                """,
                user_id,
                billing_account_id,
                normalized_settlement_mode,
                reservation_id,
                normalized_studio_job_id,
                event_type,
                int(credits_delta),
                sku_code,
                quantity,
                unit_credits,
                idempotency_key,
                normalized_country_code,
                normalized_currency,
                money_amount,
                normalized_channel,
                service_name,
                service_action,
                md,
            )
            return
    except Exception:
        pass

    # Fallback: older schema without extended columns.
    async with conn.transaction():
        await conn.execute(
            """
            insert into pricing_credit_ledger_events
              (id, user_id, event_type, credits_delta, sku_code, quantity, unit_credits,
               idempotency_key, country_code, currency, money_amount, channel, metadata_json, created_at)
            values (gen_random_uuid(), $1, $2, $3, $4, $5, $6,
                    $7, $8, $9, $10, $11, $12::jsonb, now())
            on conflict (user_id, idempotency_key) do nothing
            """,
            user_id,
            event_type,
            int(credits_delta),
            sku_code,
            quantity,
            unit_credits,
            idempotency_key,
            normalized_country_code,
            normalized_currency,
            money_amount,
            normalized_channel,
            md,
        )


# -------------------------
# Economics helpers
# -------------------------
async def _usd_to_currency_rate(conn: asyncpg.Connection, currency: str) -> Optional[Decimal]:
    c = (currency or "").upper()
    if c == "USD":
        return Decimal("1")

    fx = await conn.fetchrow(
        """
        select rate
        from pricing_fx_rates
        where base_currency='USD' and quote_currency=$1
        order by as_of desc
        limit 1
        """,
        c,
    )
    if fx and fx.get("rate") is not None:
        return _as_decimal(fx["rate"], "0")

    try:
        usd = await conn.fetchrow(
            """
            select money_per_credit
            from pricing_credit_value
            where currency='USD'
              and effective_from <= now()
              and (effective_to is null or effective_to > now())
            order by effective_from desc limit 1
            """
        )
        tgt = await conn.fetchrow(
            """
            select money_per_credit
            from pricing_credit_value
            where currency=$1
              and effective_from <= now()
              and (effective_to is null or effective_to > now())
            order by effective_from desc limit 1
            """,
            c,
        )
        if usd and tgt:
            usd_mpc = _as_decimal(usd["money_per_credit"], "0")
            tgt_mpc = _as_decimal(tgt["money_per_credit"], "0")
            if usd_mpc > 0:
                return tgt_mpc / usd_mpc
    except Exception:
        return None

    return None


async def _load_cost_components(conn: asyncpg.Connection, sku_codes: list[str]) -> dict:
    if not sku_codes:
        return {}

    rows = await conn.fetch(
        """
        select distinct on (sku_code, component_code)
          sku_code, component_code, cost_model, cost_currency,
          variable_cost_money, fixed_monthly_cost_money, assumed_monthly_units
        from pricing_sku_costs
        where sku_code = any($1::text[])
          and is_active = true
          and effective_from <= now()
          and (effective_to is null or effective_to > now())
        order by sku_code, component_code, effective_from desc
        """,
        sku_codes,
    )

    out: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        out.setdefault(str(d["sku_code"]), []).append(d)
    return out


def _component_unit_cost_usd(comp: dict) -> Optional[Decimal]:
    cur = str(comp.get("cost_currency") or "USD").upper()
    if cur != "USD":
        return None

    model = str(comp.get("cost_model") or "blended").lower()
    var = _as_decimal(comp.get("variable_cost_money"), "0")
    fixed = _as_decimal(comp.get("fixed_monthly_cost_money"), "0")
    units = _as_decimal(comp.get("assumed_monthly_units"), "0")

    amort = Decimal("0")
    if units > 0:
        amort = fixed / units

    if model == "variable":
        return var
    if model == "amortized":
        return amort
    return var + amort


async def _compute_economics_for_quote(
    conn: asyncpg.Connection,
    *,
    currency: str,
    revenue_money_est: Decimal,
    revenue_money_shadow: Decimal,
    quote_lines: list[dict],
) -> dict:
    skus: list[str] = []
    qty_by_sku: dict[str, Decimal] = {}

    for ln in quote_lines:
        sku = str(ln.get("sku_code") or "").strip()
        if not sku:
            continue
        q_raw = ln.get("qty")
        q = q_raw if isinstance(q_raw, Decimal) else _as_decimal(q_raw, "0")
        if q <= 0:
            continue
        skus.append(sku)
        qty_by_sku[sku] = qty_by_sku.get(sku, Decimal("0")) + q

    skus = sorted(set(skus))
    if not skus:
        return {
            "has_costs_complete": False,
            "missing_cost_skus": [],
            "reason": "no_skus_in_quote",
        }

    comps_by_sku = await _load_cost_components(conn, skus)

    missing: list[str] = []
    cogs_usd = Decimal("0")

    for sku in skus:
        comps = comps_by_sku.get(sku) or []
        if not comps:
            missing.append(sku)
            continue

        unit_cost_usd = Decimal("0")
        ok = False
        for comp in comps:
            c = _component_unit_cost_usd(comp)
            if c is None:
                continue
            ok = True
            unit_cost_usd += c

        if not ok:
            missing.append(sku)
            continue

        cogs_usd += unit_cost_usd * qty_by_sku.get(sku, Decimal("0"))

    rate = await _usd_to_currency_rate(conn, currency)
    if rate is None or rate <= 0:
        return {
            "has_costs_complete": False,
            "missing_cost_skus": missing,
            "reason": "missing_fx_rate",
            "cogs_usd_partial": str(_q_money(cogs_usd)),
            "currency": currency,
        }

    cogs_money_est = _q_money(cogs_usd * rate)
    has_complete = len(missing) == 0

    def gm(rev: Decimal) -> tuple[Optional[Decimal], Optional[Decimal]]:
        if not has_complete:
            return None, None
        m = _q_money(rev - cogs_money_est)
        if rev <= 0:
            return m, None
        pct = _q_pct(m / rev)
        return m, pct

    gm_money_est, gm_pct_est = gm(_q_money(revenue_money_est))
    gm_money_shadow, gm_pct_shadow = gm(_q_money(revenue_money_shadow))

    return {
        "currency": currency,
        "has_costs_complete": has_complete,
        "missing_cost_skus": missing,
        "usd_to_currency_rate_used": str(rate),
        "cogs_money_est": str(cogs_money_est) if has_complete else None,
        "revenue_money_est": str(_q_money(revenue_money_est)),
        "gross_margin_money_est": str(gm_money_est) if gm_money_est is not None else None,
        "gross_margin_pct_est": str(gm_pct_est) if gm_pct_est is not None else None,
        "revenue_money_shadow": str(_q_money(revenue_money_shadow)),
        "gross_margin_money_shadow": str(gm_money_shadow) if gm_money_shadow is not None else None,
        "gross_margin_pct_shadow": str(gm_pct_shadow) if gm_pct_shadow is not None else None,
        "cogs_usd_total": str(_q_money(cogs_usd)),
        "computed_at": _now().isoformat(),
        "reason": "ok" if has_complete else "missing_cost_rows",
    }


# -------------------------
# Reserve / Release / Finalize
# -------------------------

async def reserve(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    idempotency_key: str,
    variant_code: str,
    params: Dict[str, Any],
    channel: str,
    country_code: str,
    currency: Optional[str],
    pricing_mode: str,
    billing_mode_snapshot: str,
    job_ref: Optional[str],
    ttl_seconds: Optional[int],
    entitlement_source: Optional[str] = None,
    entitlement_reason: Optional[str] = None,
    tier_code: Optional[str] = None,
    billing_account_id: Optional[UUID] = None,
    settlement_mode: Optional[str] = None,
    service_name: Optional[str] = None,
    service_action: Optional[str] = None,
    sku_code: Optional[str] = None,
) -> ReservationView:
    ttl = ttl_seconds or settings.DEFAULT_RESERVATION_TTL_S
    ttl = max(30, min(ttl, settings.MAX_RESERVATION_TTL_S))
    expires_at = _now() + timedelta(seconds=ttl)

    normalized_channel = _clean_text(channel, default="service")
    normalized_country_code = _clean_country_code(country_code)
    normalized_currency_input = _clean_currency(currency)
    effective_settlement_mode = _normalize_settlement_mode(settlement_mode) or "prepaid"
    normalized_tier_code = _derive_tier_code(
        tier_code,
        billing_account_id=billing_account_id,
        settlement_mode=effective_settlement_mode,
    )
    normalized_entitlement_source = _derive_entitlement_source(
        entitlement_source,
        billing_account_id=billing_account_id,
        settlement_mode=effective_settlement_mode,
    )
    effective_service_name = _clean_text(service_name) or None
    effective_service_action = _clean_text(service_action) or None
    effective_sku_code = _clean_text(sku_code) or variant_code

    existing = await conn.fetchrow(
        """
        select id, status, reserved_credits, expires_at, currency, estimated_money, quote_json
        from pricing_credit_reservations
        where user_id = $1 and idempotency_key = $2
        """,
        user_id,
        idempotency_key,
    )
    if existing:
        q = _as_dict_loose(existing["quote_json"])
        return ReservationView(
            reservation_id=UUID(str(existing["id"])),
            status=str(existing["status"]),
            reserved_credits=int(existing["reserved_credits"] or 0),
            expires_at=existing["expires_at"],
            currency=str(existing["currency"] or q.get("currency") or ""),
            estimated_money=_as_decimal(existing["estimated_money"], "0"),
            quote=q,
        )

    quote: QuoteResult = await quote_variant(
        conn,
        user_id=user_id,
        variant_code=variant_code,
        params=params,
        channel=normalized_channel,
        country_code=normalized_country_code,
        currency=normalized_currency_input or None,
        billing_mode=pricing_mode,
    )

    quoted_credits = int(quote.total_credits)
    est_money = quote.total_money

    max_need = getattr(settings, "MAX_CREDITS_PER_RESERVATION", None)
    if isinstance(max_need, int) and max_need > 0 and quoted_credits > max_need:
        raise ValueError("PRICING_RESERVATION_TOO_LARGE")

    normalized_billing_mode = str((billing_mode_snapshot or pricing_mode or "")).strip().lower()
    hold_applied = bool(
        normalized_billing_mode == "bill"
        and effective_settlement_mode == "prepaid"
        and quoted_credits > 0
    )
    held_credits = quoted_credits if hold_applied else 0
    pbid = _as_uuid_or_none(getattr(quote, "pricebook_id", None))
    effective_currency = _clean_currency(getattr(quote, "currency", None), default=normalized_currency_input or "USD")

    lines_json = [
        {
            "sku_code": l.sku_code,
            "name": l.name,
            "category": l.category,
            "provider_hint": l.provider_hint,
            "unit": l.unit,
            "qty": str(l.qty),
            "unit_credits": l.unit_credits,
            "line_credits": l.line_credits,
            "unit_money": str(l.unit_money) if l.unit_money is not None else None,
            "line_money": str(l.line_money),
        }
        for l in quote.lines
    ]

    quote_json_full: dict = {
        "pricing_engine_version": "2",
        "variant_code": quote.variant_code,
        "sku_code": effective_sku_code,
        "category": quote.category,
        "billing_mode": quote.billing_mode,
        "pricing_mode_used": pricing_mode,
        "billing_mode_snapshot": billing_mode_snapshot or pricing_mode,
        "entitlement_source": normalized_entitlement_source,
        "entitlement_reason": entitlement_reason,
        "tier_code": normalized_tier_code,
        "billing_account_id": str(billing_account_id) if billing_account_id else None,
        "settlement_mode": effective_settlement_mode,
        "service_name": effective_service_name,
        "service_action": effective_service_action,
        "hold_applied": hold_applied,
        "reserved_hold_credits": held_credits,
        "pricebook_id": str(pbid) if pbid else None,
        "pricebook_name": quote.pricebook_name,
        "currency": effective_currency,
        "total_credits": quote.total_credits,
        "total_money": str(quote.total_money),
        "shadow_total_credits": quote.shadow_total_credits,
        "shadow_total_money": str(quote.shadow_total_money) if quote.shadow_total_money is not None else None,
        "rounding_mode": quote.rounding_mode,
        "alt_currency": quote.alt_currency,
        "alt_total_money": str(quote.alt_total_money) if quote.alt_total_money is not None else None,
        "country_code": normalized_country_code,
        "channel": normalized_channel,
        "params": params,
        "lines": lines_json,
    }

    try:
        revenue_est = _as_decimal(str(quote.total_money), "0")
        revenue_shadow = _as_decimal(str(quote.shadow_total_money or quote.total_money), "0")
        econ = await _compute_economics_for_quote(
            conn,
            currency=effective_currency,
            revenue_money_est=revenue_est,
            revenue_money_shadow=revenue_shadow,
            quote_lines=[{"sku_code": l.sku_code, "qty": l.qty} for l in quote.lines],
        )
        quote_json_full["economics"] = econ
    except Exception:
        logger.exception("economics estimate failed; continuing without economics")
        quote_json_full["economics"] = {
            "currency": effective_currency,
            "has_costs_complete": False,
            "missing_cost_skus": [],
            "reason": "economics_estimate_failed",
            "computed_at": _now().isoformat(),
        }

    async with conn.transaction():
        await _ensure_account_row(conn, user_id)
        await _backfill_legacy_lots_if_needed(conn, user_id)

        try:
            await conn.execute(
                """
                update pricing_credit_accounts
                set
                  billing_account_id = coalesce(billing_account_id, $2),
                  settlement_mode = $3,
                  updated_at = now()
                where user_id = $1
                """,
                user_id,
                billing_account_id,
                effective_settlement_mode,
            )
        except Exception:
            pass

        balance_before = await get_balance(conn, user_id)
        allocations: list[dict] = []
        funding_summary = {
            "strategy": "postpaid_authorization" if effective_settlement_mode == "postpaid" else "credit_lots",
            "hold_applied": hold_applied,
            "held_credits": held_credits,
            "allocations": [],
            "allocations_summary": {"included": 0, "promo": 0, "purchased": 0, "total": 0},
            "balance_before": {
                "balance_credits": balance_before.balance_credits,
                "reserved_credits": balance_before.reserved_credits,
                "available_credits": balance_before.available_credits,
            },
        }

        if held_credits > 0:
            allocations = await _reserve_lot_allocations(
                conn,
                user_id=user_id,
                requested_credits=held_credits,
            )
            funding_summary["allocations"] = allocations
            funding_summary["allocations_summary"] = _allocations_summary(allocations)

        balance_after = await get_balance(conn, user_id)
        funding_summary["balance_after"] = {
            "balance_credits": balance_after.balance_credits,
            "reserved_credits": balance_after.reserved_credits,
            "available_credits": balance_after.available_credits,
        }
        quote_json_full["funding_summary"] = funding_summary

        rid = None
        try:
            row = await conn.fetchrow(
                """
                insert into pricing_credit_reservations
                  (
                    id, user_id, billing_account_id, settlement_mode, status,
                    pricebook_id, country_code, currency, channel, tier_code,
                    service_name, service_action, sku_code,
                    quote_json, reserved_credits, estimated_money, idempotency_key, job_ref,
                    allocations_json, funding_summary_json,
                    expires_at, finalized_at, created_at, updated_at
                  )
                values
                  (
                    gen_random_uuid(), $1, $2, $3, 'reserved',
                    $4, $5, $6, $7, $8,
                    $9, $10, $11,
                    $12::jsonb, $13, $14, $15, $16,
                    $17::jsonb, $18::jsonb,
                    $19, null, now(), now()
                  )
                on conflict (user_id, idempotency_key) do nothing
                returning id
                """,
                user_id,
                billing_account_id,
                effective_settlement_mode,
                pbid,
                normalized_country_code,
                effective_currency,
                normalized_channel,
                normalized_tier_code,
                effective_service_name,
                effective_service_action,
                effective_sku_code,
                quote_json_full,
                held_credits,
                est_money,
                idempotency_key,
                job_ref,
                json.dumps({"allocations": allocations}, default=str),
                json.dumps(funding_summary, default=str),
                expires_at,
            )
            rid = row["id"] if row else None
        except Exception:
            row = await conn.fetchrow(
                """
                insert into pricing_credit_reservations
                  (
                    id, user_id, billing_account_id, settlement_mode, status,
                    pricebook_id, country_code, currency, channel, tier_code,
                    service_name, service_action, sku_code,
                    quote_json, reserved_credits, estimated_money, idempotency_key, job_ref,
                    expires_at, finalized_at, created_at, updated_at
                  )
                values
                  (
                    gen_random_uuid(), $1, $2, $3, 'reserved',
                    $4, $5, $6, $7, $8,
                    $9, $10, $11,
                    $12::jsonb, $13, $14, $15, $16,
                    $17, null, now(), now()
                  )
                on conflict (user_id, idempotency_key) do nothing
                returning id
                """,
                user_id,
                billing_account_id,
                effective_settlement_mode,
                pbid,
                normalized_country_code,
                effective_currency,
                normalized_channel,
                normalized_tier_code,
                effective_service_name,
                effective_service_action,
                effective_sku_code,
                quote_json_full,
                held_credits,
                est_money,
                idempotency_key,
                job_ref,
                expires_at,
            )
            rid = row["id"] if row else None

        if not rid:
            existing2 = await conn.fetchrow(
                """
                select id, status, reserved_credits, expires_at, currency, estimated_money, quote_json
                from pricing_credit_reservations
                where user_id = $1 and idempotency_key = $2
                """,
                user_id,
                idempotency_key,
            )
            if not existing2:
                raise RuntimeError("PRICING_RESERVATION_RACE_FETCH_FAILED")
            q2 = _as_dict_loose(existing2["quote_json"])
            return ReservationView(
                reservation_id=UUID(str(existing2["id"])),
                status=str(existing2["status"]),
                reserved_credits=int(existing2["reserved_credits"] or 0),
                expires_at=existing2["expires_at"],
                currency=str(existing2["currency"] or q2.get("currency") or ""),
                estimated_money=_as_decimal(existing2["estimated_money"], "0"),
                quote=q2,
            )

        rid_uuid = UUID(str(rid))
        ledger_sku_code = _ledger_leaf_sku_code_from_quote(quote_json_full)

        await _ledger_event(
            conn,
            user_id=user_id,
            event_type="reserve_hold",
            credits_delta=0,
            idempotency_key=f"reserve_hold:{idempotency_key}",
            sku_code=ledger_sku_code,
            quantity=_as_decimal(params.get("requested_units"), "0"),
            country_code=normalized_country_code,
            currency=effective_currency,
            channel=normalized_channel,
            money_amount=est_money,
            metadata=_ledger_metadata_with_service_job_hints(
                {
                    "reservation_id": str(rid_uuid),
                    "variant_code": variant_code,
                    "requested_code": effective_sku_code,
                    "ledger_sku_code": ledger_sku_code,
                    "billing_mode_snapshot": billing_mode_snapshot or pricing_mode,
                    "settlement_mode": effective_settlement_mode,
                    "hold_applied": hold_applied,
                    "quoted_credits": quoted_credits,
                    "held_credits": held_credits,
                    "balance_before": balance_before.balance_credits,
                    "reserved_before": balance_before.reserved_credits,
                    "available_before": balance_before.available_credits,
                    "balance_after": balance_after.balance_credits,
                    "reserved_after": balance_after.reserved_credits,
                    "available_after": balance_after.available_credits,
                    "funding_summary": funding_summary,
                },
                service_name=effective_service_name,
                service_action=effective_service_action,
                params=params,
                studio_job_id=_as_uuid_or_none(job_ref),
            ),
            billing_account_id=billing_account_id,
            settlement_mode=effective_settlement_mode,
            reservation_id=rid_uuid,
            studio_job_id=_as_uuid_or_none(job_ref),
            service_name=effective_service_name,
            service_action=effective_service_action,
        )

        await _sync_legacy_credit_account_summary(conn, user_id)

    return ReservationView(
        reservation_id=rid_uuid,
        status="reserved",
        reserved_credits=held_credits,
        expires_at=expires_at,
        currency=effective_currency,
        estimated_money=est_money,
        quote=quote_json_full,
    )



async def release(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    reservation_id: Optional[UUID],
    idempotency_key: Optional[str],
    channel: str,
    country_code: str,
    reason: str,
) -> ReservationView:
    if not reservation_id and not idempotency_key:
        raise ValueError("PRICING_RELEASE_REQUIRES_RESERVATION")

    async with conn.transaction():
        await _ensure_account_row(conn, user_id)
        await _backfill_legacy_lots_if_needed(conn, user_id)

        if reservation_id:
            r = await conn.fetchrow(
                """
                select id, status, reserved_credits, expires_at, currency, estimated_money, quote_json, allocations_json
                from pricing_credit_reservations
                where user_id = $1 and id = $2
                for update
                """,
                user_id,
                reservation_id,
            )
        else:
            r = await conn.fetchrow(
                """
                select id, status, reserved_credits, expires_at, currency, estimated_money, quote_json, allocations_json
                from pricing_credit_reservations
                where user_id = $1 and idempotency_key = $2
                for update
                """,
                user_id,
                idempotency_key,
            )
        if not r:
            raise ValueError("PRICING_RESERVATION_NOT_FOUND")

        rid = UUID(str(r["id"]))
        st = str(r["status"])
        row_reserved = int(r["reserved_credits"] or 0)
        quote = _as_dict_loose(r["quote_json"])
        allocations_json = _as_dict_loose(_row_get(r, "allocations_json"))
        allocations = allocations_json.get("allocations") or []
        currency = _clean_currency(r["currency"] or quote.get("currency"), default="USD")
        effective_channel = _clean_text(channel) or _clean_text(quote.get("channel"), default="service")
        effective_country_code = _clean_country_code(country_code) or _clean_country_code(quote.get("country_code"))

        held_effective = _held_credits_from_reservation(row_reserved, quote)
        hold_applied = held_effective > 0

        if st in {"released", "expired", "cancelled", "failed"}:
            return ReservationView(
                reservation_id=rid,
                status=st,
                reserved_credits=0,
                expires_at=r["expires_at"],
                currency=currency,
                estimated_money=_as_decimal(r["estimated_money"], "0"),
                quote=quote,
            )

        if st == "committed":
            raise ValueError("PRICING_RESERVATION_ALREADY_COMMITTED")

        balance_before = await get_balance(conn, user_id)

        if allocations:
            await _release_lot_allocations(conn, allocations=allocations)
            await _sync_legacy_credit_account_summary(conn, user_id)
        elif held_effective > 0:
            acc = await conn.fetchrow(
                "select balance_credits, reserved_credits from pricing_credit_accounts where user_id=$1 for update",
                user_id,
            )
            res = int(acc["reserved_credits"])
            await conn.execute(
                "update pricing_credit_accounts set reserved_credits=$2, updated_at=now() where user_id=$1",
                user_id,
                max(0, res - held_effective),
            )

        balance_after = await get_balance(conn, user_id)

        await conn.execute(
            """
            update pricing_credit_reservations
            set
              status='released',
              finalized_at=coalesce(finalized_at, now()),
              updated_at=now(),
              funding_summary_json = coalesce(funding_summary_json, '{}'::jsonb) || $3::jsonb
            where user_id=$1 and id=$2
            """,
            user_id,
            rid,
            json.dumps(
                {
                    "released_reason": reason,
                    "released_at": _now().isoformat(),
                    "released_summary": _allocations_summary(allocations),
                },
                default=str,
            ),
        )

        ledger_sku_code = _ledger_leaf_sku_code_from_quote(quote)

        await _ledger_event(
            conn,
            user_id=user_id,
            event_type="reserve_release",
            credits_delta=0,
            idempotency_key=f"reserve_release:{rid}",
            sku_code=ledger_sku_code,
            country_code=effective_country_code,
            currency=currency,
            channel=effective_channel,
            metadata=_ledger_metadata_with_service_job_hints(
                {
                    "reservation_id": str(rid),
                    "reason": reason,
                    "requested_code": str(quote.get("sku_code") or quote.get("variant_code") or "") or None,
                    "ledger_sku_code": ledger_sku_code,
                    "settlement_mode": _normalize_settlement_mode(quote.get("settlement_mode")) or "prepaid",
                    "hold_applied": hold_applied,
                    "held_credits": held_effective,
                    "balance_before": balance_before.balance_credits,
                    "reserved_before": balance_before.reserved_credits,
                    "available_before": balance_before.available_credits,
                    "balance_after": balance_after.balance_credits,
                    "reserved_after": balance_after.reserved_credits,
                    "available_after": balance_after.available_credits,
                    "released_allocations": allocations,
                },
                service_name=_clean_text(quote.get("service_name")) or None,
                service_action=_clean_text(quote.get("service_action")) or None,
                params=_as_dict_loose(quote.get("params")),
                studio_job_id=_as_uuid_or_none(_as_dict_loose(quote.get("params")).get("service_job_id")),
            ),
            billing_account_id=_as_uuid_or_none(quote.get("billing_account_id")),
            settlement_mode=_normalize_settlement_mode(quote.get("settlement_mode")) or "prepaid",
            reservation_id=rid,
            studio_job_id=_as_uuid_or_none(_as_dict_loose(quote.get("params")).get("service_job_id")),
            service_name=_clean_text(quote.get("service_name")) or None,
            service_action=_clean_text(quote.get("service_action")) or None,
        )

        return ReservationView(
            reservation_id=rid,
            status="released",
            reserved_credits=0,
            expires_at=r["expires_at"],
            currency=currency,
            estimated_money=_as_decimal(r["estimated_money"], "0"),
            quote=quote,
        )



async def finalize(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    reservation_id: UUID,
    finalize_idempotency_key: str,
    actuals: Dict[str, Any],
    channel: str,
    country_code: str,
    billing_mode: str,
    billing_account_id: Optional[UUID] = None,
    settlement_mode: Optional[str] = None,
    service_name: Optional[str] = None,
    service_action: Optional[str] = None,
    sku_code: Optional[str] = None,
    studio_job_id: Optional[UUID] = None,
) -> FinalizeReceipt:
    async with conn.transaction():
        await _ensure_account_row(conn, user_id)
        await _backfill_legacy_lots_if_needed(conn, user_id)

        try:
            r = await conn.fetchrow(
                """
                select
                  id, status, reserved_credits, currency, estimated_money, quote_json,
                  billing_account_id, settlement_mode, service_name, service_action, sku_code,
                  allocations_json, funding_summary_json
                from pricing_credit_reservations
                where user_id=$1 and id=$2
                for update
                """,
                user_id,
                reservation_id,
            )
        except Exception:
            r = await conn.fetchrow(
                """
                select id, status, reserved_credits, currency, estimated_money, quote_json, allocations_json
                from pricing_credit_reservations
                where user_id=$1 and id=$2
                for update
                """,
                user_id,
                reservation_id,
            )
        if not r:
            raise ValueError("PRICING_RESERVATION_NOT_FOUND")

        st = str(r["status"])
        row_reserved = int(r["reserved_credits"] or 0)
        quote = _as_dict_loose(r["quote_json"])
        allocations_json = _as_dict_loose(_row_get(r, "allocations_json"))
        funding_summary_json = _as_dict_loose(_row_get(r, "funding_summary_json"))
        allocations = allocations_json.get("allocations") or []
        currency = _clean_currency(r["currency"] or quote.get("currency"), default="USD")
        effective_channel = _clean_text(channel) or _clean_text(quote.get("channel"), default="service")
        effective_country_code = _clean_country_code(country_code) or _clean_country_code(quote.get("country_code"))

        effective_billing_account_id = (
            _as_uuid_or_none(_row_get(r, "billing_account_id"))
            or _as_uuid_or_none(quote.get("billing_account_id"))
            or billing_account_id
        )
        effective_settlement_mode = (
            _normalize_settlement_mode(_row_get(r, "settlement_mode"))
            or _normalize_settlement_mode(quote.get("settlement_mode"))
            or _normalize_settlement_mode(settlement_mode)
            or "prepaid"
        )
        effective_service_name = (
            _clean_text(_row_get(r, "service_name"))
            or _clean_text(quote.get("service_name"))
            or _clean_text(service_name)
            or None
        )
        effective_service_action = (
            _clean_text(_row_get(r, "service_action"))
            or _clean_text(quote.get("service_action"))
            or _clean_text(service_action)
            or None
        )
        effective_sku_code = (
            _clean_text(_row_get(r, "sku_code"))
            or _clean_text(quote.get("sku_code") or quote.get("variant_code"))
            or _clean_text(sku_code)
            or None
        )
        effective_job_id = (
            studio_job_id
            or _as_uuid_or_none(_as_dict_loose(quote.get("params")).get("service_job_id"))
            or _as_uuid_or_none(_as_dict_loose(quote.get("params")).get("longform_job_id"))
            or _as_uuid_or_none(_as_dict_loose(quote.get("params")).get("external_ref_id"))
            or _as_uuid_or_none(actuals.get("service_job_id"))
            or _as_uuid_or_none(actuals.get("external_ref_id"))
        )

        balance_before = await get_balance(conn, user_id)
        bal_before = balance_before.balance_credits
        res_before = balance_before.reserved_credits

        if st == "committed":
            avail_after = max(0, bal_before - res_before)
            return FinalizeReceipt(
                reservation_id=reservation_id,
                status="committed",
                charged_credits=_as_int(quote.get("final_charged_credits"), 0),
                charged_money=_as_decimal(quote.get("final_charged_money"), "0"),
                balance_before=bal_before,
                reserved_before=res_before,
                balance_after=bal_before,
                reserved_after=res_before,
                available_after=avail_after,
            )

        if st != "reserved":
            raise ValueError(f"PRICING_INVALID_RESERVATION_STATUS:{st}")

        snapshot_mode = str(quote.get("billing_mode_snapshot") or quote.get("billing_mode") or "").strip()
        effective_billing_mode = snapshot_mode if snapshot_mode else str(billing_mode or "bill")

        held_effective = _held_credits_from_reservation(row_reserved, quote)
        hold_applied = held_effective > 0

        quoted_credits = _as_int(quote.get("total_credits"), 0)
        quoted_money = _as_decimal(quote.get("total_money"), "0")

        if effective_billing_mode in {"shadow", "free", "disabled", "included"}:
            final_credits = 0
            final_money = Decimal("0")
        elif effective_billing_mode == "bill":
            if effective_settlement_mode == "postpaid":
                final_credits = 0
                final_money = quoted_money
            else:
                final_credits = quoted_credits
                final_money = quoted_money
        else:
            final_credits = quoted_credits
            final_money = quoted_money

        if effective_settlement_mode != "postpaid" and final_credits > balance_before.available_credits + held_effective:
            raise ValueError("PRICING_INSUFFICIENT_CREDITS_OVERAGE")

        commit_result = {"consumed": [], "released": [], "consumed_summary": {"included": 0, "promo": 0, "purchased": 0, "total": 0}, "released_summary": {"included": 0, "promo": 0, "purchased": 0, "total": 0}}
        if allocations:
            commit_result = await _commit_lot_allocations(
                conn,
                allocations=allocations,
                charged_credits=final_credits,
            )
            await _sync_legacy_credit_account_summary(conn, user_id)
        elif held_effective > 0:
            # Legacy fallback for pre-lots reservations.
            acc = await conn.fetchrow(
                "select balance_credits, reserved_credits from pricing_credit_accounts where user_id=$1 for update",
                user_id,
            )
            legacy_balance = int(acc["balance_credits"])
            legacy_reserved = int(acc["reserved_credits"])
            new_reserved = max(0, legacy_reserved - held_effective)
            new_balance = legacy_balance - final_credits
            if new_balance < 0:
                raise ValueError("PRICING_NEGATIVE_BALANCE_GUARD")
            await conn.execute(
                """
                update pricing_credit_accounts
                set balance_credits=$2, reserved_credits=$3, updated_at=now()
                where user_id=$1
                """,
                user_id,
                new_balance,
                new_reserved,
            )

        balance_after = await get_balance(conn, user_id)
        new_balance = balance_after.balance_credits
        new_reserved = balance_after.reserved_credits

        try:
            econ = _as_dict_loose(quote.get("economics"))
            if econ:
                econ["revenue_money_final"] = str(_q_money(final_money))
                q_lines = []
                for ln in (quote.get("lines") or []):
                    d = _as_dict_loose(ln)
                    q_lines.append({"sku_code": d.get("sku_code"), "qty": d.get("qty")})
                econ2 = await _compute_economics_for_quote(
                    conn,
                    currency=currency,
                    revenue_money_est=_as_decimal(econ.get("revenue_money_est"), "0"),
                    revenue_money_shadow=_as_decimal(econ.get("revenue_money_shadow"), "0"),
                    quote_lines=q_lines,
                )
                if econ2.get("has_costs_complete"):
                    cogs_est = _as_decimal(econ2.get("cogs_money_est"), "0")
                    econ["cogs_money_final"] = str(cogs_est)
                    econ["gross_margin_money_final"] = str(_q_money(final_money - cogs_est))
                    econ["gross_margin_pct_final"] = (
                        str(_q_pct((_q_money(final_money - cogs_est) / _q_money(final_money))))
                        if final_money > 0
                        else None
                    )
                else:
                    econ["cogs_money_final"] = None
                    econ["gross_margin_money_final"] = None
                    econ["gross_margin_pct_final"] = None
                    econ["has_costs_complete"] = False
                    econ["missing_cost_skus"] = econ2.get("missing_cost_skus") or econ.get("missing_cost_skus") or []
                    econ["reason"] = econ2.get("reason") or econ.get("reason") or "missing_cost_rows"
                econ["computed_at_final"] = _now().isoformat()
                quote["economics"] = econ
        except Exception:
            logger.exception("economics finalize failed; continuing without economics final fields")

        quote["finalize"] = {
            "finalize_idempotency_key": finalize_idempotency_key,
            "billing_mode_effective": effective_billing_mode,
            "settlement_mode_effective": effective_settlement_mode,
            "billing_mode_param": billing_mode,
            "billing_mode_snapshot": snapshot_mode,
            "actuals": actuals,
            "final_charged_credits": final_credits,
            "final_charged_money": str(final_money),
            "held_credits_row": row_reserved,
            "held_credits_effective": held_effective,
            "hold_applied": hold_applied,
            "timestamp": _now().isoformat(),
            "funding_summary_before": funding_summary_json,
            "funding_summary_final": {
                "consumed": commit_result.get("consumed"),
                "released": commit_result.get("released"),
                "consumed_summary": commit_result.get("consumed_summary"),
                "released_summary": commit_result.get("released_summary"),
            },
        }
        quote["final_charged_credits"] = final_credits
        quote["final_charged_money"] = str(final_money)
        quote["reserved_hold_credits"] = max(0, row_reserved - final_credits if hold_applied else 0)
        quote["billing_account_id"] = (
            str(effective_billing_account_id) if effective_billing_account_id else quote.get("billing_account_id")
        )
        quote["settlement_mode"] = effective_settlement_mode
        quote["currency"] = currency
        quote["country_code"] = effective_country_code
        quote["channel"] = effective_channel
        if effective_service_name:
            quote["service_name"] = effective_service_name
        if effective_service_action:
            quote["service_action"] = effective_service_action
        if effective_sku_code:
            quote["sku_code"] = effective_sku_code

        try:
            await conn.execute(
                """
                update pricing_credit_reservations
                set
                  status='committed',
                  finalized_at=now(),
                  updated_at=now(),
                  billing_account_id=coalesce(billing_account_id, $3),
                  settlement_mode=$4,
                  service_name=coalesce(service_name, $5),
                  service_action=coalesce(service_action, $6),
                  sku_code=coalesce(sku_code, $7),
                  quote_json=$8::jsonb,
                  allocations_json=$9::jsonb,
                  funding_summary_json=$10::jsonb
                where user_id=$1 and id=$2
                """,
                user_id,
                reservation_id,
                effective_billing_account_id,
                effective_settlement_mode,
                effective_service_name,
                effective_service_action,
                effective_sku_code,
                quote,
                json.dumps({"allocations": allocations}, default=str),
                json.dumps(
                    {
                        **funding_summary_json,
                        "committed_at": _now().isoformat(),
                        "consumed": commit_result.get("consumed"),
                        "released": commit_result.get("released"),
                        "consumed_summary": commit_result.get("consumed_summary"),
                        "released_summary": commit_result.get("released_summary"),
                    },
                    default=str,
                ),
            )
        except Exception:
            await conn.execute(
                """
                update pricing_credit_reservations
                set status='committed', finalized_at=now(), updated_at=now(), quote_json=$3::jsonb
                where user_id=$1 and id=$2
                """,
                user_id,
                reservation_id,
                quote,
            )

        ledger_sku_code = _ledger_leaf_sku_code_from_quote(quote)

        await _ledger_event(
            conn,
            user_id=user_id,
            event_type="consume",
            credits_delta=-final_credits,
            idempotency_key=f"consume:{finalize_idempotency_key}",
            sku_code=ledger_sku_code,
            quantity=_as_decimal(actuals.get("actual_units"), "0"),
            country_code=effective_country_code,
            currency=currency,
            channel=effective_channel,
            money_amount=final_money,
            metadata=_ledger_metadata_with_service_job_hints(
                {
                    "reservation_id": str(reservation_id),
                    "variant_code": quote.get("variant_code"),
                    "requested_code": effective_sku_code,
                    "ledger_sku_code": ledger_sku_code,
                    "billing_mode_effective": effective_billing_mode,
                    "settlement_mode_effective": effective_settlement_mode,
                    "hold_applied": hold_applied,
                    "held_credits": held_effective,
                    "charged_credits": final_credits,
                    "balance_before": bal_before,
                    "reserved_before": res_before,
                    "balance_after": new_balance,
                    "reserved_after": new_reserved,
                    "funding_summary_final": commit_result,
                },
                service_name=effective_service_name,
                service_action=effective_service_action,
                params=_as_dict_loose(quote.get("params")),
                studio_job_id=effective_job_id,
            ),
            billing_account_id=effective_billing_account_id,
            settlement_mode=effective_settlement_mode,
            reservation_id=reservation_id,
            studio_job_id=effective_job_id,
            service_name=effective_service_name,
            service_action=effective_service_action,
        )

        await _ledger_event(
            conn,
            user_id=user_id,
            event_type="reserve_release",
            credits_delta=0,
            idempotency_key=f"reserve_release_finalize:{finalize_idempotency_key}",
            sku_code=ledger_sku_code,
            country_code=effective_country_code,
            currency=currency,
            channel=effective_channel,
            metadata=_ledger_metadata_with_service_job_hints(
                {
                    "reservation_id": str(reservation_id),
                    "reason": "finalize",
                    "requested_code": effective_sku_code,
                    "ledger_sku_code": ledger_sku_code,
                    "settlement_mode_effective": effective_settlement_mode,
                    "hold_applied": hold_applied,
                    "held_credits": held_effective,
                    "balance_before": bal_before,
                    "reserved_before": res_before,
                    "balance_after": new_balance,
                    "reserved_after": new_reserved,
                    "released_summary": commit_result.get("released_summary"),
                },
                service_name=effective_service_name,
                service_action=effective_service_action,
                params=_as_dict_loose(quote.get("params")),
                studio_job_id=effective_job_id,
            ),
            billing_account_id=effective_billing_account_id,
            settlement_mode=effective_settlement_mode,
            reservation_id=reservation_id,
            studio_job_id=effective_job_id,
            service_name=effective_service_name,
            service_action=effective_service_action,
        )

        avail_after = max(0, new_balance - new_reserved)

        return FinalizeReceipt(
            reservation_id=reservation_id,
            status="committed",
            charged_credits=final_credits,
            charged_money=final_money,
            balance_before=bal_before,
            reserved_before=res_before,
            balance_after=new_balance,
            reserved_after=new_reserved,
            available_after=avail_after,
        )

