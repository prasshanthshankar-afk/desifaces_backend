# V3-C3 Canonical Account Context Gap and Repair

Change-ID: `V3-C3-ACCOUNT-CONTEXT`
Status: `REPAIR_READY_FOR_V3_DB_APPLICATION`
Owner: `#v3-core`
Date: `2026-08-17`

## 1. Requirement / question

V3-C3 authenticated cross-capability certification requires Face, Audio, Fusion, and Pricing compatibility adapters to resolve the same durable V3 `AccountRef` and `UserRef` for an existing cloned user without mutating generation, media, reservation, ledger, or credit state.

The authenticated certification script failed before invoking any adapter with:

```text
C3_CERT_FAIL=no_active_v3_user_account_context
users=6 billing_accounts=0 active_billing_accounts=0 active_memberships=0 credit_account_links=0 user_code_accounts=0
```

The failure therefore represents an account-persistence lifecycle gap, not an adapter or authentication failure.

## 2. EIP / code evidence inspected

### Existing billing-account schema and intended backfill

`migrations/2026_03_11_billing_accounts_invoices.sql` already defines:

- `pricing_billing_accounts`
- `pricing_billing_account_members`
- `pricing_credit_accounts.billing_account_id`
- billing-account linkage on reservations, ledger events, and pricing entitlements
- an existing-user backfill that creates an individual prepaid account with `account_code = 'user:' || user_id`
- an owner/default membership
- linkage from `pricing_credit_accounts` to the generated billing account.

The migration explicitly labels this section `Backfill default billing accounts for existing users`.

### Current canonical resolver

`services/shared/python/desifaces_shared/identity/account_context.py` resolves account context in this order:

1. active/default billing-account membership
2. active account linked from `pricing_credit_accounts`
3. active individual account with `account_code = user:<uuid>`

The resolver deliberately does not synthesize an account UUID if persistence is missing.

### Current free-signup bootstrap gap

`services/svc-pricing/app/app/services/entitlements/free_signup_bootstrap_service.py` can insert `pricing_credit_accounts` with:

- `user_id`
- `balance_credits`
- `reserved_credits`
- `settlement_mode`
- `updated_at`

but does not populate `billing_account_id` and does not create `pricing_billing_accounts` / membership rows.

This means users created or pricing-bootstrapped after the one-time March backfill can legitimately remain without canonical account context.

## 3. Current-state interpretation

The V3 clone currently contains six `core.users` rows and zero billing-account rows. This is consistent with the lifecycle gap above: schema exists, but account-row creation was not a durable invariant after the original migration executed.

No evidence indicates corruption of the six user identities themselves. No adapter call, provider execution, pricing reservation, credit settlement, or media mutation occurred during the failed certification attempt.

## 4. V3 impact

The gap blocks a foundational V3 invariant:

> Every registered V3 user must have a durable canonical account context suitable for ownership and billing, independent of whether a compatibility endpoint still exposes a user-centric V2 shape.

Without this invariant:

- `GenerationRequest.account_id` cannot be populated truthfully.
- `PricingQuote.account_id`, `CreditReservation.account_id`, and `CreditTransaction.account_id` cannot be guaranteed.
- Media ownership cannot reliably migrate from user-only to account-aware ownership.
- Business/enterprise account evolution would be built on an incomplete personal-account baseline.

## 5. #v3-core decision

V3 will enforce canonical account context as a database invariant.

The repair is implemented by:

`migrations/2026_08_18_v3_canonical_account_context.sql`

The migration is additive and idempotent.

### Resolution behavior

For a user, V3 preserves existing ownership in this order:

1. existing active billing-account membership
2. existing active account already linked from `pricing_credit_accounts`
3. existing active `user:<uuid>` personal account
4. otherwise create a new active individual/prepaid/USD personal account using `user:<uuid>`.

No existing explicit account ownership is overwritten.

### Existing-data repair

The migration resolves/creates account context for every existing `core.users` row and fills currently-null `billing_account_id` values on supported user-scoped pricing persistence, including:

- `pricing_credit_accounts`
- `pricing_credit_reservations`
- `pricing_credit_ledger_events`
- `pricing_user_entitlements`
- `pricing_credit_lots` when the table/column exists.

It does not change credit balances, reserved-credit amounts, ledger deltas, subscription state, entitlement amounts, jobs, or media.

### Future-user invariant

An `AFTER INSERT` trigger on `core.users` ensures newly registered V3 users receive durable account context immediately.

### Future-credit-account invariant

A trigger on `pricing_credit_accounts` prevents a newly inserted or explicitly nulled credit account from remaining detached from canonical account ownership.

## 6. Why this is not synthetic certification data

The repair does not create a fake test user or fake account solely for certification.

It establishes the account records required by the frozen V3 domain model for the existing cloned users and future V3 users. The account code convention and individual/prepaid defaults already come from the previously approved billing-account migration.

The certification script will continue to select a real existing cloned user after the repair.

## 7. Compatibility and migration impact

- V2 production is unaffected; this change exists on the isolated V3 branch and is to be applied only to the V3 database.
- Existing user UUIDs remain unchanged.
- Existing balances and reserved credits remain unchanged.
- Existing subscription/provider identities remain unchanged.
- Existing billing-account relationships, if present in future environments, take precedence over creation of a personal fallback account.
- `user:<uuid>` remains the canonical personal-account compatibility convention established by the March billing migration.

## 8. Certification gates after applying the migration

Before declaring the critical C3 path certified:

1. V3 DB must report account context for the existing cloned users.
2. The authenticated adapter certification must successfully map Face, Audio, Fusion, and Pricing for one real cloned user.
3. All four mappings must resolve the same `user_id` and `account_id`.
4. Before/after counts and balances for the selected user must remain unchanged by the read-only adapter probes.
5. No V3 execution worker/scheduler may be active.
6. No provider call or pricing reserve/commit/release may occur.

## 9. Evidence still required

After migration application, record:

- user count
- billing-account count
- active membership count
- credit-account link count
- authenticated Face/Audio/Fusion/Pricing mapping results
- persistence invariant result
- execution guard result.

## 10. Freeze statement

The absence of billing-account rows in the V3 clone is a real persistence lifecycle gap caused by one-time historical backfill combined with later user/credit bootstrap behavior.

V3 will not synthesize account IDs inside adapters and will not create fake certification users. Canonical account context is instead established as durable database state, preserving existing ownership where present and providing an individual personal account for users that otherwise have no account context.
