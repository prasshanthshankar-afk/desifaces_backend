# V3-C3 Critical Creator Path Certification

Change-ID: `V3-C3`
Status: `CERTIFIED_CRITICAL_CREATOR_PATH`
Owner: `#v3-core`
Date: `2026-08-17`
Branch: `feature/v3-c3-canonical-adapters-20260817`

## 1. Scope

This evidence record certifies the desifaces-v3 canonical compatibility path for:

`Face -> Audio -> Fusion -> Pricing/Credits`

The certification covers the compatibility-adapter boundary and read-only authenticated runtime mapping. It does not enable V3 execution workers or provider calls.

## 2. Certified architecture

The frozen migration direction is:

`Existing V2-compatible HTTP contract -> V3 compatibility adapter -> canonical V3 request/domain contracts -> capability/service implementation -> compatibility response`

The certified canonical vocabulary includes:

- `RequestContext`
- `RequestActor`
- `AccountRef` / canonical account identity
- `GenerationRequest`
- `GenerationJob`
- `ProviderExecution`
- `MediaAsset`
- `SafetyDecision`
- `PricingQuote`
- `CreditReservation`
- `CreditTransaction`

`CreditReservation` is now a first-class V3 shared contract. This supersedes the earlier provisional wording in the initial adapter matrix that treated it as optional/future.

## 3. Public compatibility proof

For each critical capability, the existing public V2 API surface remains available in the V3 API runtime and the hidden V3 adapter probe is excluded from OpenAPI.

Runtime shell certification results:

| Capability | Health | Hidden probe protection | V2/V3 OpenAPI `.paths` parity | V3 shadow enabled | Execution guard |
|---|---|---|---|---|---|
| Face | PASS | PASS (`401` without auth) | PASS | PASS | PASS |
| Audio | PASS | PASS (`401` without auth) | PASS | PASS | PASS |
| Fusion | PASS | PASS (`401` without auth) | PASS | PASS | PASS |
| Pricing | PASS | PASS (`401` without auth) | PASS | PASS | PASS |

Additional runtime guards:

- `FUSION_RECOVERY_ENABLED=false`
- `DF_SUBSCRIPTION_RECONCILER_ENABLED=false`
- no `df-v3-*worker*` or `df-v3-*scheduler*` container was active during certification

## 4. Unit/contract proof

Focused adapter test suites were executed before runtime certification.

Observed checkpoints:

- Face/shared/account adapter checkpoint: `26 passed`
- Audio-inclusive checkpoint: `28 passed`
- Face + Audio + Fusion + shared/account checkpoint: `45 passed`

Pricing adapter tests were subsequently added to the repository/CI contract suite. The critical-path authenticated runtime sweep below is the final cross-capability certification gate.

## 5. Canonical account-context defect discovered during certification

The initial authenticated sweep correctly failed because the cloned V3 database contained:

- users: `6`
- billing accounts: `0`
- active billing accounts: `0`
- active memberships: `0`
- credit-account billing links: `0`
- `user:<uuid>` billing accounts: `0`

This was not bypassed or hidden.

EIP/repository evidence showed:

1. `migrations/2026_03_11_billing_accounts_invoices.sql` defined billing-account tables and a one-time backfill for existing credit accounts.
2. The later free-signup pricing bootstrap could create `pricing_credit_accounts` without creating/linking a canonical billing account.
3. Therefore users created after the original one-time backfill could legitimately have pricing state without account context.

The V3 repair was implemented as:

`migrations/2026_08_18_v3_canonical_account_context.sql`

The repair is idempotent and preserves existing ownership when present. It creates only missing canonical account context and links null account references.

## 6. Account-context migration execution proof

The V3-only migration executed successfully against `desifaces_v3` and completed with `COMMIT`.

Observed execution included:

- function creation
- data repair updates
- future-user/account-context trigger creation
- future-credit-account/account-context trigger creation
- successful transaction commit

No V2 database was targeted by this migration.

## 7. Authenticated cross-capability certification

The repository certification script:

`scripts/v3-c3-authenticated-adapter-certify.sh`

was executed after the canonical account-context repair.

It selected an existing V3 user/account context and invoked only hidden, read-only adapter probes. It did not create generation jobs, execute providers, reserve/commit/release credits, create media, or enable workers.

Final observed result:

```text
FACE_AUTHENTICATED_MAPPING=PASS
AUDIO_AUTHENTICATED_MAPPING=PASS
FUSION_AUTHENTICATED_MAPPING=PASS
PRICING_AUTHENTICATED_MAPPING=PASS
C3_SHARED_USER_ACCOUNT_CONTEXT=PASS
C3_PERSISTENCE_INVARIANTS=PASS
C3_EXECUTION_GUARD=PASS
V3_C3_AUTHENTICATED_CRITICAL_PATH_CERTIFICATION=PASS
```

## 8. Meaning of the authenticated PASS

The final PASS proves that one real V3 cloned user can traverse all four compatibility adapters and resolve to one consistent canonical account/user identity.

Specifically:

- Face maps to `GenerationRequest(kind=face)` with canonical user/account identity.
- Audio maps to `GenerationRequest(kind=audio)` with the same canonical user/account identity.
- Fusion maps to `GenerationRequest(kind=fusion)` with the same canonical user/account identity.
- Pricing maps a compatibility preview into canonical `PricingQuote` with the same user/account identity and stable fingerprint/quote translation.
- selected-user job/media/pricing reservation/ledger/balance state is identical before and after the certification run.
- V3 execution workers/schedulers remain disabled.

## 9. Pricing/accounting rules certified at the adapter boundary

The following rules are frozen:

1. `/api/pricing/quote` remains the compatibility transparency/entitlement view during migration.
2. `/api/pricing/reservations/preview` is the authoritative generation quote lifecycle because it carries quote identity, fingerprint and expiry.
3. Legacy `qt_*` quote IDs are compatibility identifiers, not canonical UUID quote identity.
4. Only the Pricing bridge may deterministically issue canonical UUID quote identity from the legacy fingerprint/account/user tuple.
5. `CreditReservation` is first-class canonical V3 state.
6. service `billed_units` are not automatically equal to charged credits.
7. immutable `CreditTransaction` consumption must be based on settlement/ledger evidence such as charged credits and balance-after.
8. reserve/commit/release paths must remain idempotent/replay-safe.

## 10. Media/provider rules certified at the adapter boundary

The following rules are frozen:

- raw compatibility URLs/artifact IDs are not silently promoted to canonical `MediaAsset` identity.
- provider hints/status belong outside generic business request state and ultimately map to `ProviderExecution`.
- Fusion aliases are collapsed before the canonical generation boundary.
- internal longform child-render billing suppression remains orchestration metadata and is not a customer-facing billing mode.
- V3-C4 remains responsible for the physical/canonical media-lineage migration and durable media lifecycle.

## 11. Compatibility guarantees

During V3 migration:

- existing certified V2 public endpoints remain compatibility contracts unless an explicit `#v3-core` deprecation decision is recorded.
- canonical V3 contracts remain provider-neutral and reject compatibility aliases as permanent generic-domain fields.
- the current mobile/web client does not require endpoint renames merely to adopt canonical V3 internals.
- V2 production runtime/data remain unaffected by this V3 certification.

## 12. Critical-path certification status

The critical creator-path portion of V3-C3 is certified:

- canonical shared primitives: `PASS`
- Face adapter: `PASS`
- Audio adapter: `PASS`
- Fusion adapter: `PASS`
- Pricing bridge: `PASS`
- canonical account context: `PASS`
- authenticated shared identity across all four: `PASS`
- persistence invariants during probes: `PASS`
- public API compatibility: `PASS`
- V3 execution guard: `PASS`

## 13. Remaining V3-C3 scope

This certification does not by itself close the entire API-inventory milestone.

Before declaring all of V3-C3 complete, `#v3-core` must finish the remaining route inventory and caller evidence for non-critical-path families and freeze explicit decisions for:

- legacy/duplicate routes eligible for deprecation
- admin/catalog-sync/provider-control routes to internalize
- notification/support/help/dashboard compatibility families
- music/commerce/marketing route families
- longform/Fusion Extension ownership boundaries
- webhook/service-only routes
- pricing summary/fallback aliases
- health/readiness standardization

Once that remaining inventory is frozen, V3-C3 can close and V3-C4 Media Lifecycle can begin.

## 14. Freeze statement

The Face -> Audio -> Fusion -> Pricing/Credits canonical adapter path is `CERTIFIED` for V3 compatibility/runtime-shell mapping.

The result is not provisional: the adapters, canonical identity model, canonical quote/reservation/transaction rules, public compatibility strategy, and read-only authenticated cross-capability mapping are now frozen under `#v3-core` until explicitly reconsidered.
