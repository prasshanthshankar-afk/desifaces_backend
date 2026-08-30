#!/usr/bin/env python3
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECON = ROOT / "services/svc-pricing/app/app/services/entitlements/plan_credit_reconciliation_service.py"
WEBHOOK = ROOT / "services/svc-pricing/app/app/api/routes/payment_webhooks.py"
EXPECTED_BRANCH = "feature/v3-pricing-live-iap-ownership-20260830"


def sh(*args: str) -> None:
    subprocess.run(args, cwd=ROOT, check=True)


def out(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected 1 match, found {count}")
    return text.replace(old, new, 1)


def replace_in_section(text: str, start_marker: str, end_marker: str, old: str, new: str, *, label: str) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start + len(start_marker))
    section = text[start:end]
    section = replace_once(section, old, new, label=label)
    return text[:start] + section + text[end:]


def main() -> None:
    print("============================================================")
    print(" desifaces V3 PRICING — CREDIT CYCLE ISOLATION FIX")
    print("============================================================")
    branch = out("git", "branch", "--show-current")
    head = out("git", "rev-parse", "HEAD")
    print(f"branch={branch}")
    print(f"head={head}")
    if branch != EXPECTED_BRANCH:
        raise SystemExit(f"wrong branch: {branch}")

    webhook = WEBHOOK.read_text()
    webhook = replace_once(
        webhook,
        "from app.services.entitlements.plan_credit_reconciliation_service import (\n"
        "    reconcile_included_plan_credits,\n"
        ")\n",
        "from app.services.entitlements.plan_credit_reconciliation_service import (\n"
        "    reconcile_included_plan_credits,\n"
        ")\n"
        "from desifaces_shared.v3.subscription_cycle import stripe_cycle_key as canonical_stripe_cycle_key\n",
        label="canonical stripe cycle import",
    )
    webhook = replace_in_section(
        webhook,
        "async def _fetch_plan_credit_reconciliation_context(",
        "async def _reconcile_stripe_plan_credits_after_sync(",
        "    period_start = sub_dict.get(\"current_period_start\")\n"
        "    period_end = sub_dict.get(\"current_period_end\")\n"
        "    cycle_key = (\n"
        "        _metadata_cycle_key(ent_dict.get(\"metadata_json\"))\n"
        "        or _metadata_cycle_key(sub_dict.get(\"metadata_json\"))\n"
        "        or _stripe_cycle_key(plan_code=plan_code, period_start=period_start, period_end=period_end)\n"
        "    )\n",
        "    period_start = sub_dict.get(\"current_period_start\")\n"
        "    period_end = sub_dict.get(\"current_period_end\")\n"
        "    gateway_subscription_id = str(\n"
        "        sub_dict.get(\"gateway_subscription_id\") or subscription_id or \"\"\n"
        "    ).strip()\n"
        "    if not gateway_subscription_id or period_start is None or period_end is None:\n"
        "        raise RuntimeError(\"stripe_plan_credit_cycle_identity_missing\")\n"
        "    cycle_key = canonical_stripe_cycle_key(\n"
        "        gateway_subscription_id, period_start, period_end\n"
        "    )\n",
        label="stripe exact cycle identity",
    )
    WEBHOOK.write_text(webhook)

    recon = RECON.read_text()
    recon = replace_in_section(
        recon,
        "async def _fetch_active_included_cycle_totals(",
        "async def _expire_previous_cycle_included_lots(",
        "          and status = 'active'\n"
        "          and coalesce(metadata_json->>'cycle_key', $2::text) = $2::text\n",
        "          and status = 'active'\n"
        "          and (expires_at is null or expires_at > now())\n"
        "          and metadata_json->>'cycle_key' = $2::text\n",
        label="current-cycle totals predicate",
    )
    recon = replace_in_section(
        recon,
        "async def _expire_previous_cycle_included_lots(",
        "async def _adopt_legacy_included_lots_for_cycle(",
        "    cycle_key: str,\n"
        ") -> int:\n",
        "    cycle_key: str,\n"
        "    current_period_start: Optional[datetime],\n"
        ") -> int:\n",
        label="rollover signature",
    )
    recon = replace_in_section(
        recon,
        "async def _expire_previous_cycle_included_lots(",
        "async def _adopt_legacy_included_lots_for_cycle(",
        "          and status = 'active'\n"
        "          and (metadata_json ? 'cycle_key')\n"
        "          and coalesce(metadata_json->>'cycle_key', '') <> $2::text\n"
        "          and coalesce(reserved_amount, 0) = 0\n",
        "          and status = 'active'\n"
        "          and (\n"
        "            expires_at <= now()\n"
        "            or (\n"
        "              coalesce(metadata_json, '{}'::jsonb) ? 'cycle_key'\n"
        "              and coalesce(metadata_json->>'cycle_key', '') <> $2::text\n"
        "            )\n"
        "            or (\n"
        "              not (coalesce(metadata_json, '{}'::jsonb) ? 'cycle_key')\n"
        "              and $3::timestamptz is not null\n"
        "              and coalesce(granted_at, created_at) < $3::timestamptz\n"
        "            )\n"
        "          )\n"
        "          and coalesce(reserved_amount, 0) = 0\n",
        label="rollover stale legacy predicate",
    )
    recon = replace_in_section(
        recon,
        "async def _expire_previous_cycle_included_lots(",
        "async def _adopt_legacy_included_lots_for_cycle(",
        "        user_id,\n"
        "        cycle_key,\n"
        "    )\n",
        "        user_id,\n"
        "        cycle_key,\n"
        "        current_period_start,\n"
        "    )\n",
        label="rollover period arg",
    )
    recon = replace_in_section(
        recon,
        "async def _adopt_legacy_included_lots_for_cycle(",
        "async def _reduce_included_unspent(",
        "    source: str,\n"
        ") -> int:\n",
        "    source: str,\n"
        "    current_period_start: Optional[datetime],\n"
        ") -> int:\n",
        label="legacy adoption signature",
    )
    recon = replace_in_section(
        recon,
        "async def _adopt_legacy_included_lots_for_cycle(",
        "async def _reduce_included_unspent(",
        "          and status = 'active'\n"
        "          and not (metadata_json ? 'cycle_key')\n",
        "          and status = 'active'\n"
        "          and (expires_at is null or expires_at > now())\n"
        "          and not (coalesce(metadata_json, '{}'::jsonb) ? 'cycle_key')\n"
        "          and (\n"
        "            $5::timestamptz is null\n"
        "            or coalesce(granted_at, created_at) >= $5::timestamptz\n"
        "          )\n",
        label="legacy adoption safety predicate",
    )
    recon = replace_in_section(
        recon,
        "async def _adopt_legacy_included_lots_for_cycle(",
        "async def _reduce_included_unspent(",
        "        plan_code,\n"
        "        source,\n"
        "    )\n",
        "        plan_code,\n"
        "        source,\n"
        "        current_period_start,\n"
        "    )\n",
        label="legacy adoption period arg",
    )
    recon = replace_in_section(
        recon,
        "async def _reduce_included_unspent(",
        "async def reconcile_included_plan_credits(",
        "          and status = 'active'\n"
        "          and coalesce(metadata_json->>'cycle_key', $2::text) = $2::text\n",
        "          and status = 'active'\n"
        "          and (expires_at is null or expires_at > now())\n"
        "          and metadata_json->>'cycle_key' = $2::text\n",
        label="downgrade current-cycle predicate",
    )
    recon = replace_in_section(
        recon,
        "async def reconcile_included_plan_credits(",
        "    totals = await _fetch_active_included_cycle_totals",
        "        cycle_key=effective_cycle_key,\n"
        "    )\n"
        "    adopted_legacy = await _adopt_legacy_included_lots_for_cycle(\n"
        "        conn,\n"
        "        user_id=user_id,\n"
        "        cycle_key=effective_cycle_key,\n"
        "        plan_code=normalized_plan_code,\n"
        "        source=source,\n"
        "    )\n",
        "        cycle_key=effective_cycle_key,\n"
        "        current_period_start=current_period_start,\n"
        "    )\n"
        "    adopted_legacy = await _adopt_legacy_included_lots_for_cycle(\n"
        "        conn,\n"
        "        user_id=user_id,\n"
        "        cycle_key=effective_cycle_key,\n"
        "        plan_code=normalized_plan_code,\n"
        "        source=source,\n"
        "        current_period_start=current_period_start,\n"
        "    )\n",
        label="reconcile rollover/adoption calls",
    )
    RECON.write_text(recon)

    sh("python3", "-m", "compileall", "-q", str(WEBHOOK), str(RECON))
    sh(
        "python3",
        "-m",
        "pytest",
        "-q",
        "test/test_v3_pricing_native_iap_ownership.py",
        "test/test_v3_pricing_credit_cycle_isolation.py",
    )
    print("FOCUSED_REGRESSION=PASS")
    sh("git", "diff", "--check")
    sh("git", "diff", "--", str(WEBHOOK.relative_to(ROOT)), str(RECON.relative_to(ROOT)))
    sh("git", "add", str(WEBHOOK.relative_to(ROOT)), str(RECON.relative_to(ROOT)))
    sh("git", "commit", "-m", "fix: isolate plan credits by live subscription cycle")
    sh("git", "push", "origin", EXPECTED_BRANCH)
    print(f"PATCH_HEAD={out('git', 'rev-parse', 'HEAD')}")
    print("PATCH_PUSH=PASS")


if __name__ == "__main__":
    main()
