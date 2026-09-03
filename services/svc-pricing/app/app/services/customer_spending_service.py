from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID


def _num(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def _int(value: Any) -> int:
    try:
        return int(round(float(value or 0)))
    except Exception:
        return 0


def _dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value) if value is not None else {}
    except Exception:
        return {}


def _text(value: Any) -> str:
    return str(value or "").strip()


async def _table_exists(conn, name: str) -> bool:
    try:
        return bool(await conn.fetchval("select to_regclass($1)::text", name))
    except Exception:
        return False


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    compare_start: datetime
    compare_end: datetime
    grain: str


def _month_start(value: datetime) -> datetime:
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 + months
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def resolve_window(period: str, now: Optional[datetime] = None) -> Window:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    p = _text(period).lower() or "month"
    if p == "month":
        start = _month_start(now)
        end = _add_months(start, 1)
        compare_start = _add_months(start, -1)
        compare_end = start
        return Window(start, end, compare_start, compare_end, "day")
    if p == "quarter":
        q_month = ((now.month - 1) // 3) * 3 + 1
        start = now.replace(month=q_month, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = _add_months(start, 3)
        compare_start = _add_months(start, -3)
        compare_end = start
        return Window(start, end, compare_start, compare_end, "week")
    if p in {"year", "yoy"}:
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1)
        compare_start = start.replace(year=start.year - 1)
        compare_end = start
        return Window(start, end, compare_start, compare_end, "month")
    raise ValueError("period must be one of: month, quarter, year, yoy")


def _category(*, sku_category: Any, sku_code: Any, service_name: Any, service_action: Any, metadata: Any) -> str:
    cat = _text(sku_category).lower()
    sku = _text(sku_code).lower()
    service = _text(service_name).lower()
    action = _text(service_action).lower()
    md = _dict(metadata)
    blob = " ".join(
        _text(v).lower()
        for v in (
            sku,
            service,
            action,
            md.get("studio"),
            md.get("workflow_type"),
            md.get("source_surface"),
            md.get("longform_profile"),
            md.get("scenario_name"),
        )
        if _text(v)
    )
    if any(x in blob for x in ("story", "multi_person", "multi-person", "director")):
        return "Story & Multi-person"
    if cat == "face" or "face" in blob:
        return "Face"
    if cat == "audio" or any(x in blob for x in ("audio", "voice", "tts")):
        return "Voice"
    if cat in {"fusion", "video"} or any(x in blob for x in ("fusion", "video", "talking_video", "longform")):
        return "Video"
    if cat == "music" or "music" in blob:
        return "Music"
    if cat == "api" or "developer" in blob:
        return "API"
    if cat == "commerce" or "commerce" in blob:
        return "Commerce"
    return "Other"


def _pct_delta(current: Decimal, previous: Decimal) -> Optional[float]:
    if previous == 0:
        return None if current == 0 else 100.0
    return round(float(((current - previous) / abs(previous)) * 100), 1)


async def _ledger_rows(conn, user_id: UUID, start: datetime, end: datetime) -> List[Any]:
    if not await _table_exists(conn, "public.pricing_credit_ledger_events"):
        return []
    try:
        return list(await conn.fetch(
            """
            select le.id, le.event_type, le.credits_delta, le.sku_code, le.quantity,
                   le.currency, le.money_amount, le.channel, le.metadata_json,
                   le.created_at, le.service_name, le.service_action, le.studio_job_id,
                   coalesce(ps.category, '') as sku_category
            from public.pricing_credit_ledger_events le
            left join public.pricing_skus ps on ps.code = le.sku_code
            where le.user_id = $1 and le.created_at >= $2 and le.created_at < $3
            order by le.created_at desc
            """,
            user_id, start, end,
        ))
    except Exception:
        return list(await conn.fetch(
            """
            select le.id, le.event_type, le.credits_delta, le.sku_code, le.quantity,
                   le.currency, le.money_amount, le.channel, le.metadata_json,
                   le.created_at,
                   null::text as service_name, null::text as service_action,
                   null::uuid as studio_job_id, coalesce(ps.category, '') as sku_category
            from public.pricing_credit_ledger_events le
            left join public.pricing_skus ps on ps.code = le.sku_code
            where le.user_id = $1 and le.created_at >= $2 and le.created_at < $3
            order by le.created_at desc
            """,
            user_id, start, end,
        ))


async def _wallet_orders(conn, user_id: UUID, start: datetime, end: datetime) -> List[Any]:
    if not await _table_exists(conn, "public.payment_wallet_orders"):
        return []
    return list(await conn.fetch(
        """
        select id, currency, amount_minor, credits_to_grant, gateway_provider,
               payment_state, fulfillment_state, created_at, fulfilled_at, metadata_json
        from public.payment_wallet_orders
        where user_id = $1 and created_at >= $2 and created_at < $3
        order by created_at desc
        """,
        user_id, start, end,
    ))


async def _completed_plan_checkouts(conn, user_id: UUID, start: datetime, end: datetime) -> List[Any]:
    if not await _table_exists(conn, "public.payment_gateway_checkout_sessions"):
        return []
    return list(await conn.fetch(
        """
        select id, gateway_provider, purpose, currency, amount_minor, status,
               completed_at, created_at, local_subscription_id, metadata_json
        from public.payment_gateway_checkout_sessions
        where user_id = $1
          and purpose in ('plan_subscription','plan_upgrade','plan_downgrade','invoice_pay')
          and status = 'complete'
          and coalesce(completed_at, created_at) >= $2
          and coalesce(completed_at, created_at) < $3
        order by coalesce(completed_at, created_at) desc
        """,
        user_id, start, end,
    ))


async def _paid_invoices(conn, user_id: UUID, start: datetime, end: datetime) -> List[Any]:
    if not (await _table_exists(conn, "public.pricing_invoices") and await _table_exists(conn, "public.pricing_billing_account_members")):
        return []
    return list(await conn.fetch(
        """
        select i.id, i.invoice_number, i.currency, i.total_money, i.paid_at, i.status
        from public.pricing_invoices i
        join public.pricing_billing_account_members m on m.billing_account_id = i.billing_account_id
        where m.user_id = $1 and m.status = 'active' and i.status = 'paid'
          and i.paid_at >= $2 and i.paid_at < $3
        order by i.paid_at desc
        """,
        user_id, start, end,
    ))


async def _balance(conn, user_id: UUID) -> Dict[str, int]:
    if not await _table_exists(conn, "public.pricing_credit_accounts"):
        return {"available": 0, "reserved": 0}
    row = await conn.fetchrow(
        "select balance_credits, reserved_credits from public.pricing_credit_accounts where user_id=$1",
        user_id,
    )
    if not row:
        return {"available": 0, "reserved": 0}
    balance = _int(row["balance_credits"])
    reserved = _int(row["reserved_credits"])
    return {"available": max(balance - reserved, 0), "reserved": max(reserved, 0)}


def _usage_totals(rows: Sequence[Any]) -> Tuple[Decimal, Decimal, Dict[str, Decimal], Dict[str, int]]:
    consumed = Decimal("0")
    refunded = Decimal("0")
    by_category: Dict[str, Decimal] = {}
    jobs_by_category: Dict[str, set] = {}
    for row in rows:
        event = _text(row["event_type"]).lower()
        delta = _num(row["credits_delta"])
        if event == "consume" and delta < 0:
            credits = abs(delta)
            consumed += credits
            category = _category(
                sku_category=row["sku_category"], sku_code=row["sku_code"],
                service_name=row["service_name"], service_action=row["service_action"],
                metadata=row["metadata_json"],
            )
            by_category[category] = by_category.get(category, Decimal("0")) + credits
            jobs_by_category.setdefault(category, set()).add(_text(row["studio_job_id"] or row["id"]))
        elif delta > 0 and ("refund" in event or "reversal" in event):
            refunded += delta
    return consumed, refunded, by_category, {k: len(v) for k, v in jobs_by_category.items()}


def _money_totals(wallet_rows: Sequence[Any], checkout_rows: Sequence[Any], invoice_rows: Sequence[Any]) -> Tuple[Decimal, Dict[str, Decimal], str]:
    total = Decimal("0")
    parts = {"credit_purchases": Decimal("0"), "subscriptions": Decimal("0"), "invoices": Decimal("0"), "refunds": Decimal("0")}
    currencies: List[str] = []
    for row in wallet_rows:
        currency = _text(row["currency"]).upper() or "USD"
        currencies.append(currency)
        amount = _num(row["amount_minor"]) / Decimal("100")
        state = _text(row["payment_state"]).lower()
        if state == "succeeded":
            parts["credit_purchases"] += amount
            total += amount
        elif state == "refunded":
            parts["refunds"] += amount
            total -= amount
    for row in checkout_rows:
        currency = _text(row["currency"]).upper() or "USD"
        currencies.append(currency)
        amount = _num(row["amount_minor"]) / Decimal("100")
        purpose = _text(row["purpose"]).lower()
        if purpose in {"plan_subscription", "plan_upgrade", "plan_downgrade"}:
            parts["subscriptions"] += amount
        else:
            parts["invoices"] += amount
        total += amount
    for row in invoice_rows:
        currency = _text(row["currency"]).upper() or "USD"
        currencies.append(currency)
        amount = _num(row["total_money"])
        parts["invoices"] += amount
        total += amount
    currency = currencies[0] if currencies and len(set(currencies)) == 1 else ("MIXED" if currencies else "USD")
    return total, parts, currency


def _bucket_key(dt: datetime, grain: str) -> str:
    if grain == "day":
        return dt.strftime("%Y-%m-%d")
    if grain == "week":
        y, w, _ = dt.isocalendar()
        return f"{y}-W{w:02d}"
    return dt.strftime("%Y-%m")


def _trend(rows: Sequence[Any], wallet_rows: Sequence[Any], checkout_rows: Sequence[Any], invoice_rows: Sequence[Any], grain: str) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Decimal]] = {}
    def add(key: str, field: str, value: Decimal) -> None:
        buckets.setdefault(key, {"credits_consumed": Decimal("0"), "money_paid": Decimal("0")})[field] += value
    for row in rows:
        if _text(row["event_type"]).lower() == "consume" and _num(row["credits_delta"]) < 0:
            add(_bucket_key(row["created_at"], grain), "credits_consumed", abs(_num(row["credits_delta"])))
    for row in wallet_rows:
        if _text(row["payment_state"]).lower() == "succeeded":
            add(_bucket_key(row["fulfilled_at"] or row["created_at"], grain), "money_paid", _num(row["amount_minor"]) / Decimal("100"))
    for row in checkout_rows:
        add(_bucket_key(row["completed_at"] or row["created_at"], grain), "money_paid", _num(row["amount_minor"]) / Decimal("100"))
    for row in invoice_rows:
        add(_bucket_key(row["paid_at"], grain), "money_paid", _num(row["total_money"]))
    return [
        {"bucket": key, "credits_consumed": _int(vals["credits_consumed"]), "money_paid": _money(vals["money_paid"])}
        for key, vals in sorted(buckets.items())
    ]


async def spending_summary(conn, *, user_id: UUID, period: str = "month") -> Dict[str, Any]:
    window = resolve_window(period)
    rows = await _ledger_rows(conn, user_id, window.start, window.end)
    prev_rows = await _ledger_rows(conn, user_id, window.compare_start, window.compare_end)
    wallet = await _wallet_orders(conn, user_id, window.start, window.end)
    prev_wallet = await _wallet_orders(conn, user_id, window.compare_start, window.compare_end)
    checkouts = await _completed_plan_checkouts(conn, user_id, window.start, window.end)
    prev_checkouts = await _completed_plan_checkouts(conn, user_id, window.compare_start, window.compare_end)
    invoices = await _paid_invoices(conn, user_id, window.start, window.end)
    prev_invoices = await _paid_invoices(conn, user_id, window.compare_start, window.compare_end)

    consumed, refunded, by_category, job_counts = _usage_totals(rows)
    prev_consumed, _, _, _ = _usage_totals(prev_rows)
    money_paid, money_parts, currency = _money_totals(wallet, checkouts, invoices)
    prev_money_paid, _, _ = _money_totals(prev_wallet, prev_checkouts, prev_invoices)
    purchased_credits = sum(_num(r["credits_to_grant"]) for r in wallet if _text(r["payment_state"]).lower() == "succeeded")
    balance = await _balance(conn, user_id)

    category_total = sum(by_category.values(), Decimal("0"))
    categories = [
        {
            "category": category,
            "credits": _int(credits),
            "percent": round(float((credits / category_total) * 100), 1) if category_total else 0.0,
            "transactions": int(job_counts.get(category, 0)),
        }
        for category, credits in sorted(by_category.items(), key=lambda item: item[1], reverse=True)
    ]

    return {
        "period": _text(period).lower() or "month",
        "window": {
            "start": window.start.isoformat(), "end": window.end.isoformat(),
            "compare_start": window.compare_start.isoformat(), "compare_end": window.compare_end.isoformat(),
        },
        "credits": {
            "consumed": _int(consumed),
            "refunded": _int(refunded),
            "purchased": _int(purchased_credits),
            "available": balance["available"],
            "reserved": balance["reserved"],
        },
        "money": {
            "paid": _money(money_paid),
            "currency": currency,
            "credit_purchases": _money(money_parts["credit_purchases"]),
            "subscriptions": _money(money_parts["subscriptions"]),
            "invoices": _money(money_parts["invoices"]),
            "refunds": _money(money_parts["refunds"]),
            "note": "Money paid is based on locally recorded completed payments and paid invoices. Credit consumption is reported separately and is not treated as cash paid.",
        },
        "comparison": {
            "credits_consumed_previous": _int(prev_consumed),
            "credits_consumed_delta_percent": _pct_delta(consumed, prev_consumed),
            "money_paid_previous": _money(prev_money_paid),
            "money_paid_delta_percent": _pct_delta(money_paid, prev_money_paid),
        },
        "categories": categories,
        "top_category": categories[0] if categories else None,
        "trend": _trend(rows, wallet, checkouts, invoices, window.grain),
        "source": "pricing_credit_ledger_events+payment_wallet_orders+payment_gateway_checkout_sessions+pricing_invoices",
    }


async def transaction_history(
    conn,
    *,
    user_id: UUID,
    period: str = "year",
    kind: str = "all",
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    window = resolve_window(period)
    ledger = await _ledger_rows(conn, user_id, window.start, window.end)
    wallet = await _wallet_orders(conn, user_id, window.start, window.end)
    checkouts = await _completed_plan_checkouts(conn, user_id, window.start, window.end)
    invoices = await _paid_invoices(conn, user_id, window.start, window.end)
    items: List[Dict[str, Any]] = []

    for row in ledger:
        event = _text(row["event_type"]).lower()
        delta = _num(row["credits_delta"])
        if event == "consume" and delta < 0:
            category = _category(
                sku_category=row["sku_category"], sku_code=row["sku_code"], service_name=row["service_name"],
                service_action=row["service_action"], metadata=row["metadata_json"],
            )
            items.append({
                "id": str(row["id"]), "occurred_at": row["created_at"].isoformat(),
                "type": "usage", "category": category, "label": f"{category} usage",
                "credits": -_int(abs(delta)), "money": None, "currency": _text(row["currency"]).upper() or None,
                "status": "completed", "channel": _text(row["channel"]) or None,
                "sku_code": _text(row["sku_code"]) or None,
            })
        elif delta > 0 and ("refund" in event or "reversal" in event):
            items.append({
                "id": str(row["id"]), "occurred_at": row["created_at"].isoformat(),
                "type": "refund", "category": "Refund", "label": "Credit refund",
                "credits": _int(delta), "money": None, "currency": _text(row["currency"]).upper() or None,
                "status": "completed", "channel": _text(row["channel"]) or None,
            })

    for row in wallet:
        state = _text(row["payment_state"]).lower()
        tx_type = "refund" if state == "refunded" else "purchase"
        items.append({
            "id": str(row["id"]), "occurred_at": (row["fulfilled_at"] or row["created_at"]).isoformat(),
            "type": tx_type, "category": "Credit purchase", "label": "Credit top-up",
            "credits": _int(row["credits_to_grant"]) if tx_type == "purchase" else -_int(row["credits_to_grant"]),
            "money": _money(_num(row["amount_minor"]) / Decimal("100")),
            "currency": _text(row["currency"]).upper() or "USD", "status": state,
            "channel": "mobile" if _text(row["gateway_provider"]).lower() in {"apple_iap", "google_play"} else "web",
            "provider": _text(row["gateway_provider"]) or None,
        })

    for row in checkouts:
        items.append({
            "id": str(row["id"]), "occurred_at": (row["completed_at"] or row["created_at"]).isoformat(),
            "type": "subscription", "category": "Subscription", "label": _text(row["purpose"]).replace("_", " ").title(),
            "credits": None, "money": _money(_num(row["amount_minor"]) / Decimal("100")),
            "currency": _text(row["currency"]).upper() or "USD", "status": "paid",
            "channel": "web", "provider": _text(row["gateway_provider"]) or None,
        })

    for row in invoices:
        items.append({
            "id": str(row["id"]), "occurred_at": row["paid_at"].isoformat(),
            "type": "invoice", "category": "Invoice", "label": f"Invoice {row['invoice_number'] or ''}".strip(),
            "credits": None, "money": _money(_num(row["total_money"])), "currency": _text(row["currency"]).upper() or "USD",
            "status": "paid", "channel": "billing",
        })

    requested = _text(kind).lower() or "all"
    if requested != "all":
        items = [item for item in items if item["type"] == requested]
    items.sort(key=lambda item: item["occurred_at"], reverse=True)
    total = len(items)
    page = items[max(0, offset): max(0, offset) + max(1, min(limit, 200))]
    return {
        "period": _text(period).lower() or "year",
        "filter": requested,
        "total": total,
        "limit": max(1, min(limit, 200)),
        "offset": max(0, offset),
        "items": page,
        "source": "canonical_pricing_and_payment_records",
    }
