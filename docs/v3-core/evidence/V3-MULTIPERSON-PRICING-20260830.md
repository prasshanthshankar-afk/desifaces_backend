# V3 EIP Evidence Record

Change-ID: `V3-MULTIPERSON-PRICING-20260830`
Status: `READY`
Owner: `desifaces V3 pricing / Face / Audio / Fusion`
Date: `2026-08-30`

## 1. Requirement

Introduce premium customer pricing for multi-person Face, Audio and Fusion workflows without creating participant-count-specific SKUs. One participant must continue to use the existing single-person pricing path. Two or more participants must route to exactly one multi-person SKU per studio. Participant count remains request/pricing metadata rather than SKU identity.

The implementation must reuse the existing quote -> preview -> reserve -> execute -> commit/release lifecycle and existing centralized pricing tables. It must not change subscriptions, entitlements, credit packs, Stripe, Apple IAP, or Google Play products.

## 2. EIP source

- EIP repository/workspace: `~/workspace/eip` (`/home/azureuser/workspace/eip`), the certified V3 Engineering Execution Plane workspace.
- EIP ref/commit: `B1 live foundation previously certified; this PR is additionally governed by the repository V3 EIP Engineering Gate.`
- Retrieval objective(s):
  - Establish the current V3 Face/Audio/Fusion pricing call boundaries.
  - Confirm the shared pricing request contract and svc-pricing variant quantity expansion behavior.
  - Prevent multi-person SKU proliferation and preview/reserve contract divergence.
- Retrieval query/command/reference:
  - Current repository inspection of `CreatorOrchestrator`, `TTSOrchestrator`, `FusionOrchestrator`, shared pricing models/orchestration, `svc-pricing` reservations route and pricing engine.
  - GitHub Actions `V3 EIP Engineering Gate` provides the mandatory evidence-control enforcement for this PR.

## 3. V2 current-state evidence

### Code and service ownership

- Repository/ref: `prasshanthshankar-afk/desifaces_backend`, base `desifaces-v3`.
- Service/path/symbol: `services/svc-face/app/app/services/creator_orchestrator.py::CreatorOrchestrator._build_initial_pricing_block`.
- Current owner/responsibility: Face Creator selects its existing pricing SKU and estimated generation units before preview/reservation.
- Service/path/symbol: `services/svc-audio/app/app/services/tts_orchestrator.py::TTSOrchestrator` plus `services/svc-audio/app/app/api/routes/tts_jobs.py`.
- Current owner/responsibility: Audio computes TTS usage in `chars_1k` and performs pricing preview/reserve/commit/release around synthesis.
- Service/path/symbol: `services/svc-fusion/app/app/services/fusion_orchestrator.py::FusionOrchestrator._build_initial_pricing_block`.
- Current owner/responsibility: Fusion owns parent job pricing and keeps reserve/commit/release centralized with the orchestration job.

### API/contracts

- Endpoint/event/contract: shared `PricingPreviewRequest` and `PricingReserveRequest` carry `sku_code`, `units`, and `meta`.
- Handler/service: `services/shared/python/desifaces_shared/pricing/models.py` and `services/shared/python/desifaces_shared/pricing/orchestration.py`.
- Consumers: Face, Audio, Fusion and other studio services through `SvcPricingClient`.
- Pricing expansion: `services/svc-pricing/app/app/api/routes/reservations.py` passes request metadata plus requested units into `quote_variant`; variant code is the caller `sku_code`.

### Persistence

- Schema/table/migration: existing `pricing_skus`, `pricing_sku_prices`, `pricing_variants`, `pricing_variant_lines`; no new table is required.
- Readers: `svc-pricing` quote/reservation/finalization paths.
- Writers: pricing migrations and reservation/finalization services.
- FK/index/constraint dependencies: existing pricing catalog constraints are reused; migration is idempotent/upsert based and contains fail-closed certification checks.

### Runtime/configuration

- Environment/config keys: existing `DF_PRICING_*` pricing service configuration remains unchanged.
- Queue/worker/cache/storage/provider dependencies: no provider routing, storage, worker topology, or queue contract is changed.
- Runtime evidence identifier/path: PR `#11`, V3 canonical contract workflow and V3 EIP Engineering Gate.

### Tests/operations

- Existing tests: `test/test_v3_pricing_adapter.py` and the existing V3 canonical contract suite.
- Added focused test: `test/test_v3_multi_person_pricing.py`.
- Health/monitoring/runbook dependencies: no new operational dependency; pricing errors continue through existing fail-closed studio pricing behavior.

## 4. Evidence gaps

- The migration has not yet been applied to the live V3 Postgres database at the time this evidence record is first created.
- A live authenticated multi-person quote/reserve/execute/settle transaction must still be captured before this record can move from `READY` to `CERTIFIED`.
- Current Face public composition scope is known to include single-person and `two_people`; future 3+ Face UI/API expansion is not part of this pricing PR. The pricing policy itself is participant-count agnostic for that future expansion.

## 5. V3 disposition

Disposition: `ADAPT`

Rationale:

Reuse the existing pricing catalog, pricebooks, service-to-service pricing contracts, reservation lifecycle and studio orchestrators. Add only a participant-agnostic multi-person selection policy and three new premium catalog entry points. Do not introduce a parallel entitlement/payment system and do not encode participant count into identifiers.

## 6. #v3-core architecture decision

V3 multi-person customer pricing uses exactly one premium SKU/variant per studio:

- `FACE_MULTI_PERSON`
- `AUDIO_MULTI_PERSON`
- `FUSION_MULTI_PERSON`

The same SKU applies to 2, 3, 4, 5 or more participants. Participant count is metadata/context, never SKU identity. Single-person pricing is untouched. Billing quantity remains the studio-native workload meter: Face `num_edits`, Audio `chars_1k`, Fusion `minutes`. The initial premium unit-rate policy is 1.25x the corresponding baseline catalog rate and remains centralized in the pricing catalog/pricebook rather than frontend code.

## 7. Contract impact

- Canonical contract changes: additive shared helper `desifaces_shared.pricing.multi_person`; no breaking pricing request/response change.
- Versioning impact: none.
- Compatibility adapter required: yes, narrow runtime selection adapters in Face/Audio/Fusion; existing single-person callers remain unchanged.
- Client impact: clients may provide explicit participant/multi-person context; clients must display server-returned preview and must not calculate premium price locally.

## 8. Database impact

- Schema change: `additive catalog data only`.
- Migration file: `migrations/2026_08_30_multi_person_premium_pricing.sql`.
- Data backfill/reconciliation: none.
- Rollback/compensating action: deactivate/remove the three new variant/price/SKU rows; existing single-person catalog remains intact.
- Confirm V3-only DB execution: pending live V3 migration certification; no production/V2 execution is authorized by this PR.

## 9. Security and privacy impact

- Authentication: unchanged; existing authenticated studio/pricing service flow remains mandatory.
- Authorization/account ownership: unchanged; pricing reservation remains bound to the existing user/account context.
- Secrets: no new secrets or secret values.
- PII/media/privacy: participant count is non-PII operational metadata; no additional customer content is exposed to pricing.
- Audit requirements: existing quote/reservation/ledger evidence is retained; participant count and pricing policy metadata are attached to premium requests.

## 10. Pricing/entitlement/credit impact

- Pricing: additive premium multi-person unit rates; exactly three participant-agnostic SKUs/variants.
- Entitlement: no change.
- Credits/ledger/idempotency: existing preview/reserve/commit/release lifecycle and idempotency contracts are reused.
- Provider billing events: no provider-cost or provider-routing contract change.

## 11. Provider/model impact

- Provider-specific behavior inspected: pricing selection is above provider-specific generation and does not alter model/provider routing.
- Canonical normalization: participant count is normalized by the shared multi-person pricing helper from explicit structured context.
- Routing/failover impact: none.

## 12. Implementation scope

- Files/services expected to change:
  - `migrations/2026_08_30_multi_person_premium_pricing.sql`
  - `services/shared/python/desifaces_shared/pricing/multi_person.py`
  - Face/Audio/Fusion multi-person pricing policy modules and service startup wiring
  - `test/test_v3_multi_person_pricing.py`
  - V3 canonical contract workflow coverage
- Explicitly out of scope:
  - subscription and entitlement redesign
  - Stripe/Apple/Google payment product changes
  - participant-count-specific SKUs
  - provider/model routing changes
  - broad Face 3+ participant UI/API expansion

## 13. Compatibility / migration strategy

The change is additive. One-person requests continue through the pre-existing service SKU selection. Explicit 2+ participant context switches only the owning studio operation to its multi-person premium SKU. Participant count is retained as metadata while the existing native billing quantity is used for quote/reserve/finalization. The migration creates only new catalog rows and does not mutate existing single-person rows. Audio selection is request-scoped to prevent cross-request premium leakage. The policy must be installed idempotently at service startup.

## 14. Test and certification plan

- Unit tests: participant normalization, 1-person fallback, 2/3/4/5+ same-SKU selection, native unit conversion.
- Contract tests: shared `PricingPreviewSpec` compatibility; Audio preview must use only supported `sku_code`, `units`, `meta` fields.
- Integration tests: runtime policy startup wiring for Face, Audio and Fusion; preview/reserve SKU consistency.
- Migration tests: exactly three SKUs/variants/variant-lines, no MP2/MP3/MP4/MP5 identifiers, baseline pricebook cloning, 1.25 premium rate, unbounded quantity rows.
- Runtime/end-to-end certification: apply migration to V3 DB; restart/recreate affected V3 services; perform single-person negative regression and multi-person positive quote -> reserve -> generate -> commit/release checks for Face/Audio/Fusion.
- V2 regression protection: no existing single-person pricing row is modified and one-person selector returns the existing pricing block unchanged.

## 15. Final certification evidence

Complete before marking `CERTIFIED`.

- Commit/PR: PR `#11`, head advances with this evidence/test package.
- Test result: V3 Canonical Contract Tests pending at first creation; update after workflow completion.
- Runtime evidence: pending live V3 runtime execution.
- Migration/schema evidence: migration contains transactional fail-closed certification gates; live V3 DB result pending.
- #v3-core document updated: `N/A` for a breaking architecture change; this evidence record captures the additive decision and is enforced by the EIP gate.

## 16. Freeze statement

`Freeze the V3 multi-person pricing identity model at one participant-agnostic premium SKU per Face/Audio/Fusion studio, with participant count carried only as metadata and studio-native workload retained as quantity. Any future participant-count SKU proliferation, entitlement/payment-product coupling, or change to this billing ownership model requires returning to #v3-core architecture control.`
