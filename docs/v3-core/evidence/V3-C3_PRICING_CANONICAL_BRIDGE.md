# V3 EIP Evidence Record — Pricing Canonical Bridge

Change-ID: `V3-C3-PRICING-BRIDGE`
Status: `READY_FOR_RUNTIME_CERTIFICATION`
Owner: `#v3-core`
Date: `2026-08-17`

## 1. Requirement

Implement the V3-C3 Pricing compatibility bridge for the critical Face → Audio → Fusion → Pricing path while preserving the certified V2 public Pricing HTTP contract.

The bounded change establishes:

- canonical `PricingQuote` issuance from the current generation pricing preview contract;
- a first-class canonical `CreditReservation` contract because reservation identity/state crosses Face, Audio and Fusion service boundaries;
- canonical mapping of reserve state;
- immutable `CreditTransaction` construction from settlement/ledger evidence;
- deterministic canonical UUID quote identity for legacy `qt_*` quote IDs, issued only by the Pricing bridge;
- a hidden authenticated read-only V3-only pricing mapping probe;
- no reservation, commit, release, payment, subscription, bootstrap or ledger mutation from the probe;
- no public OpenAPI path change.

## 2. EIP source

- EIP repository: `prasshanthshankar-afk/desifaces-eos`.
- EIP ref: `feature/eos-foundation`.
- Exact current repository/runtime evidence:
  - `services/svc-pricing/app/app/api/routes/pricing.py`;
  - `services/svc-pricing/app/app/api/routes/reservations.py`;
  - `services/svc-pricing/app/app/services/reservations/reservation_service.py`;
  - `services/svc-pricing/app/app/api/deps.py`;
  - `services/svc-pricing/app/app/main.py`;
  - canonical `services/shared/df_contracts/v3/commerce.py`;
  - V3-C3 Face/Audio/Fusion adapter evidence and runtime-shell certification.

The current EIP foundation does not yet provide symbol-level Pricing evidence at the freshness of the live repository. This record therefore uses direct exact-branch code/runtime evidence under the established EIP hard-gate policy.

## 3. V2 current-state evidence

### Public quote surface

`POST /api/pricing/quote` accepts `variant_code`, `params`, `channel`, optional currency and country. It evaluates the module gate and computes transparent credits/money/line items, including blocked-module transparency and operational economics.

Its `QuoteOut` response contains entitlement/billing mode, variant/module, currency, pricebook, credits, money, shadow totals, economics and lines, but it does **not** itself issue quote ID, fingerprint or expiry.

### Generation pricing lifecycle

`POST /api/pricing/reservations/preview` is the current generation-facing preview lifecycle. It computes:

- `preview_fingerprint` as SHA-256 over stable preview material;
- legacy `quote_id = "qt_" + preview_fingerprint[:24]`;
- `quote_expires_at = now + 15 minutes`;
- quoted credits/money/pricebook context in `quote_breakdown`;
- balance/available-credit context;
- entitlement/billing-account/settlement context.

The reserve/commit/release routes are:

- `POST /api/pricing/reservations/reserve`;
- `POST /api/pricing/reservations/commit`;
- `POST /api/pricing/reservations/release`.

### Reservation and settlement

`reservation_service.py` defines `ReservationView` and `FinalizeReceipt`. `FinalizeReceipt` exposes `charged_credits`, `charged_money`, `balance_before`, `reserved_before`, `balance_after`, `reserved_after`, and `available_after`.

The public commit response exposes `billed_units`, amount/currency and optional ledger entry ID, but service units are not necessarily credit units. Therefore canonical credit consumption must use finalize/ledger evidence rather than infer credits from `billed_units`.

### Ledger identity

Current Pricing code stores immutable ledger events in `pricing_credit_ledger_events` and looks up the committed ledger event by idempotency key. This is the source identity for canonical consumption transactions when available.

### Billing account

Current Pricing contains its own billing-account resolution logic over `pricing_billing_account_members`, `pricing_credit_accounts.billing_account_id`, and `pricing_billing_accounts`. V3 shared adapters already introduced `desifaces_shared.identity.resolve_account_context`; the V3 Pricing probe reuses that shared canonical resolver instead of introducing a fourth resolution implementation.

### Authentication

Current Pricing `AuthContext` uses bearer auth plus `X-User-Id`, with an explicitly documented optional JWT-sub fallback that does not verify the signature. V3-C3 does not redesign public Pricing auth in this bounded change; the hidden V3 probe reuses the existing Auth dependency and stays loopback-only in the isolated V3 runtime. Auth hardening remains a shared identity/security concern under `#v3-core`.

## 4. Evidence gaps

- Pricing bridge unit tests have not yet run on the Azure V3 workspace.
- The V3 Pricing image has not yet been rebuilt with shared canonical contracts.
- The hidden Pricing probe has not yet been runtime-mounted/certified.
- No authenticated real-user canonical preview mapping has yet been executed.
- Full reserve → commit/release canonical persistence is not cut over; C3 establishes typed mapping and lifecycle rules only.
- Subscription renewal/replenishment behavior is out of scope for this bridge and remains a dedicated C6 commerce/subscription certification item.

## 5. V3 disposition

Disposition: `PRESERVE + NORMALIZE` for public Pricing contracts; `ADAPT` for generation quote/reservation lifecycle.

`/api/pricing/quote` remains the compatibility transparency view. Generation execution must rely on the reservation preview lifecycle for quote fingerprint/expiry/identity and then adapt that lifecycle into canonical V3 contracts.

## 6. #v3-core architecture decision

### Quote authority

For generation operations, canonical `PricingQuote` is sourced from the current reservation-preview result, not from `/api/pricing/quote` alone.

### Canonical quote identity

Legacy generation preview quote IDs are strings of the form `qt_<fingerprint-prefix>`. They are not UUIDs and therefore are not copied into canonical `PricingQuote.quote_id`.

The Pricing bridge, as quote authority, issues a deterministic V3 UUID using the account ID, user ID and full preview fingerprint. This preserves stable identity for the same canonical preview while preventing Face/Audio/Fusion capability adapters from fabricating quote IDs.

If a future/current pricing result already supplies a valid UUID quote ID, the bridge preserves it.

### Reservation contract

`CreditReservation` becomes a first-class V3 shared contract because reservation state crosses multiple generation services. It owns canonical reservation UUID, account/user/quote identity, state, reserved credits, reference/idempotency identity and timestamps.

### Settlement/ledger rule

A canonical `CreditTransaction(entry_type=consumption)` may be created only from actual settlement/ledger evidence. `charged_credits` and `balance_after` must come from `FinalizeReceipt`/ledger state. Public `billed_units` must never be treated as charged credits.

## 7. Contract impact

Additive canonical changes:

- `CreditReservationState`;
- `CreditReservation`;
- `PricingQuoteBridgeResult`;
- `PricingReservationBridgeResult`;
- `adapt_pricing_preview_response()`;
- `adapt_pricing_reserve_response()`;
- `canonical_quote_id()`;
- `credit_transaction_from_commit()`.

Public current Pricing response models remain unchanged.

## 8. Database impact

- Schema change: `none`.
- Migration: `N/A`.
- Existing reservation and ledger persistence is reused as evidence.
- No C3 probe writes pricing state.
- Later persistence cutover may add explicit canonical linkage columns only if C4/C6 evidence demonstrates a need; it is not assumed here.

## 9. Security and privacy impact

- Hidden probe is authenticated through current Pricing `AuthDep`.
- Canonical account identity is resolved from existing active billing-account persistence.
- Probe does not log bearer tokens or pricing payload contents.
- Existing optional unsigned JWT-sub fallback in Pricing is documented as a current-state security debt and is not normalized into the V3 canonical identity model.

## 10. Pricing/entitlement/credit impact

This change defines the canonical bridge itself and therefore has direct contract impact but no runtime financial mutation.

Rules:

- server remains authoritative for quote, entitlement and balance;
- canonical quote carries account/user/operation/credits/money/revision/fingerprint/expiry;
- current billing/settlement/entitlement metadata remains compatibility/operational metadata;
- reservation is first-class and idempotent;
- commit consumption is immutable and negative credit delta;
- release does not create consumption;
- retry must not duplicate reserve or ledger consumption.

## 11. Provider/model impact

None. Provider hints in quote line items remain pricing transparency/routing metadata and do not become provider execution state.

## 12. Implementation scope

Files introduced/changed:

- `services/shared/df_contracts/v3/commerce.py`;
- `services/shared/df_contracts/v3/pricing_adapter.py`;
- `services/shared/df_contracts/v3/__init__.py`;
- `test/test_v3_pricing_adapter.py`;
- `services/svc-pricing/app/Dockerfile.v3`;
- `services/svc-pricing/app/app/services/v3_pricing_adapter_shadow.py`;
- `services/svc-pricing/app/app/api/v3_adapter_probe.py`;
- `services/svc-pricing/app/app/main.py`;
- `docker-compose.v3.yml`;
- `.github/workflows/v3-contract-tests.yml`;
- this evidence record.

Out of scope:

- altering public quote/reserve/commit/release payloads;
- payment webhook redesign;
- subscription renewal/replenishment fix;
- entitlement schema migration;
- enabling subscription reconciler;
- real financial mutation through the hidden probe.

## 13. Compatibility / migration strategy

1. Existing clients keep using current public Pricing endpoints.
2. Studio preview outputs continue carrying legacy `qt_*` quote IDs and fingerprints.
3. V3 Pricing bridge derives/maintains the canonical UUID quote identity internally.
4. Face/Audio/Fusion compatibility confirmations continue accepting current quote IDs during migration.
5. Once the Pricing bridge is execution-integrated, services receive canonical quote/reservation references without changing mobile URLs.
6. Public versioned V3 responses may later expose canonical IDs only through an explicit API version decision.

## 14. Test and certification plan

Unit tests cover:

- deterministic UUID issuance for legacy quote IDs;
- preservation of an already-UUID quote ID;
- credits/money/pricebook/fingerprint/expiry mapping;
- stable fingerprint fallback;
- canonical reservation mapping;
- invalid reservation identity rejection;
- immutable consumption transaction mapping from settlement evidence.

Runtime shell certification must verify:

- V3 Pricing image rebuilds from repository-root V3 Dockerfile;
- `/api/health` remains healthy;
- hidden `/internal/v3/pricing-adapter/map-preview` returns `401` without auth;
- V2/V3 Pricing OpenAPI `.paths` remain identical;
- `DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED=true`;
- `DF_SUBSCRIPTION_RECONCILER_ENABLED=false`;
- no V3 worker/scheduler is running.

Full authenticated mapping certification later verifies an existing V3 billing account maps a representative preview response into canonical quote identity without mutation.

## 15. Final certification evidence

Pending Azure workspace unit/runtime-shell execution.

Current implementation branch: `feature/v3-c3-canonical-adapters-20260817`.

## 16. Freeze statement

`For V3 generation pricing, /api/pricing/quote remains the compatibility transparency surface while the reservations preview lifecycle is the authoritative source for quote fingerprint, expiry and generation quote semantics. The Pricing bridge alone may issue the canonical V3 UUID quote identity from a legacy fingerprint. CreditReservation is first-class shared state, and immutable CreditTransaction consumption may be created only from actual settlement/ledger credit evidence—not from service billed units. Any change to these rules requires returning to #v3-core.`
