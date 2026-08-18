# V3 EIP Evidence Record — Face Canonical Compatibility Adapter

Change-ID: `V3-C3-FACE-ADAPTER`
Status: `READY`
Owner: `#v3-core`
Date: `2026-08-17`

## 1. Requirement

Implement the first V3-C3 capability adapter for the critical creator path while preserving the certified V2 Face HTTP contract.

The bounded change establishes:

- a typed, provider-neutral Face generation parameter contract;
- translation from current flat/wrapped Face create payloads to canonical `RequestContext` + `GenerationRequest`;
- canonical source `MediaAsset` construction;
- canonical `GenerationJob`/media back-mapping to the current Face status shape;
- shared account-context resolution using existing billing-account persistence;
- a hidden, read-only V3-only adapter probe for integration certification;
- no provider execution and no change to public OpenAPI paths.

## 2. EIP source

- EIP repository: `prasshanthshankar-afk/desifaces-eos`
- EIP ref/commit: `feature/eos-foundation` (repository EKB tree observed as `110e101b1ded730b0e3ed1d313ed05fe448d1f80`)
- Retrieval objective(s):
  - establish the current Face API request/response contract;
  - establish current Face media identity and ownership behavior;
  - establish current pricing-confirmation shape and billing-account persistence;
  - determine whether existing service packaging already includes V3 canonical contracts;
  - preserve integration/security principles while introducing the adapter seam.
- Retrieval query/command/reference:
  - EIP/EOS foundation README and architecture/integration standards scope;
  - direct repository evidence from the exact V3 branch listed below;
  - live V2/V3 OpenAPI `.paths` comparison performed during V3-C3 discovery: all ten service surfaces `PASS`.

The linked EOS repository explicitly describes the foundation as working-draft standards that still require current code paths, ADRs, APIs, and database references as indexing matures. Therefore this evidence record supplements EIP/EOS with exact current repository/runtime evidence rather than silently assuming missing EKB detail.

## 3. V2 current-state evidence

### Code and service ownership

- Repository/ref: `prasshanthshankar-afk/desifaces_backend`, V2 anchor `70a80cef08cebb8f02385a8e0f1adbac7c85fbb8`; V3 implementation branch `feature/v3-c3-canonical-adapters-20260817` based on frozen C3 matrix commit `d1ddb0197de7f5af93a3070a7be616a5d8af7b15`.
- Service/path/symbol: `services/svc-face/app/app/api/routes/face_jobs.py::creator_generate_faces`.
- Current owner/responsibility: `svc-face` parses the current Creator request, invokes `CreatorOrchestrator`, owns Face job APIs, I2I source upload, Face safety precheck, Face pricing preview and Face status/list compatibility responses.

### API/contracts

- Endpoint/event/contract: `POST /api/face/creator/generate` accepts both the legacy flat `CreatorPlatformRequest` and the newer wrapped `CreatorGenerateRequest {studio_input, pricing_confirmation}`.
- Handler/service: `services/svc-face/app/app/api/routes/face_jobs.py` and `services/svc-face/app/app/domain/models.py`.
- Consumers: current mobile Face client in `prasshanthshankar-afk/desifaces_frontend`, including `src/core/api/faceClient.ts` and centralized `src/core/api/endpoints.ts`.
- Related endpoints: `/api/face/assets/upload`, `/api/face/creator/i2i/content-safety/check`, `/api/face/creator/pricing/preview`, `/api/face/creator/jobs/{id}/status`, `/api/face/creator/jobs` and DB-driven `/api/face/config/*` catalogs.
- Live compatibility evidence: V2 and V3 Face OpenAPI `.paths` compared equal before this implementation.

### Persistence

- Schema/table/migration: `public.media_assets` stores UUID media asset IDs, `user_id`, kind, storage reference and media metadata; `MediaAssetsRepo.create_asset()` returns the UUID ID as text.
- Readers: Face Creator orchestration and Face asset/status paths.
- Writers: `/api/face/assets/upload` and generation orchestration.
- FK/index/constraint dependencies: canonical media migration is deferred to V3-C4; C3 reuses an existing owned UUID only after ownership lookup and does not alter schema.
- Billing-account persistence: `public.pricing_billing_accounts`, `public.pricing_billing_account_members`, and `pricing_credit_accounts.billing_account_id` are already present via `migrations/2026_03_11_billing_accounts_invoices.sql`.
- Existing-user backfill behavior: individual billing accounts use `account_code = 'user:' || user_id`, with membership rows linking users to accounts.

### Runtime/configuration

- Environment/config keys: new V3-only `DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED`; existing Face storage/config keys remain unchanged.
- Queue/worker/cache/storage/provider dependencies: this adapter/probe performs no queue submission, worker activation, storage write, pricing reservation or provider call.
- Runtime evidence identifier/path: V3-C2C certified runtime commit `c09a71b`; V3 API stack remains isolated on `df-v3-net` with execution workers disabled.
- Service packaging evidence: prior `svc-face` Dockerfile copied `desifaces_shared` and shared LLM helpers but did not include `services/shared/df_contracts`; C3 adds the canonical package as `/app/df_contracts`.

### Tests/operations

- Existing tests: `test/test_v3_adapter_primitives.py` establishes shared status/error/idempotency/context behavior.
- Added tests: `test/test_v3_face_adapter.py` and `test/test_v3_account_context.py`.
- Health/monitoring/runbook dependencies: public `/api/health` behavior is unchanged; the new probe is hidden from OpenAPI and only mounted when the V3 flag is enabled.

## 4. Evidence gaps

- The GitHub EOS/EIP foundation branch does not yet expose current indexed Face symbols/API/database evidence at the same freshness as the backend repository; exact current-state findings above therefore come from direct repository/runtime inspection.
- Unit tests and the rebuilt V3 `svc-face` container have not yet been executed on the Azure V3 workspace for this implementation checkpoint.
- The hidden adapter probe has not yet been invoked with a real authenticated V3 user payload.
- V3-C4 has not yet frozen the final physical persistence model for canonical `MediaAsset`; this C3 adapter only establishes canonical contract mapping and owned UUID continuity.
- The current pricing confirmation may use non-UUID quote IDs; canonical Pricing quote identity will be completed by the Pricing bridge. C3 does not synthesize a UUID when the current quote ID is non-UUID.

## 5. V3 disposition

Disposition: `ADAPT`

Rationale:

The current public Face API is operational and is an active mobile compatibility contract, but its implementation vocabulary includes legacy/wrapped request variants, raw source URL/asset references, service-specific job states and current pricing-confirmation identifiers. V3 therefore preserves the HTTP contract at the boundary and adapts it to canonical V3 contracts rather than copying those compatibility details into the generic domain.

## 6. #v3-core architecture decision

V3 Face uses an adapter-first architecture:

`current Face HTTP payload → Face compatibility adapter → RequestContext + FaceGenerationParameters + GenerationRequest → canonical job/media lifecycle → Face compatibility response`

The adapter MUST:

- collapse current request aliases before the canonical boundary;
- require an existing canonical account identity resolved from billing-account persistence;
- treat raw URLs and opaque legacy asset references as compatibility metadata, not canonical media identity;
- use an owned existing UUID media asset only after ownership verification;
- preserve non-UUID current pricing quote IDs as compatibility metadata until the Pricing bridge supplies canonical quote identity;
- use shared idempotency, status and error normalization primitives;
- remain provider-neutral.

The first runtime integration is a hidden read-only probe, not an execution cutover.

## 7. Contract impact

- Canonical contract changes: additive `FaceGenerationParameters`, `FaceSubject`, `FacePricingConfirmationCompat`, `FaceGenerateAdapterResult` and Face mapping helpers under `services/shared/df_contracts/v3/face_adapter.py`.
- Versioning impact: additive V3 internal contract; current public Face HTTP contract remains unchanged.
- Compatibility adapter required: yes; implemented as pure shared adapter plus V3-only service probe.
- Client impact: none for current mobile/web clients.

## 8. Database impact

- Schema change: `none`.
- Migration file: `N/A`.
- Data backfill/reconciliation: existing pricing billing-account backfill and existing media UUIDs are reused as evidence; no C3 write/backfill is performed.
- Rollback/compensating action: disable/remove the V3 adapter flag/probe and revert additive code; no data compensation required.
- Confirm V3-only DB execution: probe is enabled only from `docker-compose.v3.yml` and targets the already-certified V3 database through the V3 runtime.

## 9. Security and privacy impact

- Authentication: hidden probe reuses `svc-face` authenticated `get_current_user_id`.
- Authorization/account ownership: account ID is resolved through active billing-account membership/account data; source media UUID is accepted canonically only after `media_assets.user_id` ownership verification.
- Secrets: no secrets added to code, logs, contracts or evidence.
- PII/media/privacy: adapter does not log prompts, raw image URLs or media contents; probe returns mapping only to the authenticated user request context.
- Audit requirements: later execution cutover must persist request/correlation/idempotency/generation references; this probe is read-only validation.

## 10. Pricing/entitlement/credit impact

- Pricing: existing Face pricing preview/reservation behavior remains unchanged.
- Entitlement: no change in this checkpoint.
- Credits/ledger/idempotency: shared deterministic idempotency is introduced at the canonical adapter boundary; no credit mutation occurs in the probe.
- Provider billing events: none; provider execution remains disabled for this probe.
- Quote compatibility: UUID quote IDs populate canonical `GenerationRequest.pricing_quote_id`; non-UUID current IDs are preserved explicitly as compatibility metadata and are not fabricated.

## 11. Provider/model impact

- Provider-specific behavior inspected: current Face request contains provider-facing `image_size_hint` and execution implementation remains in existing orchestrator/provider code.
- Canonical normalization: `image_size_hint` is excluded from typed canonical Face parameters and retained only as compatibility/provider-hint metadata where present.
- Routing/failover impact: none in this checkpoint.

## 12. Implementation scope

- Files/services expected to change:
  - `services/shared/df_contracts/v3/adapters.py` (existing shared primitives)
  - `services/shared/df_contracts/v3/face_adapter.py`
  - `services/shared/df_contracts/v3/__init__.py`
  - `services/shared/python/desifaces_shared/identity/*`
  - `services/svc-face/app/Dockerfile`
  - `services/svc-face/app/app/services/v3_face_adapter_shadow.py`
  - `services/svc-face/app/app/api/v3_adapter_probe.py`
  - `services/svc-face/app/app/main.py`
  - `docker-compose.v3.yml`
  - focused tests and this evidence record.
- Explicitly out of scope:
  - replacing `CreatorOrchestrator`;
  - enabling Face provider workers;
  - changing public Face paths or response models;
  - changing Face pricing/credits;
  - schema changes;
  - final C4 media persistence design;
  - Audio/Fusion adapter implementation.

## 13. Compatibility / migration strategy

The current Face API remains the client contract. The V3-only probe validates real authenticated payload translation without invoking the existing generation execution path. This permits contract and identity/media mapping certification before execution cutover.

During migration:

1. current clients continue using the same endpoints;
2. V3 mapping is validated independently;
3. canonical account/media/idempotency references are proven;
4. execution orchestration is switched behind the existing API only after adapter certification;
5. legacy Face endpoints remain deprecation candidates until caller telemetry proves they are unused;
6. V2 production is unaffected by the V3-only Compose flag and branch.

## 14. Test and certification plan

- Unit tests:
  - `test/test_v3_adapter_primitives.py`
  - `test/test_v3_face_adapter.py`
  - `test/test_v3_account_context.py`
- Contract tests:
  - confirm public Face OpenAPI `.paths` remains identical because the probe is `include_in_schema=False`;
  - confirm flat and wrapped Face payloads produce the same canonical parameter representation.
- Integration tests:
  - rebuild V3 `svc-face` image with packaged `df_contracts`;
  - verify service health;
  - call hidden adapter probe with authenticated T2I payload;
  - call hidden adapter probe with an owned I2I source asset and confirm `source_media_ids` mapping;
  - verify non-UUID pricing quote compatibility behavior.
- Migration tests: N/A for schema; verify existing billing-account and media ownership lookups in V3 clone.
- Runtime/end-to-end certification:
  - no provider worker/scheduler becomes active;
  - no studio job count/credit balance changes from probe calls;
  - V2 health remains unchanged.
- V2 regression protection:
  - no public path removal/rename;
  - V3-only flag in `docker-compose.v3.yml`;
  - hidden probe excluded from OpenAPI;
  - no mutation on probe path.

## 15. Final certification evidence

Complete before marking `CERTIFIED`.

- Commit/PR: implementation branch `feature/v3-c3-canonical-adapters-20260817`; final checkpoint pending.
- Test result: pending Azure workspace execution.
- Runtime evidence: pending V3 Face rebuild/probe certification.
- Migration/schema evidence: schema change `N/A`; existing V3 cloned account/media tables will be verified during probe certification.
- #v3-core document updated: `yes` — `V3-C3_CANONICAL_ADAPTER_MATRIX.md` is the frozen critical-path adapter design.

## 16. Freeze statement

`For V3-C3 Face migration, the current Face HTTP contract is preserved at the compatibility boundary while canonical V3 request context, account ownership, Face generation parameters, generation/job state, media identity and idempotency are introduced behind it. Raw transport URLs, provider hints, request aliases and non-canonical pricing identifiers must not become generic V3 domain fields. Any change to this boundary strategy requires returning to #v3-core.`
