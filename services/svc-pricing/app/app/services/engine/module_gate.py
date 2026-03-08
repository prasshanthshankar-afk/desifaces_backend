# services/svc-pricing/app/app/services/engine/module_gate.py
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    billing_mode: str          # bill|shadow|free|disabled
    reason: str
    rule_code: Optional[str] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_country(x: str) -> str:
    return (x or "").strip().upper()


def _norm_channel(x: str) -> str:
    v = (x or "web").strip().lower()
    return v if v in {"web", "mobile", "api"} else "web"


def _norm_tier(x: str) -> str:
    return (x or "free").strip().lower() or "free"


def _norm_mode(x: str) -> str:
    v = (x or "bill").strip().lower()
    if v in {"bill", "shadow", "free", "disabled"}:
        return v
    return "bill"


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _role_keys_allowlist() -> list[str]:
    raw = (settings.PRICING_ADMIN_ROLE_KEYS or "").strip()
    if not raw:
        return ["admin", "ops", "pricing_admin"]
    return [x.strip() for x in raw.split(",") if x.strip()]


async def _best_flag(
    conn: asyncpg.Connection,
    code: str,
    *,
    country_code: str,
    tier_code: str,
    channel: str,
) -> Optional[dict]:
    """
    pricing_feature_flags columns (as per our migrations):
      code, scope, country_code, tier_code, channel, enabled, billing_mode,
      effective_from, effective_to, priority, metadata_json
    """
    now = _now()
    cc = _norm_country(country_code)
    tc = _norm_tier(tier_code)
    ch = _norm_channel(channel)

    row = await conn.fetchrow(
        """
        select code, enabled, billing_mode, scope, country_code, tier_code, channel, priority, effective_from, effective_to, metadata_json
        from pricing_feature_flags
        where code = $1
          and effective_from <= $2
          and (effective_to is null or effective_to > $2)
          and (country_code = '' or country_code = $3)
          and (tier_code = '' or tier_code = $4)
          and (channel = '' or channel = $5)
        order by
          priority desc,
          effective_from desc,
          -- specificity tie-breakers
          (country_code = $3) desc,
          (tier_code = $4) desc,
          (channel = $5) desc
        limit 1
        """,
        code, now, cc, tc, ch,
    )
    return dict(row) if row else None


def _compose_mode(modes: list[str]) -> str:
    # strictest wins
    if "disabled" in modes:
        return "disabled"
    if "shadow" in modes:
        return "shadow"
    if "free" in modes:
        return "free"
    return "bill"


async def evaluate_gate(
    conn: asyncpg.Connection,
    *,
    module_code: str,   # e.g. "module.music"
    channel: str,       # web|mobile|api
    country_code: str,
    tier_code: str,
) -> GateDecision:
    """
    Applies 3 flags:
      - pricing.core
      - channel.<channel>   (e.g. channel.api)
      - module.<x>          (e.g. module.music)

    Also supports:
      - env kill switch PRICING_KILL_SWITCH=1
      - env per-module PRICING_ENABLE_MODULE_MUSIC=0
      - settings GLOBAL_BILLING_MODE_OVERRIDE
    """
    # Global override from config (ops lever)
    override = (settings.GLOBAL_BILLING_MODE_OVERRIDE or "").strip().lower()
    if override in {"disabled", "shadow", "free", "bill"}:
        return GateDecision(
            allowed=(override != "disabled"),
            billing_mode=override,
            reason="GLOBAL_BILLING_MODE_OVERRIDE",
            rule_code="config",
        )

    # Emergency kill switch
    if _env_bool("PRICING_KILL_SWITCH", False):
        return GateDecision(False, "disabled", "PRICING_KILL_SWITCH", "env")

    mod = (module_code or "").strip().lower()
    ch = _norm_channel(channel)

    env_key = "PRICING_ENABLE_" + mod.replace(".", "_").upper()
    if os.getenv(env_key) is not None and not _env_bool(env_key, True):
        return GateDecision(False, "disabled", f"{env_key}=0", "env")

    # DB flags (pricing_feature_flags)
    try:
        core = await _best_flag(conn, "pricing.core", country_code=country_code, tier_code=tier_code, channel=ch)
        chan = await _best_flag(conn, f"channel.{ch}", country_code=country_code, tier_code=tier_code, channel=ch)
        module = await _best_flag(conn, mod, country_code=country_code, tier_code=tier_code, channel=ch)
    except asyncpg.UndefinedTableError:
        # If flags table isn't migrated yet, default allow+bill.
        return GateDecision(True, "bill", "NO_FLAGS_TABLE", "default")
    except Exception:
        logger.exception("evaluate_gate failed; defaulting allow+bill")
        return GateDecision(True, "bill", "GATE_EVAL_FAILED_DEFAULT_BILL", "default")

    modes: list[str] = []
    reasons: list[str] = []

    def add(flag: Optional[dict], label: str) -> None:
        if not flag:
            return
        if not bool(flag.get("enabled", True)):
            modes.append("disabled")
            reasons.append(f"{label}:enabled=false")
            return
        m = _norm_mode(str(flag.get("billing_mode") or "bill"))
        modes.append(m)
        reasons.append(f"{label}:{m}")

    add(core, "core")
    add(chan, "channel")
    add(module, "module")

    mode = _compose_mode(modes) if modes else "bill"
    allowed = mode != "disabled"

    return GateDecision(
        allowed=allowed,
        billing_mode=mode,
        reason="|".join(reasons) if reasons else "default:bill",
        rule_code=mod,
    )