# V3-C3 API Inventory and Compatibility

Change-ID: `V3-C3`
Status: `CERTIFIED`
Owner: `#v3-core`
Date: `2026-08-17`
Backend evidence anchor: `70a80cef08cebb8f02385a8e0f1adbac7c85fbb8`
Frontend/mobile evidence anchor: `e1c710c7e42af423ae9bf6256ffe2fa04a871b4c`
V3 implementation branch: `feature/v3-c3-canonical-adapters-20260817`

## 1. Purpose

This record closes V3-C3 by inventorying the mounted desifaces-v2 API surface, mapping current callers and service-to-service dependencies, classifying compatibility disposition, and freezing the desifaces-v3 API/versioning strategy.

The inventory is intentionally based on the exact frozen V2 backend and frontend/mobile anchors, not on route names inferred from documentation.

The previously certified critical creator path remains governed by:

- `V3-C3_CANONICAL_ADAPTER_MATRIX.md`
- `V3-C3_FACE_CANONICAL_ADAPTER.md`
- `V3-C3_AUDIO_CANONICAL_ADAPTER.md`
- `V3-C3_FUSION_CANONICAL_ADAPTER.md`
- `V3-C3_PRICING_CANONICAL_ADAPTER.md`
- `V3-C3_CRITICAL_PATH_CERTIFICATION.md`

The critical Face -> Audio -> Fusion -> Pricing path is already runtime/authenticated certified. This record adds the remaining non-critical route families and closes the overall API inventory milestone.

## 2. Evidence and inventory rules

### 2.1 What counts as a current API

A route counts as part of the certified V2 API surface only when its router is mounted by the service application at the frozen backend anchor.

A Python route module that exists in the repository but is not mounted is dormant code, not an API compatibility contract.

### 2.2 Caller evidence

Caller evidence is categorized as:

- `MOBILE`: executable code in the frozen frontend/mobile anchor calls the route.
- `SERVICE`: backend service-to-service code calls the route.
- `PROVIDER`: an external payment/provider callback targets the route.
- `ADMIN/OPS`: route is intended for operational or administrative use.
- `NO FROZEN MOBILE CALLER`: no executable caller exists in the frozen mobile tree. This does not prove there is no other external client.

### 2.3 Classification vocabulary

- `PRESERVE`: keep the current public contract.
- `PRESERVE + NORMALIZE`: keep the external contract while translating to canonical V3 identity/domain/error/meta semantics internally.
- `ADAPT`: preserve the compatibility contract through a typed V3 capability adapter.
- `REPLACE IMPLEMENTATION`: keep the route but replace a non-compliant implementation, such as hard-coded masterdata.
- `INTERNALIZE`: retain the capability but classify it as service/admin/ops/provider-only rather than a general client API.
- `DEPRECATE CANDIDATE`: do not remove immediately; collect usage evidence, provide a supported replacement, record an explicit `#v3-core` decision, then sunset.
- `DORMANT / NOT CONTRACT`: source exists but is not mounted and therefore is not part of the certified live API surface.

## 3. Frozen V3 API/versioning strategy

The following rules are now frozen.

1. Existing certified V2 paths remain compatibility paths during V3 migration. A mass rename to `/api/v3` is prohibited.
2. Existing compatibility routes translate into `df_contracts.v3` canonical contracts at the service boundary.
3. Net-new public V3 capabilities that do not need V2 compatibility SHOULD use an explicit `/api/v3/...` public namespace.
4. Net-new internal service routes SHOULD use an explicit `/api/internal/...` or equivalent service-only namespace and service identity/authorization.
5. Hidden certification probes remain `include_in_schema=False` and are never public product APIs.
6. Canonical contract version is explicit through the `df_contracts.v3` package and `ApiMeta.contract_version = "v3"` where envelopes are used.
7. Existing V2 response shapes may remain at the compatibility façade; canonical domain objects MUST NOT absorb V2 aliases solely to maintain transport compatibility.
8. Deprecation requires caller telemetry/evidence, replacement mapping, explicit `#v3-core` approval, and a defined sunset. No endpoint is deleted merely because a cleaner V3 name exists.
9. Provider callback endpoints are versioned/governed by the provider integration contract, not treated as user-facing REST APIs.
10. Health/readiness endpoints are operational contracts and SHOULD converge on a standard health/readiness schema without breaking current load-balancer/runtime checks.

## 4. Cross-service identity and authorization rule

All V3-capable public and service operations MUST resolve or carry:

- canonical `account_id`
- canonical `user_id` when acting for a user
- `RequestActor`
- request/correlation identity
- service identity for service-to-service calls
- idempotency identity for mutating or expensive operations

The V3 canonical account-context migration establishes the durable account invariant for existing and future V3 users.

Routes that currently authenticate only a token but mutate masterdata/provider state are not considered adequately authorized for V3; they are classified `INTERNALIZE` and require explicit admin/service authorization.

## 5. svc-core inventory

Mounted business routers at the frozen anchor: health, auth, masterdata, notifications, support, help, internal notifications.

### 5.1 Authentication

Prefix: `/api/auth`

| Route | Caller/exposure | V3 disposition | V3 mapping / action |
|---|---|---|---|
| `POST /api/auth/register` | MOBILE/public | PRESERVE + NORMALIZE | Create user/account identity; pricing bootstrap becomes internal dependency. |
| `POST /api/auth/register/verify-email` | public registration | PRESERVE + NORMALIZE | Identity verification state. |
| `POST /api/auth/register/resend-email-code` | public registration | PRESERVE + NORMALIZE | Identity verification operation. |
| `POST /api/auth/login` | MOBILE/public | PRESERVE + NORMALIZE | Canonical UserRef/RequestActor. |
| `GET /api/auth/me` | public authenticated | PRESERVE + NORMALIZE | Canonical user/account projection. |
| `POST /api/auth/refresh` | MOBILE/public | PRESERVE | Token lifecycle. |
| `POST /api/auth/logout` | MOBILE/public | PRESERVE | Session/token lifecycle. |
| `POST /api/auth/password/change/start` | authenticated | PRESERVE | Credential lifecycle. |
| `POST /api/auth/password/change/confirm` | authenticated | PRESERVE | Credential lifecycle. |
| `POST /api/auth/password/reset/start` | public | PRESERVE | Credential recovery. |
| `POST /api/auth/password/reset/confirm` | public | PRESERVE | Credential recovery. |
| `POST /api/auth/forgot-password` | MOBILE compatibility alias | PRESERVE compatibility | Alias of reset-start behavior; do not remove while current clients depend on it. |
| `POST /api/auth/reset-password` | MOBILE compatibility alias | PRESERVE compatibility | Alias of reset-confirm behavior. |

Core registration currently calls Pricing free-user bootstrap as a backend dependency. That dependency is classified service-only under Pricing below.

### 5.2 Masterdata

Prefix: `/core/api/masterdata`

| Route | Disposition | Action |
|---|---|---|
| `GET /core/api/masterdata/version` | PRESERVE + NORMALIZE | Version/revision source for DB-backed masterdata. |
| `GET /core/api/masterdata/face` | PRESERVE + NORMALIZE | Authenticated, ETag/revision-aware Face masterdata. |
| `GET /core/api/masterdata/tts` | PRESERVE + NORMALIZE | Authenticated, ETag/revision-aware TTS masterdata. |

These APIs align with the frozen globalization rule: geography/language/voice/cultural behavior is data-driven, not source-hardcoded.

### 5.3 Notifications

Prefix: `/api/notifications`

- `GET /api/notifications`
- `GET /api/notifications/unread-count`
- `POST /api/notifications/{item_id}/read`
- `POST /api/notifications/read-all`
- `GET /api/notifications/preferences`
- `PUT /api/notifications/preferences`
- `POST /api/notifications/devices/register`

Disposition: `PRESERVE + NORMALIZE`.

These are authenticated user APIs. V3 should standardize envelope/error/meta semantics while preserving behavior.

### 5.4 Internal notification event ingestion

- `POST /api/internal/notifications/events`

Disposition: `INTERNALIZE`.

The current route already requires service authentication. Pricing/payment components use this path for best-effort customer notifications. It remains a service-only contract.

### 5.5 Support

Prefix: `/api/support`

- `POST /api/support/contact`
- `GET /api/support/requests`
- `GET /api/support/requests/{request_id}`
- `POST /api/support/requests/{request_id}/reply`

Disposition: `PRESERVE` with canonical account ownership and common error/meta normalization.

### 5.6 Help

Prefix: `/api/help`

- `GET /api/help/categories`
- `GET /api/help/faq`
- `GET /api/help/articles/{slug}`

Disposition: `PRESERVE` as public/read-only help content.

### 5.7 Core dormant routes

Any Core admin/experimental route module not included by `svc-core/app/app/main.py` is `DORMANT / NOT CONTRACT` for C3. V3 must not preserve a route merely because a source file exists.

## 6. svc-face inventory

Current Face router is mounted under `/api/face`.

### 6.1 Current Creator/public routes

| Route | Caller | Disposition |
|---|---|---|
| `POST /api/face/creator/i2i/content-safety/check` | MOBILE/studio | PRESERVE + NORMALIZE -> `SafetyDecision` |
| `POST /api/face/assets/upload` | MOBILE | ADAPT -> canonical `MediaAsset(role=source)` in C4 |
| `POST /api/face/creator/pricing/preview` | studio | ADAPT -> canonical `PricingQuote` |
| `POST /api/face/creator/prompt/enhance` | creator capability | PRESERVE + NORMALIZE |
| `POST /api/face/creator/tips` | creator capability | PRESERVE + NORMALIZE |
| `POST /api/face/creator/generate` | MOBILE | ADAPT -> `GenerationRequest(kind=face)` |
| `GET /api/face/creator/jobs/{job_id}/status` | MOBILE | ADAPT -> `GenerationJob` + output media |
| `GET /api/face/creator/jobs` | MOBILE | PRESERVE + NORMALIZE -> filtered canonical jobs |
| `GET /api/face/creator/jobs/{job_id}/status-light` | compatibility/optimized polling | PRESERVE + NORMALIZE pending caller telemetry |
| `GET /api/face/profiles` | MOBILE | PRESERVE + NORMALIZE; media identity converges in C4 |
| `GET /api/face/config/regions` | MOBILE | PRESERVE + NORMALIZE DB masterdata |
| `GET /api/face/config/countries` | MOBILE | PRESERVE + NORMALIZE DB masterdata |
| `GET /api/face/config/subdivisions` | MOBILE | PRESERVE + NORMALIZE DB masterdata |
| `GET /api/face/config/contexts` | MOBILE | PRESERVE + NORMALIZE DB masterdata |

### 6.2 Legacy Face routes

- `POST /api/face/generate`
- `GET /api/face/jobs/{job_id}`
- `GET /api/face/jobs`

Disposition: `DEPRECATE CANDIDATE`.

Frozen mobile caller evidence shows the active Face service layer uses Creator APIs only. Removal still requires backend/other-client telemetry and an explicit sunset decision.

### 6.3 Operational Face route

- `POST /api/face/creator/internal/recovery/sweep`

Disposition: `INTERNALIZE`.

This is an operational recovery trigger, not a user API. V3 must require explicit admin/service authorization and keep automatic execution separately certified.

## 7. svc-audio inventory

### 7.1 Audio generation and creator assistance

- `POST /api/audio/tts/pricing/preview` -> `ADAPT` to canonical PricingQuote
- `POST /api/audio/tts/prompt/enhance` -> `PRESERVE + NORMALIZE`
- `POST /api/audio/tts/tips` -> `PRESERVE + NORMALIZE`
- `POST /api/audio/tts` -> `ADAPT` to `GenerationRequest(kind=audio)`
- `GET /api/audio/jobs/{job_id}/status` -> `ADAPT` to canonical job/media state

Compatibility-only pricing aliases also exist:

- `POST /api/audio/pricing/preview` (`include_in_schema=False`)
- `POST /api/audio/tts/preview` (`include_in_schema=False`)

Disposition: `DEPRECATE CANDIDATE`; primary supported path is `/api/audio/tts/pricing/preview`.

### 7.2 Audio catalog

- `GET /api/audio/catalog/locales`
- `GET /api/audio/catalog/countries`
- `GET /api/audio/catalog/target-languages`
- `GET /api/audio/catalog/voices`

Disposition: `PRESERVE + NORMALIZE`.

Frozen mobile code actively calls all four read APIs. Current backend implementation derives availability from DB/provider capability tables and treats `market` as a deprecated compatibility parameter, matching the V3 globalization rule.

### 7.3 Catalog mutation

- `POST /api/audio/catalog/sync`

Disposition: `INTERNALIZE`.

Frozen mobile code does not call this route. It synchronizes provider/masterdata state and therefore requires explicit admin/service authorization in V3. Current generic authenticated-claims protection is insufficient as the final governance boundary.

## 8. svc-fusion inventory

Current public service paths are rooted at the Fusion service base URL.

- `POST /jobs/pricing/preview` -> `ADAPT` to canonical PricingQuote
- `POST /jobs` -> `ADAPT` to `GenerationRequest(kind=fusion)`
- `GET /jobs/{job_id}` -> `ADAPT` to canonical job/provider/media view
- `GET /jobs/{job_id}/status-light` -> `PRESERVE + NORMALIZE`
- `GET /jobs/{job_id}/status` -> `ADAPT`
- `POST /internal/recovery/sweep` -> `INTERNALIZE`

### Required V3 auth hardening

The frozen source shows create/preview obtaining user identity, while the job read handlers do not themselves carry the same explicit user ownership dependency. V3 compatibility implementations MUST enforce account/user ownership for all job reads. Knowledge of a job UUID is never authorization.

### Provider/billing normalization

- provider name/job/status maps to `ProviderExecution`, not generic `GenerationRequest`.
- internal child-render pricing suppression remains internal orchestration metadata.
- child renders must not create independent customer charges when billed by a Longform parent.

## 9. svc-dashboard inventory

Prefix: `/api/dashboard`

- `GET /api/dashboard/home`
- `GET /api/dashboard/header`
- `GET /api/dashboard/library`
- `POST /api/dashboard/refresh`

Disposition: `PRESERVE + NORMALIZE`.

Dashboard is a read/projection capability. In C4, library/media results must become projections over canonical `MediaAsset`, while preserving current final-output/library semantics. Refresh is cache/projection refresh, not provider execution.

## 10. svc-fusion-extension / Longform inventory

Mounted business router: `/api/longform`.

- `POST /api/longform/pricing/preview`
- `POST /api/longform/jobs`
- `GET /api/longform/jobs/{job_id}`
- `GET /api/longform/jobs/{job_id}/segments`

Disposition: `ADAPT`.

Current mobile Fusion code depends on Longform. Compatibility remains required.

V3 target direction:

`Longform compatibility request -> Story/Scene/Director orchestration -> child GenerationJobs -> final MediaAsset`

The current implementation contains extensive alias/profile/provider normalization and calls Pricing/Fusion internally. Those details terminate at the compatibility boundary. V3 workers remain disabled until a separate execution milestone certifies them.

## 11. svc-music inventory

Mounted routers: health, catalog, projects, jobs, assets, support.

Frozen mobile evidence: Music screens/features at the exact frontend anchor are zero-byte placeholders. Therefore Music has no executable current-mobile caller obligation. The backend capability is nevertheless retained for V3 product evolution.

### 11.1 Catalog

- `GET /api/music/catalog`

Disposition: `PRESERVE + REPLACE IMPLEMENTATION`.

Current catalog data is hard-coded in Python source. V3 MUST move modes/layouts/camera edits/band packs/scene packs to DB-backed/versioned masterdata/capability metadata.

### 11.2 Projects and creative state

- `POST /api/music/projects`
- `GET /api/music/projects/{project_id}`
- `PATCH /api/music/projects/{project_id}`
- `POST /api/music/projects/{project_id}/voice-reference`
- `GET /api/music/projects/{project_id}/voice-reference`
- `GET /api/music/projects/{project_id}/performers`
- `POST /api/music/projects/{project_id}/performers`
- `GET /api/music/projects/{project_id}/lyrics`
- `POST /api/music/projects/{project_id}/lyrics`

Disposition: `PRESERVE + NORMALIZE` as Music capability/project state. Media links normalize in C4; Project/Participant relationships align with canonical creative-domain references where applicable.

### 11.3 Generation/publish

- `POST /api/music/projects/{project_id}/generate`
- `GET /api/music/jobs/{job_id}/status`
- `POST /api/music/jobs/{job_id}/publish`

Disposition: `ADAPT`.

Generation/status converge on canonical generation/job/provider/media semantics. Publish remains a higher-level orchestration/consent capability and must not expose provider implementation details as domain state.

### 11.4 Music assets

- `POST /api/music/assets/upload`
- `GET /api/music/assets/{artifact_id}`

Disposition: `ADAPT` in C4.

Current upload can create both legacy `artifact_id` and optional `media_asset_id`. This dual identity is direct migration evidence: V3 canonical identity is `MediaAsset`; legacy artifacts remain compatibility references until C4 migration is certified.

### 11.5 Music support/audit

User-facing:

- `POST /api/music/support/sessions/upsert`
- `POST /api/music/support/events`

Disposition: `PRESERVE + NORMALIZE` if Music support remains a product capability.

Admin/ops:

- `POST /api/music/support/admin/events`
- `POST /api/music/support/admin/events/query`
- `GET /api/music/support/admin/sessions/{session_id}/verify-chain`

Disposition: `INTERNALIZE / ADMIN`.

## 12. svc-commerce inventory

Frozen mobile evidence: Retail/Commerce app and feature files are zero-byte placeholders at the frozen frontend anchor. There is no executable current-mobile Commerce caller. Backend Commerce remains a V3 product capability rather than a V2 mobile compatibility obligation.

### 12.1 Products

- `POST /api/commerce/merchants/{merchant_id}/products`
- `GET /api/commerce/merchants/{merchant_id}/products`
- `GET /api/commerce/products/{product_id}`
- `POST /api/commerce/products/{product_id}/validate`
- `POST /api/commerce/products/{product_id}/publish`

Disposition: `PRESERVE + NORMALIZE` for Commerce domain state, ownership and publication. Media fields converge on canonical MediaAsset in C4.

### 12.2 Primary quote/campaign/generation surface

The supported implementation in `commerce_quotes.py` exposes:

- `POST /api/commerce/quote`
- `POST /api/commerce/confirm`
- `GET /api/commerce/jobs/{studio_job_id}/status`
- `GET /api/commerce/gallery`
- `GET /api/commerce/campaigns/{campaign_id}`

Disposition:

- quote -> `PRESERVE + NORMALIZE`, ultimately canonical pricing policy/quote integration
- confirm -> `ADAPT` to Commerce generation/job orchestration
- job status -> `ADAPT` to canonical GenerationJob/provider/media semantics
- gallery/campaign detail -> `PRESERVE + NORMALIZE`, media projections in C4

### 12.3 Duplicate Commerce campaign handlers

`commerce_campaigns.py` is also mounted and defines overlapping:

- `POST /api/commerce/confirm`
- `GET /api/commerce/jobs/{studio_job_id}/status`

Meanwhile `/api/commerce/tryon/help` explicitly identifies `commerce_quotes.py` as the primary endpoint implementation.

Disposition: `DEPRECATE/REMOVE DUPLICATE CANDIDATE` after route-order/runtime verification and explicit C3/C4 implementation work. V3 MUST have one authoritative handler per method/path.

### 12.4 Commerce assets

- `POST /api/commerce/assets/upload`
- `GET /api/commerce/assets/{asset_id}`
- `GET /api/commerce/assets/{asset_id}/view`

Disposition: `ADAPT` in C4.

These routes already use `public.media_assets`, SHA-256 deduplication, ownership checks and stable storage references. They are strong C4 reuse candidates but still need canonical role/lineage/lifecycle normalization.

### 12.5 Commerce templates/masterdata

- `GET /api/commerce/templates/components`
- `GET /api/commerce/templates/combinations`
- `GET /api/commerce/templates/asset_roles`

Disposition: `PRESERVE + NORMALIZE` as DB-backed/versioned Commerce masterdata.

### 12.6 Placeholder/introspection routes

- `GET /api/commerce/looksets/ping`
- `GET /api/commerce/tryon/help`
- `GET /commerce/exports/ping`

Disposition: `DEPRECATE CANDIDATE / NOT A PERMANENT V3 PRODUCT CONTRACT`.

These are placeholder/diagnostic routes rather than complete domain APIs.

### 12.7 Training operations

- `POST /training/start`
- `GET /training/{checkpoint_id}/status`
- `POST /training/resume`

Disposition: `INTERNALIZE / ADMIN`.

These routes directly start/poll/resume model-provider training and are not current-mobile APIs. V3 must place them behind explicit administrative/service authorization and operational governance. They must never remain broadly reachable merely because they are mounted.

## 13. svc-pricing inventory

Pricing is split into customer/public APIs, service orchestration APIs, provider callbacks and internal bootstrap.

### 13.1 Quote

- `POST /api/pricing/quote`

Disposition: `PRESERVE + NORMALIZE`.

This remains the compatibility pricing-transparency/entitlement view. Generation quote identity/fingerprint/expiry is governed by reservation preview as frozen by the critical-path certification.

### 13.2 Public credit balance

- `GET /api/credits/balance`

Disposition: `PRESERVE + NORMALIZE`.

Current mobile pricing code calls this route.

### 13.3 Legacy credit mutation family

Mounted legacy routes include:

- `POST /api/credits/reserve`
- `POST /api/credits/finalize`
- `POST /api/credits/release`

An older service client also expects `/api/credits/reservations/{reservation_id}`, although that read path is not present in the mounted Credits router evidence.

Disposition: `DEPRECATE CANDIDATE` for mutation/orchestration use.

The canonical service path is the newer shared pricing client below. Existing legacy service callers must be migrated before removal.

### 13.4 Canonical service reservation HTTP contract

- `POST /api/pricing/reservations/preview`
- `POST /api/pricing/reservations/reserve`
- `POST /api/pricing/reservations/commit`
- `POST /api/pricing/reservations/release`
- reservation read route(s) retained where currently exposed for compatibility/ops

Disposition: `PRESERVE + NORMALIZE AS SERVICE CONTRACT`.

The shared `desifaces_shared.pricing.SvcPricingClient` sends `X-Service-Name`, `X-User-Id` and internal bearer identity. These are service orchestration operations, not normal end-user mutation APIs.

V3 mapping:

`PricingQuote -> CreditReservation -> commit/release -> immutable CreditTransaction`

### 13.5 Payment/subscription customer APIs

Mounted under `/api/payments` and retained for C6 normalization include:

- `POST /api/payments/customer/sync`
- `GET /api/payments/payment-methods`
- `POST /api/payments/wallet/topups/create-checkout-session`
- `GET /api/payments/wallet/orders/{wallet_order_id}`
- `POST /api/payments/subscriptions/create-checkout-session`
- `GET /api/payments/plans/catalog`
- `GET /api/payments/subscriptions/current`
- `GET /api/payments/overview`
- `GET /api/payments/topups/catalog`
- `POST /api/payments/customer-portal/create-session`
- `POST /api/payments/subscriptions/change`
- `POST /api/payments/subscriptions/undo-pending-change`
- `POST /api/payments/subscriptions/cancel`
- `POST /api/payments/subscriptions/reactivate`

Disposition: `PRESERVE + NORMALIZE`, owner `C6`.

C6 must consolidate entitlement/subscription/credit-period behavior and fix recurring subscription-credit replenishment rather than reproducing the V2 defect.

### 13.6 Mobile-store confirmation APIs

- `POST /api/payments/apple/subscriptions/confirm`
- `POST /api/payments/apple/credits/confirm`
- `POST /api/payments/google/subscriptions/confirm`
- `POST /api/payments/google/credits/confirm`

Disposition: `PRESERVE + NORMALIZE`, C6. These are authenticated channel-specific purchase confirmation APIs.

### 13.7 Provider notification/webhook endpoints

- `POST /api/payments/apple/notifications`
- `POST /api/payments/google/notifications`
- `POST /api/payments/webhooks/stripe`

Disposition: `INTERNALIZE AS EXTERNAL-PROVIDER CALLBACK`.

They remain network-reachable only as required by the provider integration, with signature/authenticity verification, event persistence, replay safety and idempotent fulfillment. They are not general client APIs.

The Stripe implementation already records gateway event identity/status before processing; V3 retains and strengthens that pattern.

### 13.8 Internal free-user bootstrap

- `POST /api/pricing/bootstrap/free-user`

Disposition: `INTERNALIZE`.

The route explicitly requires an internal Pricing caller and is invoked by Core registration. It is not a public account-management API.

### 13.9 Mobile pricing summary fallback aliases

Frozen frontend code defines candidates:

Plan:

- `/api/pricing/plan-summary`
- `/api/pricing/account-summary`
- `/api/pricing/summary`

Usage:

- `/api/pricing/usage-summary`
- `/api/pricing/usage`
- `/api/pricing/account-summary`

No matching backend route literals were found at the frozen backend anchor. The live Pricing capability instead has `/api/payments/overview`, plan catalog/current subscription and credit balance.

Disposition: `CLIENT DEPRECATION / CONSOLIDATION CANDIDATE`, not a backend compatibility contract. Do not create six V3 aliases merely to satisfy unused/fallback client code. The client should converge on the supported payments overview/account projection when the consuming UI is finalized.

## 14. svc-marketing inventory

Mounted routers: marketing runs, schedules, health.

Frozen frontend evidence contains no Marketing path or executable mobile caller.

### 14.1 Marketing runs

- `POST /api/marketing/runs`
- `GET /api/marketing/runs/{run_id}/status`

Disposition: `PRESERVE + NORMALIZE` as a V3 product capability, not a V2 mobile compatibility obligation.

### 14.2 Publish

- `POST /api/marketing/runs/{run_id}/publish`

Current code requires the configured marketing admin user.

Disposition: `INTERNALIZE / ADMIN`.

### 14.3 Schedules

Prefix `/api/marketing/admin`:

- `POST /api/marketing/admin/schedules`
- `GET /api/marketing/admin/schedules`
- `POST /api/marketing/admin/schedules/{schedule_id}/toggle`

Disposition: `INTERNALIZE / ADMIN`.

### 14.4 Dormant Marketing modules

Other source modules such as metrics/use-case admin routes that are not mounted by `svc-marketing/app/app/main.py` are `DORMANT / NOT CONTRACT` for C3.

## 15. Health, root and documentation endpoints

Every service currently exposes some combination of health, root, docs, redoc and OpenAPI endpoints.

Disposition:

- health/readiness -> `PRESERVE + STANDARDIZE`
- OpenAPI/docs -> operational/developer surface controlled by environment
- service root/ping pages -> not business-domain contracts unless explicitly used by platform health checks

V3 target health contract should distinguish at minimum:

- process liveness
- dependency readiness
- service/version identity
- optional degraded dependency status

Standardization must not break current Compose/load-balancer checks.

## 16. Backend service-to-service dependency inventory

### 16.1 Core -> Pricing

Core registration invokes `/api/pricing/bootstrap/free-user` with internal bearer, `X-User-Id` and service identity.

Disposition: internal service contract.

### 16.2 Face/Audio/Fusion/Longform/Commerce -> Pricing

The modern shared pricing client owns:

- preview
- reserve
- commit
- release

using `/api/pricing/reservations/*` plus service/user headers and internal bearer authentication.

Disposition: canonical service orchestration boundary.

### 16.3 Longform -> Fusion

Longform orchestrates child Fusion jobs using backend HTTP/service identity. It must not persist short-lived user JWTs for asynchronous work.

Disposition: internal service contract; future Director/Story/Scene orchestration.

### 16.4 Music -> Fusion/video publication

Music generation/publish integrates with video/Fusion orchestration.

Disposition: internal capability dependency; provider/service details terminate below canonical Generation/Media contracts.

### 16.5 Commerce -> Pricing and generation providers/services

Commerce quote/generation code uses the shared pricing client and provider/generation adapters.

Disposition: internal dependency; canonical PricingQuote/CreditReservation/Generation/Media boundaries apply.

### 16.6 Pricing -> Core Notifications

Payments/webhook code emits best-effort notifications to `/api/internal/notifications/events` using service bearer identity.

Disposition: internal service contract.

## 17. Frozen deprecation and internalization register

### 17.1 Deprecation candidates

The following are not removed by this decision, but are approved deprecation candidates:

1. legacy Face `/api/face/generate` and `/api/face/jobs*`
2. hidden Audio pricing aliases `/api/audio/pricing/preview` and `/api/audio/tts/preview`
3. legacy Pricing `/api/credits/reserve|finalize|release` after remaining old service callers migrate
4. obsolete/unimplemented mobile Pricing summary aliases
5. duplicate Commerce `commerce_campaigns.py` confirm/status handlers once the `commerce_quotes.py` primary path is verified as sole runtime handler
6. Commerce placeholder routes `looksets/ping`, `tryon/help`, `exports/ping`
7. other status-light/compatibility aliases only after runtime caller telemetry proves they are unused

### 17.2 Internal/admin/provider-only register

The following are explicitly non-general-client capabilities:

- Core internal notification event ingestion
- Face recovery sweep
- Audio catalog sync
- Fusion recovery sweep
- Longform workers/execution controls
- Music support admin audit/query/verify operations
- Commerce model-training start/status/resume
- Marketing publish and schedules
- Pricing free-user bootstrap
- Pricing reservation mutations when invoked as service orchestration
- Apple/Google notification callbacks
- Stripe webhook
- any provider-control, recovery, scheduler or worker-control API

## 18. Security findings carried forward

C3 identified security/governance hardening that implementation milestones MUST address:

1. Fusion job read paths require explicit user/account ownership enforcement.
2. Audio catalog sync requires explicit admin/service authorization, not generic authenticated claims.
3. Commerce training routes require explicit admin/service authorization and should move under an internal/admin namespace.
4. Duplicate Commerce method/path handlers must be eliminated so route ownership is deterministic.
5. Internal service routes must consistently verify service identity, acting user and account context.
6. Provider callbacks must verify provider authenticity and remain replay-safe.
7. Knowledge of a UUID/job/media/reservation identifier is never authorization.

## 19. Masterdata/globalization findings carried forward

1. Core and Audio masterdata are already substantially DB/version/capability driven and are strong V3 reuse candidates.
2. Face config is DB-driven and remains compatibility surface.
3. Commerce templates are DB-backed and should be versioned as capability masterdata.
4. Music catalog is hard-coded in source and MUST be replaced with DB-backed/versioned masterdata before V3 product exposure.
5. No country/region/language/voice/provider-routing hardcoding may be added during adapter migration.

## 20. Media findings carried forward to C4

C3 provides direct evidence for C4 Media Lifecycle:

- Face upload/output currently exposes compatibility asset/media fields.
- Music upload can create both legacy artifact and media-asset identity.
- Fusion still consumes/returns legacy artifacts and raw URLs in compatibility contracts.
- Longform produces child/final artifacts requiring parent-child lineage.
- Commerce already writes `public.media_assets` with SHA-256 dedupe, ownership and stable storage refs.
- Dashboard/library is a media projection.

Therefore C4 must inventory and normalize physical persistence around canonical `MediaAsset`, including role, ownership, lineage, stable storage identity, SAS delivery, preview/final distinction, dedupe and migration from legacy artifacts.

## 21. Pricing findings carried forward to C6

C3 freezes the transport/boundary contract but does not redesign the complete subscription engine.

C6 owns:

- subscription period state
- recurring included-credit replenishment
- Apple/Google/Stripe parity
- immutable grant/consumption/refund/expiry transactions
- plan-change reset/carryover policy
- entitlement synchronization
- provider webhook replay safety
- customer payment overview projections

The known monthly renewal-credit replenishment defect MUST be resolved in C6 rather than copied into V3.

## 22. Caller conclusions

### Current frozen mobile has executable callers for

- Core auth
- Dashboard
- Face Creator/config/profile/upload
- Audio TTS/catalog
- Fusion
- Longform
- Pricing/credits/payments
- Notifications
- Support
- Help

### Frozen mobile has no executable implementation for

- Music: route/feature files are placeholders/zero-byte
- Commerce/Retail: route/feature files are placeholders/zero-byte
- Marketing: no route/path in the frozen frontend tree

This absence affects compatibility obligation, not product strategy. Music/Commerce/Marketing backend capabilities remain subject to V3 architecture and may be exposed through future V3 web/mobile/developer experiences.

## 23. C3 certification gates

| Gate | Result |
|---|---|
| V2/V3 live OpenAPI `.paths` parity across 10 services | PASS |
| Critical Face/Audio/Fusion/Pricing adapter design | PASS |
| Critical adapter unit/runtime-shell certification | PASS |
| Authenticated real V3 user/account mapping across critical path | PASS |
| Persistence invariants during authenticated probes | PASS |
| Canonical account-context persistence invariant | PASS |
| Current mobile caller inventory | PASS |
| Mounted non-critical route-family inventory | PASS |
| Internal/service/provider/admin classification | PASS |
| Legacy/duplicate/placeholder deprecation register | PASS |
| V3 API/versioning strategy frozen | PASS |
| Security hardening items recorded | PASS |
| C4/C5/C6 ownership of deferred concerns recorded | PASS |

## 24. V3-C3 final status

`V3-C3 — API Inventory & Compatibility` is **CERTIFIED**.

What is frozen:

- compatibility-first public API migration
- canonical V3 adapter boundary
- explicit versioning strategy
- route-family classifications
- service-to-service boundary classification
- provider/admin/internal separation
- deprecation candidates
- current mobile caller obligations
- critical path authenticated certification
- canonical account identity invariant
- ownership of media/generation/commerce follow-on work

No remaining C3 evidence gap blocks the next milestone.

## 25. Next milestone

Proceed to:

`V3-C4 — Canonical Media Lifecycle & Migration`

C4 should start with EIP evidence over:

1. `public.media_assets` schema and all writers/readers
2. legacy `public.artifacts` schema and all writers/readers
3. Face input/output/profile media
4. Audio output media
5. Fusion artifacts/final video/share URL
6. Longform segment/final media
7. Music asset dual-write behavior
8. Commerce media-assets implementation
9. Dashboard/Saved Work final-only projections
10. Azure Blob container/path/SAS lifecycle
11. lineage, ownership, dedupe, preview/final/thumbnail roles
12. V2 -> V3 media migration and compatibility adapters

C4 implementation must remain provider-neutral and preserve the current working V2 application while V3 media identity is introduced in parallel.

## 26. Freeze statement

The desifaces-v3 API compatibility architecture and mounted-route disposition are frozen under `#v3-core`.

Existing certified public V2 paths remain compatibility contracts during migration. Canonical V3 domain/application contracts are the internal source of truth. Internal/admin/provider routes are not promoted to public APIs. Legacy/duplicate routes may be removed only through the explicit deprecation process. New public V3 capabilities use explicit versioning rather than forcing disruptive renames of working compatibility endpoints.

V3-C3 is closed. V3-C4 is the next architecture milestone.
