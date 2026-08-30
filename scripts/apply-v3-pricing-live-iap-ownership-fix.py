#!/usr/bin/env python3
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

BRANCH = "feature/v3-pricing-live-iap-ownership-20260830"
ROOT = Path(__file__).resolve().parents[1]
PAYMENTS = ROOT / "services/svc-pricing/app/app/api/routes/payments.py"
TEST = ROOT / "test/test_v3_pricing_native_iap_ownership.py"

REPLACEMENTS = {
    '    return _subscription_provider(row) == "apple_iap"\n': (
        '    return (\n'
        '        _subscription_provider(row) == "apple_iap"\n'
        '        and _subscription_row_is_live(row)\n'
        '    )\n'
    ),
    '    return _subscription_provider(row) == "google_play"\n': (
        '    return (\n'
        '        _subscription_provider(row) == "google_play"\n'
        '        and _subscription_row_is_live(row)\n'
        '    )\n'
    ),
    '    return _subscription_provider(row) in {"apple_iap", "google_play"}\n': (
        '    return (\n'
        '        _subscription_provider(row) in {"apple_iap", "google_play"}\n'
        '        and _subscription_row_is_live(row)\n'
        '    )\n'
    ),
}


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, text=True, check=check)


def output(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def validate_helpers() -> None:
    tree = ast.parse(PAYMENTS.read_text(encoding="utf-8"), filename=str(PAYMENTS))
    wanted = {
        "_subscription_row_is_live",
        "_is_apple_managed_subscription",
        "_is_google_play_managed_subscription",
        "_is_native_iap_managed_subscription",
    }
    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    if {node.name for node in selected} != wanted:
        raise RuntimeError("ownership helper set is incomplete")

    namespace = {
        "Any": object,
        "Dict": dict,
        "Optional": __import__("typing").Optional,
        "_subscription_provider": lambda row: str((row or {}).get("gateway_provider") or "").strip().lower(),
    }
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(PAYMENTS), "exec"), namespace)

    stale_apple = {
        "gateway_provider": "apple_iap",
        "subscription_state": "canceled",
        "entitlement_state": "inactive",
        "cancel_at_period_end": False,
    }
    stale_google = dict(stale_apple, gateway_provider="google_play")
    live_apple = dict(stale_apple, subscription_state="active", entitlement_state="active")
    live_google = dict(live_apple, gateway_provider="google_play")

    assert namespace["_is_apple_managed_subscription"](stale_apple) is False
    assert namespace["_is_google_play_managed_subscription"](stale_google) is False
    assert namespace["_is_native_iap_managed_subscription"](stale_apple) is False
    assert namespace["_is_native_iap_managed_subscription"](stale_google) is False
    assert namespace["_is_apple_managed_subscription"](live_apple) is True
    assert namespace["_is_google_play_managed_subscription"](live_google) is True


def main() -> int:
    print("============================================================")
    print(" desifaces V3 PRICING — LIVE NATIVE IAP OWNERSHIP FIX")
    print("============================================================")

    branch = output("git", "branch", "--show-current")
    print(f"branch={branch}")
    print(f"head={output('git', 'rev-parse', 'HEAD')}")
    if branch != BRANCH:
        raise RuntimeError(f"expected branch {BRANCH}, found {branch}")

    if subprocess.run(["git", "diff", "--quiet", "--"], cwd=ROOT).returncode != 0:
        raise RuntimeError("tracked working tree has modifications; refusing ambiguous patch")

    source = PAYMENTS.read_text(encoding="utf-8")
    for old, new in REPLACEMENTS.items():
        count = source.count(old)
        if count != 1:
            raise RuntimeError(f"expected exactly one ownership expression, found {count}: {old.strip()}")
        source = source.replace(old, new, 1)
    PAYMENTS.write_text(source, encoding="utf-8")

    run(sys.executable, "-m", "py_compile", str(PAYMENTS))
    validate_helpers()
    print("STATIC_REGRESSION=PASS")

    diff = output("git", "diff", "--", str(PAYMENTS.relative_to(ROOT)))
    if "_subscription_row_is_live(row)" not in diff:
        raise RuntimeError("expected live-subscription guard missing from diff")
    print(diff)

    run("git", "add", str(PAYMENTS.relative_to(ROOT)))
    run("git", "commit", "-m", "fix: require live native IAP subscription ownership")
    run("git", "push", "origin", BRANCH)

    print(f"PATCH_HEAD={output('git', 'rev-parse', 'HEAD')}")
    print("PATCH_PUSH=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
