# svc-pricing Ownership Contract

## Goal
Prevent drift between plan, billing, entitlement, and wallet data by assigning a single writer for each concept and a single read model for billing UI.

## Canonical ownership

### 1. Product plan / tier entitlement
**Owned by:** `pricing_user_entitlements`
**Primary code owner:** `services/svc-pricing/app/app/repo/entitlements_repo.py`
**Write paths allowed:**
- free signup bootstrap
- admin/ops plan/tier override flows
- explicit plan migration tools

**Write paths forbidden:**
- billing UI shaping
- preview/reserve/commit routes
- payments catalog service

**Meaning:** current effective user tier for product access (`free`, `pro`, `business`, `enterprise`).

### 2. Paid subscription lifecycle facts
**Owned by:** `payment_plan_subscriptions`
**Primary code owner:** `services/svc-pricing/app/app/repo/payment_plan_subscriptions_repo.py`
**Write paths allowed:**
- Stripe / payment webhook sync
- subscription entitlement sync service

**Write paths forbidden:**
- signup bootstrap for free users
- UI overview builders
- pricing preview / reserve / commit routes

**Meaning:** gateway subscription truth, period boundaries, invoice state, cancel-at-period-end, paid plan code.

### 3. Billing policy / plan guardrails
**Owned by:** `billing_entitlements`
**Primary code owner:** `services/svc-pricing/app/app/repo/billing_entitlements_repo.py`
**Write paths allowed:**
- free signup bootstrap for free plan policy
- entitlement sync service for paid plan policy refresh
- explicit lifecycle transitions (upgrade/downgrade/inactivation)

**Write paths forbidden:**
- UI overview display calculations
- wallet balance updates
- preview/reserve/commit spending math

**Meaning:** billing mode, settlement mode, overage policy, wallet-topup allowed, hard-stop policy, included plan cap.

**Important rule:** `included_credits_remaining` is NOT the UI source of truth for spendable credits.
Treat it as a policy/projection field only.

### 4. Spendable wallet / reserved credits
**Owned by:** wallet summary derived from lots / ledger, surfaced through `v_pricing_account_overview.lots_json` and `pricing_credit_accounts`
**Primary code owners:**
- `services/svc-pricing/app/app/services/reservations/reservation_service.py`
- database projection `v_pricing_account_overview`
- `pricing_credit_accounts` as summary/cache

**Write paths allowed:**
- wallet grant / spend / release / commit flows
- reservation/finalize/release internals
- subscription credit grant logic

**Write paths forbidden:**
- billing overview service
- billing entitlements repo
- plan catalog service

**Meaning:** what the user can actually spend right now.

## Single read model for UI
**Canonical billing UI read model:** `v_pricing_account_overview` + current plan metadata

The billing UI must derive:
- available credits from lots / wallet summary
- reserved credits from lots / wallet summary
- total credits from plan cap / entitlement cap
- used credits as `total - available - reserved`

The billing UI must NOT derive available credits from `billing_entitlements.included_credits_remaining`.

## File-by-file contract

### `repo/entitlements_repo.py`
- Owns reads/writes for `pricing_user_entitlements` and `pricing_feature_flags`
- May write user tier and feature flag rows
- Must not write wallet balances or billing entitlements

### `repo/payment_plan_subscriptions_repo.py`
- Owns reads/writes for `payment_plan_subscriptions`
- May only be called by gateway / subscription sync flows
- Must not be used for free signup seeding

### `repo/billing_entitlements_repo.py`
- Owns reads/writes for `billing_entitlements`
- May update billing policy fields and plan cap fields
- Must not be used as a spendable balance source for UI
- Must not be used to infer wallet availability

### `services/entitlements/free_signup_bootstrap_service.py`
- Allowed to seed:
  - `pricing_user_entitlements`
  - `billing_entitlements`
  - wallet summary/account for free users
- Must not create `payment_plan_subscriptions` for normal free users
- Must preserve existing non-free rows
- Must never reset already-used free balances

### `services/entitlement_sync_service.py`
- Owns paid subscription -> entitlement reconciliation
- Allowed to upsert:
  - `payment_plan_subscriptions`
  - `billing_entitlements`
  - subscription cycle wallet grants
- Must not be the billing UI source

### `services/entitlement_service.py`
- Read-only decision layer for runtime entitlement evaluation
- May read `billing_entitlements`, `pricing_user_entitlements`, feature flags, tier defaults
- Must not mutate balances

### `services/reservations/reservation_service.py`
- Owns reserve / commit / release spending lifecycle
- Owns wallet summary updates and spendability math
- Is the canonical balance service for preview/reserve flows
- Must not rewrite plan/tier ownership tables except via explicit lifecycle helpers

### `api/routes/reservations.py`
- Orchestrates preview/reserve/commit/release
- May call bootstrap on first touch
- Must not compute billing UI credits from billing entitlements
- Must rely on reservation_service for balance truth

### `services/payments_catalog_service.py`
- Read-model shaper only
- Must not write any pricing truth tables
- Must consume the single read model for billing UI
- Must source `available` and `reserved` from wallet/lots, not billing entitlements

### `api/routes/payments.py`
- Public billing UI/API surface
- Must treat `build_payment_overview(...)` as shaping only
- Must not independently reconcile plan/wallet data in-route

## Invariants

1. If `current_plan_code = free` and wallet shows positive spendable credits, billing overview must show the same spendable credits.
2. `available_credits = spendable wallet credits`, never raw policy remaining credits.
3. `reserved_credits` must come from wallet reservation state, not inferred from policy tables.
4. `used_credits = max(total_credits - available_credits - reserved_credits, 0)` whenever `total_credits` exists.
5. Free signup must not create a `payment_plan_subscriptions` row.
6. `billing_entitlements` may describe plan cap and billing policy, but not override wallet-spendable truth in UI.
7. No route may write the same conceptual field through two different repos.

## Enforcement checklist

- Add unit tests for overview shaping using conflicting inputs:
  - wallet=100, entitlement remaining=0 => overview available must be 100
  - wallet balance 100, reserved 15 => available must be 85
- Add integration tests for:
  - register -> login -> payments overview
  - free signup first touch bootstrap
  - subscription upgrade -> overview
- Add a contributor note in each repo/service header describing owned tables and forbidden writes.
- Treat any new table as either:
  - canonical write model, or
  - read projection
  but never both.

## Recommendation on a new table
Do NOT add a bridge table just to reconcile conflicts.
If you add anything new, add a **read projection** only, such as `pricing_account_state`, and make it:
- written by one projector only
- never directly written by routes
- the exclusive source for billing UI



## IMPORTANT
billing_entitlements is policy/cap
pricing_credit_lots is spendable truth
pricing_credit_accounts is summary/cache
v_pricing_account_overview is the billing UI read model