# V3-C3 Canonical Adapter Matrix — Face → Audio → Fusion → Pricing

Change-ID: `V3-C3`
Status: `DESIGN_FROZEN_FOR_CRITICAL_CREATOR_PATH`
Owner: `#v3-core`
Date: `2026-08-17`

## 1. Purpose

This record freezes the compatibility and canonical-adapter strategy for the first desifaces-v3 critical creator path:

`Face → Audio → Fusion → Pricing/Credits`

The goal is to preserve the certified V2 public API behavior required by the current mobile/web clients while moving implementation semantics behind those APIs to the canonical V3 contract model.

This is an adapter-first migration. V2-compatible HTTP contracts remain available during migration; canonical V3 contracts become the internal source of truth.

## 2. Evidence baseline

The following evidence was inspected before this decision:

- V2 and V3 live OpenAPI `.paths` parity was tested service-by-service for `svc-core`, `svc-fusion`, `svc-face`, `svc-audio`, `svc-dashboard`, `svc-fusion-extension`, `svc-music`, `svc-commerce`, `svc-pricing`, and `svc-marketing`. Result: 10/10 `PASS`.
- Current V3 branch: `feature/v3-c2c-parallel-runtime-isolation-20260817`.
- Certified V3-C2C checkpoint before this record: `c09a71b`.
- Canonical contracts:
  - `services/shared/df_contracts/v3/common.py`
  - `services/shared/df_contracts/v3/domain.py`
  - `services/shared/df_contracts/v3/commerce.py`
- Backend API evidence:
  - `services/svc-face/app/app/api/routes/face_jobs.py`
  - `services/svc-audio/app/app/api/routes/tts_jobs.py`
  - `services/svc-fusion/app/app/api/routes/fusion_jobs.py`
  - `services/svc-pricing/app/app/api/routes/pricing.py`
  - `services/svc-pricing/app/app/api/routes/reservations.py`
- Mobile API evidence:
  - `src/core/api/endpoints.ts`
  - `src/core/api/faceClient.ts`
  - `src/features/audio/api/audioTts.ts`
  - `src/features/fusion/api/creatorFusion.ts`
  - `src/core/api/pricingClient.ts`

## 3. Frozen compatibility rule

Existing certified V2 public APIs are compatibility contracts during V3 migration.

New V3 implementation logic MUST terminate in canonical V3 application/domain contracts. Existing endpoints MAY remain as compatibility facades. Legacy or duplicate endpoints MUST NOT be removed until caller evidence proves they are unused and an explicit `#v3-core` deprecation decision is recorded.

The required direction is:

`Existing client contract → compatibility adapter → canonical V3 request/domain → service capability → provider/storage/pricing adapter → canonical result → compatibility response`

V3 MUST NOT copy V2 implementation-specific request ambiguity, provider fields, pricing artifacts, or status vocabulary into the canonical domain merely to preserve the old HTTP shape.

## 4. Canonical contracts used by the adapters

### 4.1 Request/context

All adapters SHOULD construct a `RequestContext` containing:

- `request_id`
- `correlation_id`
- `RequestActor`
- `idempotency_key` when available
- `client_app`
- `client_version`
- `requested_at`

The canonical actor model supports `user`, `service`, `api_key`, and `system` actors.

### 4.2 Generation

Creator generation operations map to:

- `GenerationRequest`
- `GenerationJob`
- `ProviderExecution`
- `MediaAsset`
- `SafetyDecision`

Generation kinds for this phase are:

- `face`
- `audio`
- `fusion`

Canonical job states are:

- `submitted`
- `queued`
- `running`
- `succeeded`
- `failed`
- `blocked`
- `canceled`
- `expired`

Provider-specific states MUST be normalized into these states before crossing the canonical application boundary.

### 4.3 Pricing/credits

Pricing operations map to:

- `PricingQuote`
- `Entitlement`
- `CreditTransaction`

`PricingQuote` contains canonical account/user ownership, operation, credits, optional money, pricebook revision, fingerprint, expiry, and creation time.

`CreditTransaction` is the immutable accounting event. Reservation implementation details remain a pricing capability concern until a dedicated canonical reservation contract is introduced.

## 5. Classification vocabulary

- `PRESERVE`: current external contract is suitable and remains public.
- `PRESERVE + NORMALIZE`: preserve external behavior but normalize canonical request/response/error/meta semantics internally.
- `ADAPT`: preserve current caller contract through a compatibility adapter backed by canonical V3 contracts.
- `INTERNALIZE`: route/capability should not be part of normal public client surface.
- `DEPRECATE CANDIDATE`: legacy/duplicate route requiring caller-proof before removal.

## 6. Canonical adapter matrix

| Capability | Current external contract | Current caller/evidence | Classification | Canonical V3 mapping | Adapter responsibility | Compatibility output |
|---|---|---|---|---|---|---|
| Face source upload | `POST /api/face/assets/upload` | mobile Face client | ADAPT | `MediaAsset(kind=image, role=source)` | validate auth/ownership/safety/storage, create canonical media identity and lineage | preserve `asset_id`, `image_url`, content metadata |
| Face I2I precheck | `POST /api/face/creator/i2i/content-safety/check` | Face Creator flow | PRESERVE + NORMALIZE | `SafetyDecision` linked to pending generation/source media | normalize provider/policy result into canonical safety state | preserve allow/status/reason semantics |
| Face pricing preview | `/api/face/creator/pricing/preview` | endpoint registry / studio flow | ADAPT | canonical `PricingQuote` | translate Face inputs into canonical pricing operation and bind quote/fingerprint | preserve current studio pricing preview payload during migration |
| Face create | `POST /api/face/creator/generate` | `faceClient.ts` | ADAPT | `GenerationRequest(kind=face)` → `GenerationJob` | accept legacy flat or wrapped `studio_input`; extract `pricing_confirmation`; resolve source media; normalize parameters; propagate request/idempotency context | preserve current `job_id`, status/message/config response |
| Face status | `GET /api/face/creator/jobs/{id}/status` | `faceClient.ts` | ADAPT | `GenerationJob` + output `MediaAsset[]` | map canonical state/progress/errors/media into Face-specific variant view | preserve Face `JobStatusResponse` |
| Face list jobs | `GET /api/face/creator/jobs` | `faceClient.ts` | PRESERVE + NORMALIZE | paged canonical jobs filtered by `kind=face` | account ownership, pagination and state normalization | preserve current list until client adopts pagination envelope |
| Face legacy generate/jobs | `/api/face/generate`, `/api/face/jobs*` | endpoint registry retains legacy routes | DEPRECATE CANDIDATE | canonical Face generation | route through same adapter if still called; add usage telemetry | remove only after caller proof and explicit decision |
| Face config/masterdata | `/api/face/config/*` | mobile Face client | PRESERVE + NORMALIZE | DB-backed masterdata/capability contracts | keep geography/context DB-driven; no hard-coded regional rules | preserve current catalog payload while versioning masterdata |
| Audio create | `POST /api/audio/tts` | `audioTts.ts` | ADAPT | `GenerationRequest(kind=audio)` → `GenerationJob` | map text/locale/translation/voice/style/rate/pitch/volume/context/output format into canonical parameters; attach quote confirmation; derive source/participant relationships when invoked from Face/Fusion flow | preserve current `job_id` response |
| Audio pricing preview | `POST /api/audio/tts/pricing/preview` | endpoint registry/studio flow | ADAPT | canonical `PricingQuote` | stable preview fingerprint; operation/units normalization; entitlement decision stays pricing-owned | preserve preview response including `quote_id`/fingerprint |
| Audio status | `GET /api/audio/jobs/{id}/status` | `audioTts.ts` | ADAPT | canonical `GenerationJob` + audio `MediaAsset[]` | normalize status/error/artifact identifiers and output URLs | preserve current variants payload |
| Audio catalog | `/api/audio/catalog/*` | endpoint registry | PRESERVE + NORMALIZE | masterdata/provider capability catalog | keep locale/language/voice eligibility DB/provider-catalog driven | preserve mobile catalog contract |
| Audio catalog admin/sync | catalog sync/admin routes | backend/admin paths | INTERNALIZE | platform operations | require explicit administrative/service authorization; exclude normal client contract | no normal consumer dependency |
| Fusion pricing preview | `POST /jobs/pricing/preview` | `creatorFusion.ts` / endpoint registry | ADAPT | canonical `PricingQuote` | collapse duration/profile/provider aliases before pricing; bind quote to normalized Fusion operation | preserve current `quote_id`, fingerprint, pricing summaries |
| Fusion create | `POST /jobs` | `creatorFusion.ts` | ADAPT | `GenerationRequest(kind=fusion)` → `GenerationJob` | canonicalize face/audio sources to `source_media_ids`; normalize profile/mode/duration/camera/prompt/consent; ignore provider-specific aliases as domain fields; bind pricing quote; create provider execution separately | preserve current Fusion job response |
| Fusion status | `GET /jobs/{id}` | `creatorFusion.ts` | ADAPT | `GenerationJob` + `ProviderExecution` + video `MediaAsset` | hide provider-specific status behind canonical state; map final/preview/share media to compatibility fields | preserve current job/status/video URL contract |
| Fusion internal child renders | child jobs marked pricing-suppressed/bill-to-parent | Fusion/Fusion Extension backend | INTERNALIZE | child `GenerationJob` linked to parent generation/scene | pricing suppression is orchestration metadata, not a public billing mode; prevent child charge; preserve lineage | internal only |
| Longform/Fusion Extension | `/api/longform/*` | `creatorFusion.ts` | ADAPT, then future Story/Scene migration | future `StoryRef`/`SceneRef` + child generation jobs + final `MediaAsset` | maintain compatibility now; later move segmentation/orchestration to Story/Director without breaking caller | preserve current longform contract during C3-C5 |
| Pricing quote | `POST /api/pricing/quote` | `pricingClient.ts` | PRESERVE + NORMALIZE | `PricingQuote` | translate variant/params/channel/country/currency into canonical operation; retain module gate/entitlement evaluation inside pricing capability; issue revision/fingerprint/expiry | preserve allowed/billing/credits/money/lines response |
| Credit balance | `GET /api/credits/balance` | `pricingClient.ts` | PRESERVE + NORMALIZE | projection over immutable `CreditTransaction` ledger plus reservations | server authoritative balance/reserved/available calculation | preserve current balance payload |
| Pricing preview/reserve | `/api/pricing/reservations/preview`, `/reserve` | shared pricing orchestration/studios | PRESERVE + NORMALIZE | `PricingQuote` plus pricing-internal reservation | validate quote/fingerprint/account ownership/idempotency; reserve once | preserve reservation identifiers and pricing artifacts |
| Pricing commit | `/api/pricing/reservations/commit` | generation orchestrators | PRESERVE + NORMALIZE | immutable `CreditTransaction(entry_type=consumption)` | commit exactly once using idempotency/reference to canonical generation/job | preserve finalize receipt |
| Pricing release | `/api/pricing/reservations/release` | failure/cancel paths | PRESERVE + NORMALIZE | reservation state + no consumption transaction | release exactly once; retry-safe | preserve current release response |

## 7. Face adapter rules

### 7.1 Request compatibility

The current Face backend accepts two shapes:

1. legacy flat `CreatorPlatformRequest`
2. wrapped request containing `studio_input` and optional `pricing_confirmation`

The V3 Face adapter MUST accept both during compatibility mode but MUST construct exactly one canonical `GenerationRequest`.

### 7.2 Parameter mapping

Face-specific generation controls remain in `GenerationRequest.parameters` until typed capability contracts are introduced. They include generation mode, geography/masterdata codes, composition, style/context/shot/aspect ratio, variant count, prompt, seed behavior, identity-preservation controls and source image references.

Source image URLs MUST NOT become canonical identity. The adapter resolves them to canonical `MediaAsset` IDs where possible and populates `source_media_ids`.

### 7.3 Output mapping

Generated Face variants become canonical image `MediaAsset` records. The compatibility adapter may expose `face_profile_id`, `media_asset_id`, `image_url`, `prompt_used`, technical specs and creative variations, but canonical lineage remains on `MediaAsset`/`GenerationJob`.

## 8. Audio adapter rules

### 8.1 Input normalization

The current TTS contract includes:

- text
- target locale
- optional source language
- translate flag
- voice
- style/style degree
- rate/pitch/volume
- context
- output format
- optional pricing confirmation

The adapter MUST retain these as Audio capability parameters while canonical ownership, job state, source/output media, pricing quote reference and request context use shared V3 contracts.

### 8.2 Face → Audio continuity

When Audio is invoked from a Face-led creator flow, the V3 orchestration layer SHOULD carry participant/generation/media references forward. Gender/voice selection remains a capability concern and MUST NOT be re-inferred from geography.

### 8.3 Output

Audio outputs become `MediaAsset(kind=audio)` linked to the Audio generation/job and upstream source lineage.

## 9. Fusion adapter rules

### 9.1 Alias collapse is mandatory

Current Fusion callers expose many overlapping aliases for the same concepts, including face image/artifact, audio URL/artifact, profile, mode, provider hints, duration fields, camera controls, prompt fields, script and pricing confirmation.

The compatibility adapter MUST normalize these before constructing canonical contracts. Canonical V3 models MUST NOT preserve every alias.

### 9.2 Source media

Face and Audio inputs MUST resolve to canonical `MediaAsset` references and populate Fusion `GenerationRequest.source_media_ids`.

Direct raw URLs may remain accepted at the compatibility edge but SHOULD be ingested/resolved into canonical media before durable orchestration.

### 9.3 Provider separation

Provider name, provider job ID, provider status and provider attempts belong to `ProviderExecution`, not `GenerationRequest` or `GenerationJob` business state.

### 9.4 Parent/child billing

Fusion internal child jobs used by longform orchestration MUST never create independent customer charges when the parent job is billable. Parent-child billing linkage is an orchestration/pricing concern and MUST be explicit and retry-safe.

## 10. Pricing adapter rules

### 10.1 Server authority

Pricing, entitlements, credits and settlement remain server authoritative. Client-supplied country/currency/provider hints cannot override policy except through explicit supported/QA mechanisms.

### 10.2 Quote identity

Canonical `PricingQuote` uses UUID quote identity, account/user ownership, operation, credits, optional money, pricebook revision, fingerprint, expiry and creation time.

The current pricing implementation also exposes variant/module/pricebook names, line items, shadow totals and economics. Those remain compatibility/operational projections; they are not all required fields of the canonical quote.

### 10.3 Reservation lifecycle

The current backend implements preview, reserve, commit/finalize and release. V3-C3 keeps those endpoints but treats reservation as pricing-capability state rather than embedding reservation fields into generic generation contracts.

A future canonical `CreditReservation` contract MAY be added if cross-service use requires a versioned shared contract. Until then, generation stores only the quote reference plus pricing orchestration references in controlled metadata.

### 10.4 Ledger rule

Successful billable execution MUST produce an immutable, idempotent `CreditTransaction`. Failed/canceled execution MUST release the reservation without creating duplicate consumption.

## 11. Status normalization matrix

| Current/possible status | Canonical `JobState` |
|---|---|
| accepted / submitted / created | `submitted` |
| queued / pending | `queued` |
| processing / running / in_progress | `running` |
| completed / complete / succeeded / success | `succeeded` |
| failed / error | `failed` |
| blocked / safety_blocked | `blocked` |
| canceled / cancelled | `canceled` |
| expired / timed_out where terminal by policy | `expired` |

Provider status MUST be retained on `ProviderExecution.metadata` or a provider-specific adapter if operationally required, but public capability status is derived from canonical job state.

## 12. Error normalization

Compatibility adapters SHOULD map service-specific HTTP errors to canonical `ApiError` codes internally:

- authentication failures → `unauthenticated`
- ownership/authorization failures → `forbidden`
- missing jobs/media/quotes → `not_found`
- invalid request/masterdata → `validation_error`
- replay with incompatible payload → `idempotency_conflict`
- disabled module/plan → `entitlement_required`
- credit shortage → `insufficient_credits`
- safety rejection → `safety_blocked`
- provider outage/exhaustion → `provider_unavailable`

Compatibility responses MAY preserve current error payloads until clients migrate to the V3 `ApiEnvelope`.

## 13. Idempotency and correlation requirements

Every expensive generation request MUST have a stable idempotency boundary.

Adapters MUST propagate or generate:

- request ID
- correlation ID
- actor/account identity
- idempotency key
- canonical generation ID
- canonical job ID
- pricing quote ID
- reservation/reference IDs when applicable
- provider execution IDs

A retry MUST NOT create a duplicate provider call, duplicate credit reservation, duplicate ledger consumption or duplicate durable media record.

## 14. Media lifecycle requirements

The critical path MUST preserve canonical lineage:

`Face source MediaAsset → Face GenerationJob → Face output MediaAsset → Audio GenerationJob → Audio MediaAsset → Fusion GenerationJob → Video MediaAsset`

Audio may not always consume the Face media directly, but the enclosing creator/project/participant context SHOULD maintain the relationship for end-to-end traceability.

Final user-visible media MUST be distinguishable from preview/intermediate artifacts by `MediaRole`.

## 15. Public API decisions for this phase

### Preserve during migration

- Face Creator generation/status/config
- Audio TTS/status/catalog
- Fusion create/status
- Pricing quote/balance/reservation lifecycle
- current pricing preview contracts used by studios

### Internalize

- provider-control endpoints
- catalog sync/admin operations
- recovery/scheduler/worker control APIs
- internal child-render billing controls
- webhook/service-only mutation routes

### Deprecation candidates requiring caller proof

- legacy Face generate/jobs routes
- duplicate/fallback pricing summary aliases
- guessed/fallback longform route variants once the validated route is universally adopted

## 16. Contract gaps identified by C3

The current canonical V3 contracts provide the correct shared vocabulary but additional typed capability contracts are still required before implementation cutover.

Required next contracts:

1. `FaceGenerationParameters` or equivalent typed Face capability request.
2. `AudioGenerationParameters` or equivalent typed Audio capability request.
3. `FusionGenerationParameters` with one normalized representation for source media, profile, duration, camera, prompt, consent and quality.
4. Optional canonical `CreditReservation` if reservation identity/state must cross service boundaries as a first-class shared contract.
5. Compatibility response mappers for Face, Audio and Fusion.
6. Explicit versioned pricing preview/confirmation contract using canonical quote identity, fingerprint and expiry.

These types MUST remain capability-specific and MUST NOT bloat the generic `GenerationRequest` with provider or UI aliases.

## 17. Implementation sequence

C3 implementation order is frozen as:

1. Create shared adapter utilities for `RequestContext`, job-state normalization, error normalization and idempotency propagation.
2. Implement Face compatibility adapter to canonical generation/media contracts.
3. Implement Audio compatibility adapter using the same lifecycle primitives.
4. Implement Fusion compatibility adapter, including alias collapse and source-media lineage.
5. Implement Pricing quote/confirmation/reservation bridge and immutable consumption reference.
6. Add contract tests asserting existing HTTP request/response compatibility.
7. Add canonical-domain tests asserting one normalized internal representation per operation.
8. Add cross-service E2E test: Face → Audio → Fusion with pricing preview/reserve/commit/release behavior.
9. Collect runtime usage evidence for deprecation candidates.
10. Only after certification, enable V3 execution workers/provider calls in a separately controlled milestone.

## 18. Certification gates

V3-C3 critical creator path is not complete until all are true:

- current public route/method surface remains compatible or explicitly versioned
- mobile Face/Audio/Fusion/Pricing callers pass without endpoint rewrites required for compatibility
- canonical `RequestContext` is propagated
- canonical generation/job/media objects are created or deterministically mapped
- provider state is separated from canonical job state
- quote identity/fingerprint/expiry are validated server-side
- credit reservation/commit/release is replay-safe
- provider calls cannot duplicate on retry
- media lineage is preserved
- errors normalize to canonical codes internally
- legacy/deprecation candidates have caller evidence
- V2 production behavior remains unaffected

## 19. Freeze statement

For the critical Face → Audio → Fusion → Pricing path, V3 adopts an adapter-first compatibility architecture.

The certified V2 HTTP surface remains available during migration. Canonical V3 `RequestContext`, `GenerationRequest`, `GenerationJob`, `ProviderExecution`, `MediaAsset`, `SafetyDecision`, `PricingQuote`, `Entitlement`, and `CreditTransaction` are the internal source-of-truth vocabulary.

Capability-specific adapters translate legacy/current request and response shapes at the boundary. Provider-specific fields, duplicated aliases, UI fallback routes, and pricing implementation details MUST NOT become permanent generic V3 domain fields.

This decision remains frozen until explicitly reconsidered in `#v3-core`.
