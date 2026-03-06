# services/svc-pricing/app/app/services/engine/module_gate.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    billing_mode: str  # disabled|shadow|free|bill
    reason: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _compose_mode(modes: list[str]) -> str:
    # strictest wins
    if "disabled" in modes:
        return "disabled"
    if "shadow" in modes:
        return "shadow"
    if "free" in modes:
        return "free"
    return "bill"


async def _best_flag(
    conn: asyncpg.Connection,
    code: str,
    *,
    country_code: str,
    tier_code: str,
    channel: str,
) -> Optional[dict]:
    """
    Pick best matching flag by:
      - code match
      - effective window
      - enabled/billing_mode
      - highest priority
      - most recent effective_from
      - best scope match (we use scopes but also allow simple matching by columns)
    """
    now = _now()
    rows = await conn.fetch(
        """
        select code, enabled, billing_mode, scope, country_code, tier_code, channel, priority, effective_from, effective_to, metadata_json
        from pricing_feature_flags
        where code = $1
          and effective_from <= $2
          and (effective_to is null or effective_to > $2)
          and (
              (country_code = '' or country_code = $3) and
              (tier_code = '' or tier_code = $4) and
              (channel = '' or channel = $5)
          )
        order by
          priority desc,
          effective_from desc,
          -- tie-breakers: more specific wins
          (country_code = $3) desc,
          (tier_code = $4) desc,
          (channel = $5) desc
        limit 1
        """,
        code, now, country_code or "", tier_code or "", channel or "",
    )
    if not rows:
        return None
    return dict(rows[0])


async def evaluate_gate(
    conn: asyncpg.Connection,
    *,
    module_code: str,          # e.g. "module.music"
    channel: str,              # web|mobile|api
    country_code: str,
    tier_code: str,
) -> GateDecision:
    """
    Applies:
      - pricing.core
      - channel.<channel>
      - module.<module>
    Global override via settings.GLOBAL_BILLING_MODE_OVERRIDE.
    """
    # Global override (ops lever)
    override = (settings.GLOBAL_BILLING_MODE_OVERRIDE or "").strip().lower()
    if override in {"disabled", "shadow", "free", "bill"}:
        allowed = override != "disabled"
        return GateDecision(allowed=allowed, billing_mode=override, reason="GLOBAL_OVERRIDE")

    # If feature flags table doesn't exist yet, default to bill.
    try:
        core = await _best_flag(conn, "pricing.core", country_code=country_code, tier_code=tier_code, channel=channel)
        ch = await _best_flag(conn, f"channel.{channel}", country_code=country_code, tier_code=tier_code, channel=channel)
        mod = await _best_flag(conn, module_code, country_code=country_code, tier_code=tier_code, channel=channel)
    except asyncpg.UndefinedTableError:
        return GateDecision(allowed=True, billing_mode="bill", reason="NO_FLAGS_TABLE")

    modes: list[str] = []
    reasons: list[str] = []

    def add_flag(flag: Optional[dict], label: str) -> None:
        if not flag:
            return
        # enabled=false always behaves like disabled
        if not bool(flag.get("enabled", True)):
            modes.append("disabled")
            reasons.append(f"{label}:disabled")
            return
        m = str(flag.get("billing_mode") or "bill").lower()
        if m not in {"disabled", "shadow", "free", "bill"}:
            m = "bill"
        modes.append(m)
        reasons.append(f"{label}:{m}")

    add_flag(core, "core")
    add_flag(ch, "channel")
    add_flag(mod, "module")

    mode = _compose_mode(modes) if modes else "bill"
    allowed = mode != "disabled"

    return GateDecision(allowed=allowed, billing_mode=mode, reason="|".join(reasons))