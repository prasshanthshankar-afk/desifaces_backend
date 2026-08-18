# V3-C4 / V3-C5 / V3-C6 Foundation Certification

Change-IDs: `V3-C4`, `V3-C5`, `V3-C6`
Status: `CERTIFIED`
Owner: `#v3-core`
Date: `2026-08-18`
Certified branch: `feature/v3-c4-c6-foundation-closure-20260818`
Certified C3 baseline: `61876055c5f43cdd5032e6eadd37da5ab24a9ec4`

## 1. Certification scope

This record is the final runtime certification for the three post-C3 V3 foundation milestones required before desifaces-v3 enhancement development:

- V3-C4 — Canonical Media Lifecycle
- V3-C5 — Canonical Generation Persistence
- V3-C6 — Billing and Renewal Credit Integrity

This certification supersedes the pre-runtime `READY_FOR_RUNTIME_CERTIFICATION` markers in the three implementation-plan evidence records. The architecture decisions and detailed implementation scope in those records remain binding.

## 2. V3-C4 certification result — CERTIFIED

Runtime certification proved:

- focused V3 unit/contract suite passed (`73 passed`);
- migration target was exactly `desifaces_v3`;
- pre-migration V3 database backup completed successfully;
- `2026_08_18_v3_media_lifecycle.sql` applied successfully;
- canonical media schema invariants passed;
- canonical MediaAsset write/read behavior passed;
- source-to-derived media lineage passed;
- synthetic media certification data was rolled back;
- V3 API health remained good;
- no V3 execution worker/scheduler was enabled.

Observed certification markers:

- `C4_C5_SCHEMA=PASS`
- `C4_MEDIA_INVARIANTS=PASS`
- `C4_MEDIA_WRITE_READ_LINEAGE=PASS`
- `C4_C5_CERTIFICATION_ROLLBACK=PASS`
- `V3_C4_C5_RUNTIME_CERTIFICATION=PASS`

V3-C4 status: `CERTIFIED`.

## 3. V3-C5 certification result — CERTIFIED

Runtime certification proved:

- `2026_08_18_v3_generation_persistence.sql` applied successfully;
- canonical generation request persistence works;
- repeated request with the same account/idempotency identity resolves to the same canonical generation identity;
- root/child job persistence and provider execution registration work without invoking an external provider;
- input/output MediaAsset linkage works;
- canonical job transitions and terminal-state protections work;
- synthetic generation/provider/media records were rolled back;
- no V3 execution worker/scheduler was enabled.

Observed certification markers:

- `C5_GENERATION_IDEMPOTENCY=PASS`
- `C5_JOB_PROVIDER_MEDIA_STATE_MACHINE=PASS`
- `C4_C5_CERTIFICATION_ROLLBACK=PASS`
- `V3_C4_C5_RUNTIME_CERTIFICATION=PASS`

V3-C5 status: `CERTIFIED`.

## 4. V3-C6 defect discovery and remediation

C6 certification intentionally exercised the known monthly renewal problem instead of only validating schema presence.

The first corrected runtime test established that the actual plan reconciliation granted the expected Business Monthly allowance, but the certification lookup exposed inconsistent cycle metadata persistence.

The second runtime test reproduced the real renewal defect:

- cycle 1 funded with 2,000 included credits;
- 100 included credits were synthetically spent;
- cycle 2 incorrectly retained 1,900 instead of replenishing to 2,000;
- the reconciler reported `adopted_legacy_lots=1`, proving that the previous-cycle lot had lost usable cycle identity and was being adopted into the next cycle rather than rolled over.

Root cause:

- `svc-pricing` registers asyncpg JSON/JSONB codecs whose encoder serializes Python values;
- some pricing writers also passed pre-serialized JSON strings;
- this could persist JSON objects as JSON string scalars;
- `metadata_json->>'cycle_key'` then returned NULL, so a funded cycle could later be misclassified as legacy.

Remediation implemented and certified:

1. Pricing JSON/JSONB codec accepts both structured Python values and legacy pre-serialized object/list payloads without double serialization.
2. `2026_08_18_v3_credit_lot_jsonb_normalization.sql` normalizes existing double-encoded credit-lot metadata and installs a narrow DB normalization trigger.
3. Credit-lot account ownership remains mandatory.
4. Provider-neutral cycle integrity is separated from Stripe reconciliation failure so Apple/Google repair does not depend on Stripe health.
5. Native current-period recognition is period-aware.
6. Google Play renewal identity remains period-end-aware because Google may retain purchase token/original start while expiry advances.
7. Already-correct Apple/Google current-period lots are recognized so spent credits are not reset within the same billing period.

## 5. V3-C6 certification result — CERTIFIED

Final C6 runtime certification proved:

- database target exactly `desifaces_v3`;
- credit-lot JSONB normalization migration passed;
- V3 Pricing API healthy;
- C6 schema passed;
- every user-owned credit lot has canonical billing-account ownership;
- initial subscription cycle funding passed;
- next monthly cycle replenished to the full plan allowance after prior-cycle spend;
- replaying the same renewal cycle did not double-grant credits;
- purchased/top-up credits remained unchanged;
- active-period integrity sweep passed without provider API execution;
- all synthetic certification financial mutations rolled back;
- V3 subscription reconciler remained disabled during development certification;
- no V3 execution worker/scheduler was enabled;
- V2 Pricing remained healthy;
- V2/V3 public Pricing OpenAPI path parity remained intact.

Observed final certification markers:

- `C6_DB_TARGET=PASS`
- `C6_CREDIT_LOT_JSONB_NORMALIZATION=PASS`
- `C6_V3_PRICING_HEALTH=PASS`
- `C6_SCHEMA=PASS`
- `C6_ACCOUNT_OWNERSHIP=PASS`
- `C6_INITIAL_CYCLE_FUNDING=PASS`
- `C6_MONTHLY_RENEWAL_REPLENISHMENT=PASS`
- `C6_RENEWAL_IDEMPOTENCY=PASS`
- `C6_TOPUP_PRESERVATION=PASS`
- `C6_ACTIVE_PERIOD_INTEGRITY_SWEEP=PASS`
- `C6_CERTIFICATION_ROLLBACK=PASS`
- `V3_C6_RUNTIME_CERTIFICATION=PASS`
- `C6_EXECUTION_GUARD=PASS`
- `C6_V2_PRICING_COEXISTENCE=PASS`
- `C6_PUBLIC_API_PARITY=PASS`
- `C6_STATUS=CERTIFIED`
- `V3_C6_RESUME_CERTIFICATION=PASS`

V3-C6 status: `CERTIFIED`.

## 6. Final foundation state

The V3 foundation milestones are now frozen as:

- V3-C1 Canonical contracts — `CERTIFIED`
- V3-C2 Persistence/runtime isolation — `CERTIFIED`
- V3-C2C Parallel API runtime — `CERTIFIED`
- V3-C3 API inventory, compatibility and canonical adapters — `CERTIFIED`
- V3-C4 Canonical Media Lifecycle — `CERTIFIED`
- V3-C5 Canonical Generation Persistence — `CERTIFIED`
- V3-C6 Billing/Renewal Credit Integrity — `CERTIFIED`

## 7. Enhancement handoff

With C1-C6 certified, `#v3-core` foundation work is closed for normal development. Core is reopened only when an enhancement exposes a concrete shared-contract, persistence, security, pricing, media, or runtime gap that cannot be solved inside the enhancement boundary.

The next primary workstreams are:

1. Multi-Person Face / Audio / Fusion
2. Assistant / Chatbot (RAG + tools + conversation)
3. Rich Web Application

All three must consume the certified canonical Account, Participant, MediaAsset, GenerationRequest/GenerationJob, pricing/credit and compatibility foundations rather than defining competing shared models.
