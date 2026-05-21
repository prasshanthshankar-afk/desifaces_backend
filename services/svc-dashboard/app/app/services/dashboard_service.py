import json
import math
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from app.settings import settings
from app.services.blob_sas_service import AzureBlobSasSigner, split_container_blob_from_url


def _coerce_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _as_number(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except Exception:
        return None


def _as_dict_deep_loose(v: Any) -> Dict[str, Any]:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, (list, tuple)):
        merged: Dict[str, Any] = {}
        for item in v:
            item_dict = _as_dict_deep_loose(item)
            if item_dict:
                merged.update(item_dict)
        return merged
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return {}
        try:
            return _as_dict_deep_loose(json.loads(s))
        except Exception:
            return {}
    try:
        return dict(v)
    except Exception:
        return {}


def _record_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row.get(key)
        return default if value is None else value
    except Exception:
        pass
    try:
        value = row[key]
        return default if value is None else value
    except Exception:
        return default


def _to_int_credits(value: Any, default: int = 0) -> int:
    n = _as_number(value)
    if n is None:
        return default
    return max(int(round(n)), 0)


def _first_number(*values: Any) -> Optional[float]:
    for value in values:
        n = _as_number(value)
        if n is not None:
            return n
    return None


def _credit_label(value: Any) -> str:
    return f"{_to_int_credits(value, 0)} credits"


def _included_label(available: int, total: int) -> str:
    return f"{max(int(available), 0)} / {max(int(total), 0)} credits"


def _cycle_usage_label(used: int, total: int) -> str:
    if total > 0:
        return f"{max(int(used), 0)} / {int(total)} credits used"
    return f"{max(int(used), 0)} credits used"


def _public_plan_name(plan_code: Any, tier_code: Any = None) -> str:
    code = _clean_text(plan_code).lower()
    if code in {"free", ""}:
        return "Free"
    if code in {"pro", "pro_monthly", "pro_monthly_v1"}:
        return "Pro"
    if code == "pro_yearly_v1":
        return "Pro Yearly"
    if code in {"business", "business_monthly", "business_monthly_v1"}:
        return "Business"
    if code == "business_yearly_v1":
        return "Business Yearly"
    if code.startswith("enterprise"):
        return "Enterprise"
    tier = _clean_text(tier_code).lower()
    if tier == "enterprise":
        return "Enterprise"
    if tier == "business":
        return "Business"
    if tier == "pro":
        return "Pro"
    return _humanize_code(code) if code else "Free"


def _canonical_credit_contract(
    *,
    resp: Dict[str, Any],
    ent_row: Optional[asyncpg.Record],
    pricing_account_row: Optional[asyncpg.Record],
    pricing_account_overview_row: Optional[asyncpg.Record],
) -> Dict[str, Any]:
    """Normalize dashboard/payments credit display.

    Live balances come from v_pricing_account_overview / pricing_credit_accounts.
    billing_entitlements is used for plan metadata and plan cap only.
    """
    plan_summary = _as_dict_deep_loose(resp.get("plan_summary"))
    pricing_summary = _as_dict_deep_loose(resp.get("pricing_summary"))
    usage_summary = _as_dict_deep_loose(resp.get("usage_summary"))
    usage = _as_dict_deep_loose(resp.get("usage"))

    ent = _as_dict_deep_loose(ent_row)
    pao = _as_dict_deep_loose(pricing_account_overview_row)
    plan_json = _as_dict_deep_loose(pao.get("plan_json"))
    lots_json = _as_dict_deep_loose(pao.get("lots_json"))
    legacy_account = _as_dict_deep_loose(pao.get("legacy_account_json"))
    account = _as_dict_deep_loose(pricing_account_row)

    plan_code = _clean_text(
        plan_json.get("plan_code")
        or ent.get("plan_code")
        or plan_summary.get("plan_code")
        or "free"
    ).lower() or "free"
    tier_code = _clean_text(
        plan_json.get("tier_code")
        or ent.get("tier_code")
        or plan_summary.get("tier_code")
        or ""
    ).lower() or ("enterprise" if plan_code.startswith("enterprise") else "business" if plan_code.startswith("business") else "pro" if plan_code.startswith("pro") else "free")
    billing_mode = _clean_text(plan_json.get("billing_mode") or ent.get("billing_mode") or plan_summary.get("billing_mode")).lower()
    settlement_mode = _clean_text(plan_json.get("settlement_mode") or ent.get("settlement_mode") or plan_summary.get("settlement_mode")).lower()

    plan_total = _to_int_credits(
        _first_number(
            plan_json.get("included_credits_total"),
            ent.get("included_credits_total"),
            plan_summary.get("included_credits_total"),
            pricing_summary.get("total_credits"),
        ),
        0,
    )

    included_available = _to_int_credits(
        _first_number(
            lots_json.get("included_available"),
            lots_json.get("included_credits_available"),
        ),
        default=-1,
    )
    if included_available < 0:
        included_available = _to_int_credits(
            _first_number(
                pricing_summary.get("included_available"),
                pricing_summary.get("available_credits"),
                legacy_account.get("legacy_balance_credits"),
                account.get("balance_credits"),
            ),
            0,
        )

    included_reserved = _to_int_credits(
        _first_number(lots_json.get("included_reserved"), lots_json.get("included_credits_reserved")),
        0,
    )
    wallet_available = _to_int_credits(
        _first_number(lots_json.get("purchased_available"), lots_json.get("wallet_available"), lots_json.get("topup_available")),
        0,
    )
    wallet_reserved = _to_int_credits(
        _first_number(lots_json.get("purchased_reserved"), lots_json.get("wallet_reserved"), lots_json.get("topup_reserved")),
        0,
    )
    promo_available = _to_int_credits(_first_number(lots_json.get("promo_available")), 0)
    promo_reserved = _to_int_credits(_first_number(lots_json.get("promo_reserved")), 0)

    total_available = _to_int_credits(
        _first_number(
            lots_json.get("total_spendable"),
            lots_json.get("total_available"),
            pricing_summary.get("total_available"),
            pricing_summary.get("available_credits"),
            legacy_account.get("legacy_balance_credits"),
            account.get("balance_credits"),
        ),
        included_available + wallet_available + promo_available,
    )
    total_reserved = _to_int_credits(
        _first_number(
            lots_json.get("total_reserved"),
            pricing_summary.get("total_reserved"),
            pricing_summary.get("reserved_credits"),
            legacy_account.get("legacy_reserved_credits"),
            account.get("reserved_credits"),
        ),
        included_reserved + wallet_reserved + promo_reserved,
    )
    included_used = _to_int_credits(
        _first_number(lots_json.get("included_used"), usage_summary.get("used_credits"), usage.get("used_credits")),
        max(plan_total - included_available - included_reserved, 0) if plan_total > 0 else 0,
    )
    usage_percent = round((included_used / plan_total) * 100.0, 2) if plan_total > 0 else None

    is_enterprise = tier_code == "enterprise" or plan_code.startswith("enterprise")
    is_postpaid = is_enterprise or settlement_mode in {"postpaid", "money", "invoice"} or billing_mode in {"postpaid", "invoice"}
    billing_model = "postpaid" if is_postpaid else "prepaid"
    plan_name = _public_plan_name(plan_code, tier_code)

    if billing_model == "postpaid":
        display = {
            "header_label": f"{plan_name} • Postpaid",
            "billing_label": "Monthly usage in credits",
            "included_label": "Postpaid credits",
            "wallet_label": "Not needed",
            "reserved_label": _credit_label(total_reserved),
            "cycle_usage_label": _cycle_usage_label(included_used, plan_total),
            "total_available_label": "Postpaid credits",
        }
    else:
        display = {
            "header_label": f"{total_available} available • {total_reserved} reserved",
            "billing_label": "Credits",
            "included_label": _included_label(included_available, plan_total),
            "wallet_label": _credit_label(wallet_available),
            "reserved_label": _credit_label(total_reserved),
            "cycle_usage_label": _cycle_usage_label(included_used, plan_total),
            "total_available_label": _credit_label(total_available),
        }

    resp["billing_model"] = billing_model
    resp["plan"] = {
        "plan_code": plan_code,
        "plan_name": plan_name,
        "tier_code": tier_code,
        "billing_mode": billing_mode or ("invoice" if is_postpaid else "subscription"),
        "settlement_mode": settlement_mode or ("postpaid" if is_postpaid else "credits"),
        "included_credits_total": plan_total,
        "source": "billing_entitlements+pricing_account_overview",
    }
    resp["credits"] = {
        "available_credits": total_available,
        "reserved_credits": total_reserved,
        "used_credits": included_used,
        "total_credits": plan_total,
        "included_available": included_available,
        "included_reserved": included_reserved,
        "included_used": included_used,
        "wallet_available": wallet_available,
        "wallet_reserved": wallet_reserved,
        "promo_available": promo_available,
        "promo_reserved": promo_reserved,
        "total_available": total_available,
        "total_reserved": total_reserved,
        "total_spendable": total_available,
        "usage_percent": usage_percent,
        "source": "v_pricing_account_overview",
    }
    resp["display"] = display

    resp["plan_summary"] = {
        **plan_summary,
        "plan_code": plan_code,
        "plan_name": plan_name,
        "tier_code": tier_code,
        "billing_mode": resp["plan"]["billing_mode"],
        "settlement_mode": resp["plan"]["settlement_mode"],
        "included_credits_total": plan_total,
        # Keep legacy field visible for diagnostics only; do not let it drive UI.
        "included_credits_remaining_legacy": ent.get("included_credits_remaining"),
        "source": plan_summary.get("source") or "canonical_credit_contract",
    }
    resp["pricing_summary"] = {
        **pricing_summary,
        "available_credits": total_available,
        "reserved_credits": total_reserved,
        "total_credits": plan_total,
        "credit_cap": plan_total,
        "included_available": included_available,
        "wallet_available": wallet_available,
        "total_available": total_available,
        "total_reserved": total_reserved,
        "billing_model": billing_model,
        "source": "v_pricing_account_overview",
    }
    resp["usage_summary"] = {
        **usage_summary,
        "used_credits": included_used,
        "usage_percent": usage_percent,
        "source": "canonical_credit_contract",
    }
    resp["usage"] = {
        **usage,
        "used_credits": included_used,
        "usage_percent": usage_percent,
        "reserved_credits": total_reserved,
        "available_credits": total_available,
    }

    return resp


async def _fetch_home_row(conn: asyncpg.Connection, user_id: str) -> Optional[asyncpg.Record]:
    return await conn.fetchrow(
        """
        select user_id, updated_at, gauges_json, alerts_json, face_carousel_json, video_carousel_json, header_json
        from public.v_dashboard_home
        where user_id = $1::uuid
        """,
        user_id,
    )


async def _fetch_pricing_snapshot_row(conn: asyncpg.Connection, user_id: str) -> Optional[asyncpg.Record]:
    return await conn.fetchrow(
        """
        select user_id, plan_summary_json, pricing_summary_json, usage_summary_json, usage_json
        from public.v_dashboard_pricing_snapshot
        where user_id = $1::uuid
        """,
        user_id,
    )


async def _fetch_pricing_account_row(conn: asyncpg.Connection, user_id: str) -> Optional[asyncpg.Record]:
    return await conn.fetchrow(
        """
        select user_id, balance_credits, reserved_credits, updated_at
        from public.pricing_credit_accounts
        where user_id = $1::uuid
        """,
        user_id,
    )

async def _fetch_pricing_account_overview_row(conn: asyncpg.Connection, user_id: str) -> Optional[asyncpg.Record]:
    try:
        return await conn.fetchrow(
            """
            select user_id, plan_json, lots_json, legacy_account_json
            from public.v_pricing_account_overview
            where user_id = $1::uuid
            """,
            user_id,
        )
    except Exception:
        return None



async def _fetch_billing_entitlement_row(conn: asyncpg.Connection, user_id: str) -> Optional[asyncpg.Record]:
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
            updated_at
        from public.billing_entitlements
        where user_id = $1::uuid
        order by updated_at desc nulls last
        limit 1
        """,
        user_id,
    )


async def _fetch_runway_mode_rows(conn: asyncpg.Connection) -> List[asyncpg.Record]:
    try:
        rows = await conn.fetch(
            """
            select
                studio,
                mode,
                label,
                display_unit,
                baseline_display_qty,
                variant_code,
                variant_name,
                variant_category,
                estimated_credits_for_baseline_qty,
                estimated_credits_per_display_unit,
                supported_line_count,
                unsupported_line_count,
                source_sku_codes,
                sort_order
            from public.v_dashboard_runway_mode_costs
            order by sort_order asc, studio asc, mode asc
            """
        )
        return list(rows)
    except Exception:
        return []


async def _fetch_realtime_operational_gauges(conn: asyncpg.Connection) -> Dict[str, Any]:
    """
    Best-effort live gauges from public.studio_jobs.
    Falls back silently if the table/columns differ in a given deployment.
    """
    try:
        throughput_row = await conn.fetchrow(
            """
            select count(*)::int as completed_60m
            from public.studio_jobs
            where status = 'succeeded'
              and coalesce(updated_at, created_at) >= (now() at time zone 'utc') - interval '60 minutes'
            """
        )
        completed_60m = int(throughput_row["completed_60m"] or 0)

        queue_row = await conn.fetchrow(
            """
            select count(*)::int as queue_depth
            from public.studio_jobs
            where status in ('queued', 'pending', 'processing', 'running', 'in_progress')
            """
        )
        queue_depth = int(queue_row["queue_depth"] or 0)

        success_row = await conn.fetchrow(
            """
            with recent as (
                select status
                from public.studio_jobs
                where coalesce(updated_at, created_at) >= (now() at time zone 'utc') - interval '24 hours'
            )
            select
                count(*) filter (where status = 'succeeded')::int as success_count,
                count(*) filter (where status in ('failed', 'error', 'canceled', 'cancelled'))::int as failed_count,
                count(*)::int as total_count
            from recent
            """
        )
        success_count = int(success_row["success_count"] or 0)
        failed_count = int(success_row["failed_count"] or 0)
        total_count = int(success_row["total_count"] or 0)
        success_rate = round((success_count / total_count) * 100.0, 2) if total_count > 0 else None

        queue_status = "high" if queue_depth >= 25 else "busy" if queue_depth >= 10 else "ok"

        return {
            "throughput": {
                "label": "Throughput",
                "completed_60m": completed_60m,
                "raw_value": completed_60m,
                "value_norm": min(completed_60m / 25.0, 1.0) if completed_60m > 0 else 0.0,
                "status": "ok" if completed_60m > 0 else "idle",
                "helper": "Completed jobs in the last 60 minutes.",
            },
            "queue_pressure": {
                "label": "Queue pressure",
                "queue_depth": queue_depth,
                "raw_value": queue_depth,
                "value_norm": min(queue_depth / 25.0, 1.0) if queue_depth > 0 else 0.0,
                "status": queue_status,
                "helper": "Queued and in-flight jobs right now.",
            },
            "success_rate": {
                "label": "Success rate",
                "success_rate_24h": success_rate,
                "raw_value": success_rate,
                "value_norm": (success_rate / 100.0) if success_rate is not None else None,
                "status": "ok" if (success_rate is None or success_rate >= 95.0) else "warn" if success_rate >= 85.0 else "bad",
                "helper": f"{success_count} succeeded / {failed_count} failed in the last 24 hours." if total_count > 0 else "No jobs in the last 24 hours.",
            },
        }
    except Exception:
        return {}


def _record_to_dict(r: asyncpg.Record) -> Dict[str, Any]:
    return {
        "user_id": str(r["user_id"]),
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        "gauges": _coerce_json(r["gauges_json"]) or {},
        "alerts": _coerce_json(r["alerts_json"]) or [],
        "face_carousel": _coerce_json(r["face_carousel_json"]) or [],
        "video_carousel": _coerce_json(r["video_carousel_json"]) or [],
        "header": _coerce_json(r["header_json"]) or {},
    }


def _parse_dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            s = v.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _is_recent(item: Dict[str, Any], days: int) -> bool:
    dt = _parse_dt(item.get("created_at")) or _parse_dt(item.get("updated_at"))
    if not dt:
        return True
    return dt >= (datetime.now(timezone.utc) - timedelta(days=days))


def _get_storage_path(item: Dict[str, Any]) -> Optional[str]:
    meta = item.get("meta") or {}
    return meta.get("storage_path") or item.get("storage_path") or item.get("output_storage_path")


def _fmt_credit_value(v: Optional[float]) -> Optional[str]:
    if v is None:
        return None
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _infer_plan_name(resp: Dict[str, Any], fallback: str = "Free") -> str:
    header = resp.get("header") or {}
    candidates = [
        header.get("plan_name"),
        header.get("planName"),
        header.get("plan"),
        header.get("plan_label"),
        header.get("planLabel"),
    ]
    for c in candidates:
        s = str(c or "").strip()
        if s:
            return s
    return fallback


def _coerce_nonnegative(v: Optional[float]) -> float:
    if v is None:
        return 0.0
    return max(v, 0.0)


def _floor_div(numerator: Optional[float], denominator: Optional[float]) -> int:
    num = _coerce_nonnegative(numerator)
    den = _coerce_nonnegative(denominator)
    if den <= 0:
        return 0
    return max(int(math.floor(num / den)), 0)


def _build_runway_summary(resp: Dict[str, Any], runway_rows: List[asyncpg.Record]) -> Dict[str, Any]:
    if not runway_rows:
        return {}

    plan_summary = resp.get("plan_summary") or {}
    pricing_summary = resp.get("pricing_summary") or {}
    usage_summary = resp.get("usage_summary") or {}

    total_credits_raw = (
        _as_number(pricing_summary.get("total_credits"))
        or _as_number(pricing_summary.get("credit_cap"))
        or _as_number(plan_summary.get("included_credits_total"))
    )
    included_remaining = _as_number(plan_summary.get("included_credits_remaining"))

    available = _coerce_nonnegative(
        _as_number(pricing_summary.get("available_credits"))
        or included_remaining
    )
    reserved = _coerce_nonnegative(_as_number(pricing_summary.get("reserved_credits")))
    used = _as_number(usage_summary.get("used_credits"))
    usage_percent = _as_number(usage_summary.get("usage_percent"))
    plan_name = _infer_plan_name(resp, fallback=str(plan_summary.get("plan_name") or "Free"))
    total_credits = _coerce_nonnegative(total_credits_raw) if total_credits_raw is not None else None

    estimates: List[Dict[str, Any]] = []
    hero_lines: List[str] = []

    for row in runway_rows:
        unit = str(row["display_unit"])
        credits_for_baseline = _coerce_nonnegative(_as_number(row["estimated_credits_for_baseline_qty"]))
        credits_per_display_unit = _coerce_nonnegative(_as_number(row["estimated_credits_per_display_unit"]))
        baseline_display_qty = _coerce_nonnegative(_as_number(row["baseline_display_qty"]))

        remaining_units = _floor_div(available, credits_per_display_unit)

        item = {
            "studio": str(row["studio"]),
            "mode": str(row["mode"]),
            "label": str(row["label"]),
            "unit": unit,
            "baseline_display_qty": baseline_display_qty,
            "variant_code": str(row["variant_code"]),
            "variant_name": row["variant_name"],
            "variant_category": row["variant_category"],
            "estimated_credits_for_baseline_qty": credits_for_baseline,
            "estimated_credits_per_display_unit": credits_per_display_unit,
            "remaining_units": remaining_units,
            "supported_line_count": int(row["supported_line_count"] or 0),
            "unsupported_line_count": int(row["unsupported_line_count"] or 0),
            "source_sku_codes": row["source_sku_codes"],
        }
        estimates.append(item)

        if unit == "runs":
            noun = "runs" if remaining_units != 1 else "run"
            hero_lines.append(f"~{remaining_units} {row['studio']} {noun}")
        elif unit == "seconds":
            hero_lines.append(f"~{remaining_units} sec of {row['studio']}")
        elif unit == "minutes":
            hero_lines.append(f"~{remaining_units} min of {row['studio']}")
        elif unit == "kchars":
            hero_lines.append(f"~{remaining_units} audio 1K-char blocks")
        elif unit == "chars":
            hero_lines.append(f"~{remaining_units} audio chars")

    # Keep runway/dashboard hero text aligned with the compact billing header.
    # Detailed cycle usage belongs in plan/usage cards, not the top-line summary.
    top_line = (
        f"{plan_name} • "
        f"{_fmt_credit_value(available) or '0'} available • "
        f"{_fmt_credit_value(reserved) or '0'} reserved"
    )

    return {
        "plan_name": plan_name,
        "total_credits": total_credits,
        "available_credits": available,
        "reserved_credits": reserved,
        "used_credits": used,
        "usage_percent": usage_percent,
        "top_line": top_line,
        "hero_lines": hero_lines,
        "estimates": estimates,
        "cta": {
            "primary": "upgrade",
            "secondary": "view_running_jobs" if reserved > 0 else "top_up",
        },
    }


async def _fallback_live_pricing_snapshot(conn: asyncpg.Connection, user_id: str) -> Optional[Dict[str, Any]]:
    account = await _fetch_pricing_account_row(conn, user_id)
    if account is None:
        return None

    available = _as_number(account["balance_credits"]) or 0.0
    reserved = _as_number(account["reserved_credits"]) or 0.0

    return {
        "plan_summary": {
            "plan_name": "Free",
            "source": "dashboard_service_fallback_default",
        },
        "pricing_summary": {
            "available_credits": available,
            "reserved_credits": reserved,
            "updated_at": account["updated_at"].isoformat() if account["updated_at"] else None,
            "source": "pricing_credit_accounts",
        },
        "usage_summary": {
            "used_credits": None,
            "usage_percent": None,
            "source": "fallback_no_usage_source",
        },
        "usage": {
            "used_credits": None,
            "available_credits": available,
            "reserved_credits": reserved,
            "usage_percent": None,
        },
    }


async def _augment_with_live_pricing(
    conn: asyncpg.Connection,
    resp: Dict[str, Any],
    user_id: str,
) -> Dict[str, Any]:
    snapshot: Optional[Dict[str, Any]] = None

    try:
        row = await _fetch_pricing_snapshot_row(conn, user_id)
        if row is not None:
            snapshot = {
                "plan_summary": _coerce_json(row["plan_summary_json"]) or {},
                "pricing_summary": _coerce_json(row["pricing_summary_json"]) or {},
                "usage_summary": _coerce_json(row["usage_summary_json"]) or {},
                "usage": _coerce_json(row["usage_json"]) or {},
            }
    except Exception:
        snapshot = None

    if snapshot is None:
        snapshot = await _fallback_live_pricing_snapshot(conn, user_id)

    if snapshot:
        resp["plan_summary"] = snapshot.get("plan_summary") or {}
        resp["pricing_summary"] = snapshot.get("pricing_summary") or {}
        resp["usage_summary"] = snapshot.get("usage_summary") or {}
        resp["usage"] = snapshot.get("usage") or {}

    try:
        ent_row = await _fetch_billing_entitlement_row(conn, user_id)
    except Exception:
        ent_row = None

    try:
        pricing_account_row = await _fetch_pricing_account_row(conn, user_id)
    except Exception:
        pricing_account_row = None

    try:
        pricing_account_overview_row = await _fetch_pricing_account_overview_row(conn, user_id)
    except Exception:
        pricing_account_overview_row = None

    resp = _canonical_credit_contract(
        resp=resp,
        ent_row=ent_row,
        pricing_account_row=pricing_account_row,
        pricing_account_overview_row=pricing_account_overview_row,
    )

    available = _as_number((resp.get("credits") or {}).get("total_available"))
    reserved = _as_number((resp.get("credits") or {}).get("total_reserved"))
    used = _as_number((resp.get("credits") or {}).get("included_used"))
    total_credits = _as_number((resp.get("credits") or {}).get("total_credits"))
    display = resp.get("display") or {}
    plan = resp.get("plan") or {}
    plan_name = str(plan.get("plan_name") or "Free")

    resp.setdefault("gauges", {})
    fuel = (resp["gauges"] or {}).get("fuel") or {}
    fuel["label"] = fuel.get("label") or "Credits"
    fuel["credits_remaining"] = available or 0
    fuel["reserved_credits"] = reserved or 0
    fuel["total_credits"] = total_credits or 0
    fuel["credit_cap"] = total_credits or 0
    if total_credits and total_credits > 0 and available is not None:
        fuel["value_norm"] = max(min(float(available) / float(total_credits), 1.0), 0.0)
    else:
        fuel["value_norm"] = fuel.get("value_norm") if fuel.get("value_norm") not in (None, "") else 0
    resp["gauges"]["fuel"] = fuel

    resp["gauges"].update(await _fetch_realtime_operational_gauges(conn))

    resp.setdefault("header", {})
    resp["header"].update(
        {
            "plan_name": plan_name,
            "plan_label": plan_name,
            "usage_label": display.get("header_label"),
            "billing_value_label": display.get("header_label"),
            "header_label": display.get("header_label"),
            "available_credits": int(available or 0),
            "reserved_credits": int(reserved or 0),
            "used_credits": int(used or 0),
            "total_credits": int(total_credits or 0),
            "billing_model": resp.get("billing_model"),
        }
    )

    runway_rows = await _fetch_runway_mode_rows(conn)
    resp["runway_summary"] = _build_runway_summary(resp, runway_rows)

    return resp


def _enrich_carousels_with_sas(resp: Dict[str, Any]) -> Dict[str, Any]:
    face_ttl_seconds = int(getattr(settings, "DASHBOARD_FACE_SAS_TTL_SECONDS", 2 * 24 * 3600))
    recent_video_ttl_seconds = int(getattr(settings, "DASHBOARD_RECENT_VIDEO_SAS_TTL_SECONDS", 15 * 24 * 3600))
    default_video_ttl_seconds = int(getattr(settings, "DASHBOARD_VIDEO_SAS_TTL_SECONDS", 24 * 3600))
    recent_window_days = int(getattr(settings, "DASHBOARD_RECENT_WINDOW_DAYS", 15))

    face_container = getattr(settings, "AZURE_FACE_OUTPUT_CONTAINER", "face-output")
    video_container = getattr(settings, "AZURE_VIDEO_OUTPUT_CONTAINER", "video-output")

    signer = AzureBlobSasSigner.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)

    for it in (resp.get("face_carousel") or []):
        if not isinstance(it, dict):
            continue

        sp = _get_storage_path(it)
        if sp:
            it["image_url"] = signer.sign_read_url(face_container, sp, face_ttl_seconds)
            continue

        existing = it.get("image_url")
        parts = split_container_blob_from_url(existing) if existing else None
        it["image_url"] = signer.sign_read_url(parts[0], parts[1], face_ttl_seconds) if parts else None

    for it in (resp.get("video_carousel") or []):
        if not isinstance(it, dict):
            continue

        ttl = recent_video_ttl_seconds if _is_recent(it, recent_window_days) else default_video_ttl_seconds

        sp = _get_storage_path(it)
        if sp:
            it["video_url"] = signer.sign_read_url(video_container, sp, ttl)
            continue

        existing = it.get("video_url")
        parts = split_container_blob_from_url(existing) if existing else None
        it["video_url"] = signer.sign_read_url(parts[0], parts[1], ttl) if parts else None

    return resp


def _clean_text(v: Any) -> str:
    return str(v or "").strip()


def _pick_first(d: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in d:
            value = d.get(key)
            if value not in (None, ""):
                return value
    return None


def _humanize_code(value: Any) -> str:
    s = _clean_text(value)
    if not s:
        return ""
    s = s.replace(".", " ").replace("_", " ").replace("-", " ")
    parts = [p for p in s.split() if p]
    if not parts:
        return ""
    acronyms = {"ai", "tts", "i2i", "t2i", "ugc", "b2b", "b2c"}
    out = []
    for p in parts:
        pl = p.lower()
        if pl in acronyms:
            out.append(pl.upper())
        else:
            out.append(pl.capitalize())
    return " ".join(out)


def _is_generic_face_title(title: Any) -> bool:
    s = _clean_text(title).lower()
    if not s:
        return True
    if s == "face":
        return True
    if s.startswith("face variant "):
        suffix = s.replace("face variant ", "", 1).strip()
        return suffix.isdigit()
    return False


def _build_face_library_title(item: Dict[str, Any], meta: Dict[str, Any], reuse: Dict[str, Any]) -> str:
    current_title = _clean_text(_pick_first(item, "title", "name"))
    variant_number = _clean_text(_pick_first(reuse, "variant_number", "variant", "variant_index"))         or _clean_text(meta.get("variant_number"))         or _clean_text(_coerce_json(meta.get("technical_specs") or {}).get("variant_number"))

    if current_title and not _is_generic_face_title(current_title):
        if variant_number and f"variant {variant_number}".lower() not in current_title.lower():
            return f"{current_title} • Variant {variant_number}"
        return current_title

    job_payload = _coerce_json(meta.get("job_payload")) or {}
    profile_meta = _coerce_json(meta.get("profile_meta")) or {}
    profile_attributes = _coerce_json(meta.get("profile_attributes")) or {}
    technical_specs = _coerce_json(meta.get("technical_specs")) or {}

    label = (
        _humanize_code(job_payload.get("context_label"))
        or _humanize_code(job_payload.get("context_code"))
        or _humanize_code(job_payload.get("use_case_label"))
        or _humanize_code(job_payload.get("use_case_code"))
        or _humanize_code(job_payload.get("shot_type_label"))
        or _humanize_code(job_payload.get("shot_type_code"))
        or _humanize_code(profile_meta.get("context_label"))
        or _humanize_code(profile_meta.get("context_code"))
        or _humanize_code(profile_attributes.get("context_label"))
        or _humanize_code(profile_attributes.get("context_code"))
        or _humanize_code(technical_specs.get("context_code"))
        or "Face"
    )

    if variant_number:
        return f"{label} • Variant {variant_number}"
    return label


async def _fetch_library_view_columns(conn: asyncpg.Connection) -> List[str]:
    try:
        rows = await conn.fetch(
            """
            select column_name
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'v_dashboard_asset_library'
            order by ordinal_position
            """
        )
        return [str(r["column_name"]) for r in rows]
    except Exception:
        return []


async def _fetch_library_view_rows(
    conn: asyncpg.Connection,
    user_id: str,
    asset_type: str,
    limit: int,
    offset: int,
) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    columns = await _fetch_library_view_columns(conn)
    if not columns or "user_id" not in columns:
        return [], None

    where_sql = ["user_id = $1::uuid"]
    params: List[Any] = [user_id]

    can_filter_by_studio = "studio" in columns
    if asset_type != "all" and can_filter_by_studio:
        params.append(asset_type)
        where_sql.append(f"lower(coalesce(studio, '')) = lower(${len(params)})")

    order_column = "created_at" if "created_at" in columns else ("updated_at" if "updated_at" in columns else None)
    base_sql = f"from public.v_dashboard_asset_library where {' and '.join(where_sql)}"

    total_count: Optional[int] = None
    try:
        total_row = await conn.fetchrow(f"select count(*)::int as total_count {base_sql}", *params)
        total_count = int(total_row["total_count"] or 0) if total_row else 0
    except Exception:
        total_count = None

    query_sql = f"select * {base_sql}"
    if order_column:
        query_sql += f" order by {order_column} desc nulls last"
    query_sql += f" limit ${len(params)+1} offset ${len(params)+2}"

    try:
        rows = await conn.fetch(query_sql, *params, limit, offset)
        items = [dict(r) for r in rows]
    except Exception:
        return [], total_count

    if asset_type != "all" and not can_filter_by_studio:
        items = [r for r in items if _clean_text(r.get("studio")).lower() == asset_type]

    return items, total_count


def _signed_url_from_parts(
    signer: AzureBlobSasSigner,
    raw_url: Any,
    container: Any,
    blob_path: Any,
    ttl_seconds: int,
) -> Optional[str]:
    container_text = _clean_text(container)
    blob_text = _clean_text(blob_path)
    if container_text and blob_text:
        try:
            return signer.sign_read_url(container_text, blob_text, ttl_seconds)
        except Exception:
            pass

    existing = _clean_text(raw_url)
    if not existing:
        return None

    try:
        parts = split_container_blob_from_url(existing)
        if parts:
            return signer.sign_read_url(parts[0], parts[1], ttl_seconds)
    except Exception:
        pass

    return existing


def _normalize_library_item(
    item: Dict[str, Any],
    signer: AzureBlobSasSigner,
) -> Optional[Dict[str, Any]]:
    studio = _clean_text(_pick_first(item, "studio", "kind", "source_studio")).lower()
    if studio not in {"face", "audio", "video", "fusion"}:
        studio = "video" if _clean_text(item.get("asset_type")).lower() == "video" else studio
    if studio == "fusion":
        studio = "video"

    meta = _coerce_json(_pick_first(item, "metadata_json", "meta_json", "metadata")) or {}
    reuse = _coerce_json(_pick_first(item, "reuse_payload_json", "reuse_payload")) or {}

    face_ttl_seconds = int(getattr(settings, "DASHBOARD_FACE_SAS_TTL_SECONDS", 2 * 24 * 3600))
    audio_ttl_seconds = int(getattr(settings, "DASHBOARD_AUDIO_SAS_TTL_SECONDS", 2 * 24 * 3600))
    recent_video_ttl_seconds = int(getattr(settings, "DASHBOARD_RECENT_VIDEO_SAS_TTL_SECONDS", 15 * 24 * 3600))
    default_video_ttl_seconds = int(getattr(settings, "DASHBOARD_VIDEO_SAS_TTL_SECONDS", 24 * 3600))
    recent_window_days = int(getattr(settings, "DASHBOARD_RECENT_WINDOW_DAYS", 15))

    ttl = face_ttl_seconds if studio == "face" else audio_ttl_seconds if studio == "audio" else (
        recent_video_ttl_seconds if _is_recent(item, recent_window_days) else default_video_ttl_seconds
    )

    thumbnail_url = _signed_url_from_parts(
        signer,
        _pick_first(item, "thumbnail_url", "image_url", "preview_url"),
        _pick_first(item, "thumbnail_container", "thumbnail_storage_container", "storage_container", "container_name"),
        _pick_first(item, "thumbnail_storage_path", "thumbnail_blob_path", "thumbnail_path", "storage_path", "blob_path"),
        ttl,
    )

    preview_url = _signed_url_from_parts(
        signer,
        _pick_first(item, "preview_url", "image_url", "audio_url", "video_url", "download_url"),
        _pick_first(item, "preview_container", "preview_storage_container", "storage_container", "container_name"),
        _pick_first(item, "preview_storage_path", "preview_blob_path", "preview_path", "storage_path", "blob_path"),
        ttl,
    )

    download_url = _signed_url_from_parts(
        signer,
        _pick_first(item, "download_url", "preview_url", "image_url", "audio_url", "video_url"),
        _pick_first(item, "download_container", "download_storage_container", "storage_container", "container_name"),
        _pick_first(item, "download_storage_path", "download_blob_path", "download_path", "storage_path", "blob_path"),
        ttl,
    )

    artifact_id = _clean_text(_pick_first(item, "artifact_id", "face_artifact_id", "audio_artifact_id", "video_artifact_id"))
    media_asset_id = _clean_text(_pick_first(item, "media_asset_id", "face_media_asset_id", "audio_media_asset_id", "video_media_asset_id"))
    face_profile_id = _clean_text(_pick_first(item, "face_profile_id")) or _clean_text(meta.get("face_profile_id"))
    created_at = _pick_first(item, "created_at", "updated_at")

    library_id = _clean_text(_pick_first(item, "library_id", "id"))
    if not library_id:
        key = artifact_id or media_asset_id or _clean_text(preview_url) or _clean_text(download_url)
        if not key:
            return None
        library_id = f"{studio or 'asset'}:{key}"

    if studio == "face":
        resolved_image_url = _clean_text(_pick_first(reuse, "image_url", "face_image_url")) or _clean_text(preview_url) or _clean_text(thumbnail_url)
        reuse_payload = {
            "face_artifact_id": _clean_text(_pick_first(reuse, "face_artifact_id", "artifact_id")) or artifact_id or None,
            "media_asset_id": _clean_text(_pick_first(reuse, "media_asset_id", "face_media_asset_id")) or media_asset_id or None,
            "face_profile_id": _clean_text(_pick_first(reuse, "face_profile_id")) or face_profile_id or None,
            "image_url": resolved_image_url or None,
            "gender": _clean_text(_pick_first(reuse, "gender")) or _clean_text(meta.get("gender")) or None,
            "aspect_ratio": _clean_text(_pick_first(reuse, "aspect_ratio")) or _clean_text(meta.get("aspect_ratio")) or None,
        }
        title = _build_face_library_title(item, meta, reuse)
        asset_type = _clean_text(_pick_first(item, "asset_type")) or "image"
    elif studio == "audio":
        resolved_audio_url = _clean_text(_pick_first(reuse, "audio_url")) or _clean_text(preview_url) or _clean_text(download_url)
        duration_sec = _as_number(_pick_first(reuse, "duration_sec", "audio_duration_sec", "duration_seconds", "duration")) or _as_number(_pick_first(item, "duration_sec", "audio_duration_sec"))
        reuse_payload = {
            "audio_artifact_id": _clean_text(_pick_first(reuse, "audio_artifact_id", "artifact_id")) or artifact_id or None,
            "media_asset_id": _clean_text(_pick_first(reuse, "media_asset_id", "audio_media_asset_id")) or media_asset_id or None,
            "audio_url": resolved_audio_url or None,
            "duration_sec": duration_sec,
            "locale": _clean_text(_pick_first(reuse, "locale", "audio_locale")) or _clean_text(meta.get("audio_locale")) or None,
            "voice_id": _clean_text(_pick_first(reuse, "voice_id", "voice")) or _clean_text(meta.get("audio_voice")) or None,
            "voice": _clean_text(_pick_first(reuse, "voice", "voice_id")) or _clean_text(meta.get("audio_voice")) or None,
            "script_text": _clean_text(_pick_first(reuse, "script_text")) or _clean_text(meta.get("script_text")) or None,
        }
        title = _clean_text(_pick_first(item, "title", "name")) or "Audio"
        asset_type = _clean_text(_pick_first(item, "asset_type")) or "audio"
    else:
        resolved_video_url = _clean_text(_pick_first(reuse, "video_url")) or _clean_text(preview_url) or _clean_text(download_url)
        reuse_payload = {
            "video_artifact_id": _clean_text(_pick_first(reuse, "video_artifact_id", "artifact_id")) or artifact_id or None,
            "media_asset_id": _clean_text(_pick_first(reuse, "media_asset_id", "video_media_asset_id")) or media_asset_id or None,
            "video_url": resolved_video_url or None,
        }
        title = _clean_text(_pick_first(item, "title", "name")) or "Video"
        asset_type = _clean_text(_pick_first(item, "asset_type")) or "video"

    return {
        "library_id": library_id,
        "studio": studio,
        "asset_type": asset_type,
        "title": title,
        "status": _clean_text(_pick_first(item, "status")) or "ready",
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "thumbnail_url": thumbnail_url,
        "preview_url": preview_url,
        "download_url": download_url,
        "duration_sec": _as_number(_pick_first(item, "duration_sec", "audio_duration_sec")) or reuse_payload.get("duration_sec"),
        "source_job_id": _clean_text(_pick_first(item, "source_job_id", "job_id")) or None,
        "artifact_id": artifact_id or None,
        "media_asset_id": media_asset_id or None,
        "reuse_payload": {k: v for k, v in reuse_payload.items() if v not in (None, "")},
    }


def _fallback_library_from_home(resp: Dict[str, Any], asset_type: str = "all") -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    if asset_type in ("all", "face"):
        for it in (resp.get("face_carousel") or []):
            if not isinstance(it, dict):
                continue
            image_url = _clean_text(it.get("image_url") or it.get("url") or ((it.get("meta") or {}).get("image_url")))
            if not image_url:
                continue
            meta = it.get("meta") or {}
            artifact_id = _clean_text(it.get("artifact_id") or meta.get("artifact_id"))
            media_asset_id = _clean_text(it.get("media_asset_id") or meta.get("media_asset_id"))
            items.append({
                "library_id": f"face:{artifact_id or media_asset_id or image_url}",
                "studio": "face",
                "asset_type": "image",
                "title": "Face",
                "status": "ready",
                "created_at": it.get("created_at"),
                "thumbnail_url": image_url,
                "preview_url": image_url,
                "download_url": image_url,
                "artifact_id": artifact_id or None,
                "media_asset_id": media_asset_id or None,
                "reuse_payload": {
                    "face_artifact_id": artifact_id or None,
                    "media_asset_id": media_asset_id or None,
                    "face_profile_id": _clean_text(meta.get("face_profile_id")) or None,
                    "image_url": image_url,
                    "gender": _clean_text(meta.get("gender")) or None,
                    "aspect_ratio": _clean_text(meta.get("aspect_ratio")) or None,
                },
            })

    if asset_type in ("all", "video"):
        for it in (resp.get("video_carousel") or []):
            if not isinstance(it, dict):
                continue
            video_url = _clean_text(it.get("video_url") or it.get("url") or ((it.get("meta") or {}).get("video_url")))
            if not video_url:
                continue
            meta = it.get("meta") or {}
            artifact_id = _clean_text(it.get("artifact_id") or meta.get("artifact_id"))
            media_asset_id = _clean_text(it.get("media_asset_id") or meta.get("media_asset_id"))
            items.append({
                "library_id": f"video:{artifact_id or media_asset_id or video_url}",
                "studio": "video",
                "asset_type": "video",
                "title": "Video",
                "status": "ready",
                "created_at": it.get("created_at"),
                "thumbnail_url": None,
                "preview_url": video_url,
                "download_url": video_url,
                "artifact_id": artifact_id or None,
                "media_asset_id": media_asset_id or None,
                "reuse_payload": {
                    "video_artifact_id": artifact_id or None,
                    "media_asset_id": media_asset_id or None,
                    "video_url": video_url,
                },
            })

    return items


async def get_dashboard_library(
    pool: asyncpg.Pool,
    user_id: str,
    asset_type: str = "all",
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    asset_type = _clean_text(asset_type).lower() or "all"
    if asset_type not in {"all", "face", "audio", "video"}:
        asset_type = "all"

    safe_limit = max(1, min(int(limit or 50), 100))
    safe_offset = max(0, int(offset or 0))

    async with pool.acquire() as conn:
        signer = AzureBlobSasSigner.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)

        view_rows, total_count = await _fetch_library_view_rows(conn, user_id, asset_type, safe_limit, safe_offset)
        normalized_items = [x for x in (_normalize_library_item(row, signer) for row in view_rows) if x]

        if normalized_items:
            return {
                "items": normalized_items,
                "total": total_count if total_count is not None else len(normalized_items),
                "limit": safe_limit,
                "offset": safe_offset,
                "source": "v_dashboard_asset_library",
                "partial": False,
            }

        home = await get_dashboard_home(pool, user_id, force_refresh=False)
        fallback_items = _fallback_library_from_home(home, asset_type=asset_type)
        sliced_items = fallback_items[safe_offset:safe_offset + safe_limit]

        return {
            "items": sliced_items,
            "total": len(fallback_items),
            "limit": safe_limit,
            "offset": safe_offset,
            "source": "dashboard_home_fallback",
            "partial": asset_type in {"all", "audio", "video"},
            "note": "Audio and full historical library require public.v_dashboard_asset_library for complete results.",
        }



async def get_dashboard_home(pool: asyncpg.Pool, user_id: str, force_refresh: bool = False) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        row = await _fetch_home_row(conn, user_id)

        if row is None and settings.DASHBOARD_FORCE_REFRESH_ON_MISS:
            await conn.execute("select public.fn_dashboard_refresh_home_cache($1::uuid)", user_id)
            row = await _fetch_home_row(conn, user_id)

        if row is None:
            resp = {
                "user_id": user_id,
                "updated_at": None,
                "gauges": {},
                "alerts": [],
                "face_carousel": [],
                "video_carousel": [],
                "header": {},
                "runway_summary": {},
            }
            resp = await _augment_with_live_pricing(conn, resp, user_id)
            return _enrich_carousels_with_sas(resp)

        if force_refresh:
            await conn.execute("select public.fn_dashboard_refresh_home_cache($1::uuid)", user_id)
            row = await _fetch_home_row(conn, user_id)

        updated_at: Optional[datetime] = row["updated_at"] if row else None
        if updated_at:
            age = (datetime.now(timezone.utc) - updated_at).total_seconds()
            if age >= settings.DASHBOARD_STALE_SECONDS:
                await conn.execute(
                    "select public.fn_dashboard_enqueue_refresh($1::uuid, $2::text)",
                    user_id,
                    "stale_home",
                )

        resp = _record_to_dict(row) if row else {
            "user_id": user_id,
            "updated_at": None,
            "gauges": {},
            "alerts": [],
            "face_carousel": [],
            "video_carousel": [],
            "header": {},
            "runway_summary": {},
        }
        resp = await _augment_with_live_pricing(conn, resp, user_id)
        return _enrich_carousels_with_sas(resp)


async def get_dashboard_header(pool: asyncpg.Pool, user_id: str) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select user_id, updated_at, header_json
            from public.v_dashboard_home
            where user_id = $1::uuid
            """,
            user_id,
        )
        if row is None and settings.DASHBOARD_FORCE_REFRESH_ON_MISS:
            await conn.execute("select public.fn_dashboard_refresh_home_cache($1::uuid)", user_id)
            row = await conn.fetchrow(
                """
                select user_id, updated_at, header_json
                from public.v_dashboard_home
                where user_id = $1::uuid
                """,
                user_id,
            )

        if row is None:
            resp = {"user_id": user_id, "updated_at": None, "header": {}}
            resp = await _augment_with_live_pricing(conn, resp, user_id)
            return resp

        resp = {
            "user_id": str(row["user_id"]),
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "header": _coerce_json(row["header_json"]) or {},
        }
        resp = await _augment_with_live_pricing(conn, resp, user_id)
        return resp


async def request_refresh(pool: asyncpg.Pool, user_id: str, reason: str = "manual") -> None:
    async with pool.acquire() as conn:
        await conn.execute("select public.fn_dashboard_enqueue_refresh($1::uuid, $2::text)", user_id, reason)
