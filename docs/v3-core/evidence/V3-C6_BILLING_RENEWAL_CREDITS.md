# V3-C6 Billing and Renewal Credit Integrity

Change-ID: `V3-C6`
Status: `READY_FOR_RUNTIME_CERTIFICATION`
Owner: `#v3-core`
Date: `2026-08-18`
Backend V2 evidence anchor: `70a80cef08cebb8f02385a8e0f1adbac7c85fbb8`
C3 baseline: `61876055c5f43cdd5032e6eadd37da5ab24a9ec4`
Implementation branch: `feature/v3-c4-c6-foundation-closure-20260818`

## 1. Requirement

Close the known subscription-renewal credit risk before enhancement development accelerates. For every provider-confirmed active subscription billing period, included plan credits must be represented exactly once for that cycle, purchased/top-up credits must be preserved, credit lots must belong to the canonical billing account, duplicate provider callbacks must be idempotent, and an integrity sweep must repair a missed cycle-credit reconciliation without inventing a provider renewal.

## 2. EIP source

EIP repository: `prasshanthshankar-afk/desifaces-eos`
EIP ref: `feature/eos-foundation`
Primary standard: `ekb/06-integration/Integration_Architecture_Standard.md`

Relevant EIP rules:
- payment/webhook handling must be replay-safe and idempotent;
- backend pricing/credits/entitlements are server authoritative;
- retries must not duplicate financial or AI side effects;
- provider-specific data is normalized behind canonical backend state;
- operational reconciliation must be observable rather than silently swallowing drift.

## 3. V2 current-state evidence

The source already contains substantial correct billing behavior:
- `sync_subscription_and_entitlement()` persists Stripe subscription/entitlement state and calls `reconcile_included_plan_credits()` after the billing period is known;
- Apple and Google native confirmation flows invoke subscription-cycle credit synchronization;
- `df_sync_subscription_cycle_credits(uuid)` is idempotent and specifically uses Google `current_period_end` because a Google purchase token/original start can remain constant while renewal expiry advances;
- `reconcile_included_plan_credits()` preserves purchased/top-up lots and uses cycle identity/source refs to prevent double grants;
- gateway webhook event persistence already records provider event identity/status.

The remaining integrity gaps are:
- there was no provider-neutral sweep to repair an already-persisted active period when the cycle-credit step was missed/interrupted;
- legacy credit-lot inserts can leave `billing_account_id` null even after C3 made AccountRef mandatory;
- V3 development intentionally keeps the subscription reconciler disabled, so C6 must not accidentally activate it during certification.

## 4. Evidence gaps

Before certification:
- prove every existing user-owned credit lot resolves to a billing account after the migration;
- prove a cycle with spent included credits rolls to the next cycle with the full new allowance;
- prove duplicate processing of the same new cycle does not double-grant;
- prove purchased/top-up remaining/reserved values do not change;
- prove the provider-neutral integrity sweep can execute against already-persisted active subscription rows without external provider calls;
- prove certification changes are rolled back;
- prove V3 background subscription reconciler remains disabled.

## 5. V3 disposition

Disposition: `PRESERVE PROVIDER FLOWS + HARDEN CANONICAL INTEGRITY`.

Existing Stripe, Apple and Google public/provider callback contracts remain. C6 does not invent a replacement billing provider abstraction at the transport layer. It strengthens the canonical credit-lot/account/cycle invariants underneath existing flows.

## 6. #v3-core architecture decision

1. `pricing_credit_lots` remains the source of truth for spendable credit buckets.
2. `billing_entitlements` remains plan/entitlement metadata; it is not a substitute for live spendable lot totals.
3. Every user-owned credit lot MUST carry canonical `billing_account_id`.
4. Included plan credits are cycle-scoped and provider-period-driven.
5. A renewal is recognized only after the provider period has been persisted as active; C6 never advances or fabricates a provider billing period.
6. Stripe cycle identity reconstructs the same `<subscription>:<period_start_epoch>:<period_end_epoch>` key used by entitlement sync.
7. Google native cycle identity uses current period end; Apple uses the current period start consistent with the existing native synchronization function.
8. Same cycle processing is idempotent. A new period creates/true-ups the new cycle; a repeated callback for that period does not duplicate the allowance.
9. Purchased/top-up lots are never consumed, expired or reset by plan-cycle reconciliation.
10. A provider-neutral integrity sweep runs after the existing subscription reconciler when that reconciler is explicitly enabled.
11. The integrity sweep defaults enabled only within an enabled parent reconciler and has an independent emergency-disable flag.
12. V3 development retains `DF_SUBSCRIPTION_RECONCILER_ENABLED=false` until production-readiness activation is explicitly certified.

## 7. Contract impact

No breaking public API change. C6 strengthens persistence and operational reconciliation. C3 PricingQuote/CreditReservation/CreditTransaction contracts remain authoritative for generation billing.

## 8. Database impact

Migration: `migrations/2026_08_18_v3_subscription_credit_integrity.sql`

Adds:
- `df_v3_resolve_user_billing_account_id(uuid)`;
- trigger that fills/requires billing-account identity for future user-owned `pricing_credit_lots` rows;
- backfill for existing null lot account IDs;
- `v3_subscription_credit_reconciliation` audit table.

Existing amounts, purchased lots and subscription provider records are not rewritten beyond missing account linkage.

## 9. Security and privacy impact

Financial state is explicitly account-owned. The integrity sweep processes only persisted active subscriptions and does not call provider APIs or accept arbitrary client identity.

## 10. Pricing/entitlement/credit impact

This is the primary C6 scope.

Required invariants:
- full included allowance per confirmed billing cycle subject to plan cap;
- no duplicate grant for repeated same-cycle processing;
- previous-cycle included credits expire/roll according to existing reconciler policy;
- in-flight reserved included credits remain protected by existing reconciliation logic;
- purchased/top-up lots survive renewals and plan transitions;
- legacy aggregate account is rebuilt from canonical lots by existing services.

## 11. Provider/model impact

Providers: Stripe, Apple IAP, Google Play.

C6 does not alter provider purchase verification. It consumes provider periods already persisted by existing verification/webhook/restore flows.

## 12. Implementation scope

- `migrations/2026_08_18_v3_subscription_credit_integrity.sql`
- `services/shared/python/desifaces_shared/v3/subscription_cycle.py`
- `services/svc-pricing/app/app/services/subscription_credit_integrity_service.py`
- `services/svc-pricing/app/app/services/subscription_reconciler_loop.py`
- `services/svc-pricing/app/app/tools/v3_c6_certify.py`
- focused unit/runtime tests

## 13. Compatibility / migration strategy

Current mobile/web purchase, restore, confirmation and provider-notification routes remain unchanged. The new integrity layer is additive and uses current persisted provider subscription rows.

The V3 DB is certified first. Production enablement later requires deploying the certified code/migrations and explicitly enabling/monitoring the reconciler according to the production runbook. No V2 production billing state is changed during V3 certification.

## 14. Test and certification plan

Certification requires:
- focused V3 contract/unit tests pass;
- C6 migration applies only to V3 DB;
- zero user-owned credit lots with null billing account;
- synthetic cycle 1 funded;
- synthetic included-credit spend recorded in transaction;
- synthetic cycle 2 replenishes to full plan cap;
- duplicate cycle 2 leaves totals unchanged;
- purchased/top-up totals remain exactly unchanged;
- provider-neutral integrity sweep executes successfully against cloned active subscription state;
- certification transaction rolls back completely;
- V3 Pricing API remains healthy;
- `DF_SUBSCRIPTION_RECONCILER_ENABLED=false` remains in V3 development;
- no V3 worker/scheduler is activated.

Status becomes `CERTIFIED` only after runtime evidence passes.
