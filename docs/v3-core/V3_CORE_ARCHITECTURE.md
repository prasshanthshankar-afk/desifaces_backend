# desifaces-v3 Core Architecture

Status: Initial controlled baseline
Authority: #v3-core — Architecture & Integration Control

## 1. Purpose

This document establishes the first controlled architecture baseline for desifaces-v3. V3 begins as an as-is copy of the proven V2 implementation, then evolves incrementally behind explicit shared contracts. Existing Face, Audio, Fusion, pricing, identity, dashboard, storage, and payment behavior must not be rewritten merely to conform to a theoretical target model.

EIP evidence is required before reuse, replacement, or migration of V2 behavior.

## 2. Architectural rule

The target layering is:

```text
Clients
  |
V3 API contracts
  |
Application/use-case orchestration
  |
Canonical V3 domain model
  |
Service capability boundaries
  |
Provider/storage/payment adapters
  |
Infrastructure
```

V2-derived services may temporarily contain more than one of these layers. V3 migration progressively separates them without destabilizing working flows.

## 3. Canonical cross-service domain

The first V3 contract baseline establishes these concepts.

### Identity and ownership

- AccountRef — billing/ownership boundary.
- UserRef — authenticated user within an account.
- RequestActor — user, service, API key, or system actor.
- RequestContext — request, correlation, idempotency, client, and actor context propagated between services and jobs.

### Creative domain

- ParticipantRef — a person, character, pet, or other participant in generated content.
- ProjectRef — optional durable workspace/creative-project boundary.
- ConversationRef — conversation identity that may attach to a project.
- StoryRef — story identity that may attach to a project.
- SceneRef — ordered story scene.

These references are deliberately small. Rich persistence models will be introduced only after EIP inspection of V2 tables and feature-specific requirements.

### Media lifecycle

MediaAsset is the canonical representation of media identity and lineage. It separates media kind from lifecycle role.

Kinds: image, audio, video, document, other.

Roles: source, intermediate, preview, final, thumbnail.

Media assets carry account ownership, optional user/project ownership, storage URI, source-media lineage, producing job, thumbnail relationship, and extensible metadata.

A provider URL is not the canonical media identity. Provider/storage details remain infrastructure concerns.

### Generation lifecycle

GenerationRequest describes user/product intent independently from a provider invocation.

GenerationJob tracks asynchronous execution independently from product intent.

ProviderExecution records each provider/model attempt behind a job.

This explicitly separates:

```text
User intent
   -> GenerationRequest
      -> GenerationJob
         -> ProviderExecution(s)
            -> MediaAsset(s)
```

This separation is required for retries, failover, multi-provider orchestration, accounting, observability, and future Director orchestration.

### Job state model

Canonical job states are:

- submitted
- queued
- running
- succeeded
- failed
- blocked
- canceled
- expired

Feature-specific internal states may exist but must map to this public lifecycle before cross-service/client exposure.

### Safety

SafetyDecision is independent from provider execution and records a canonical safety state:

- pending
- allowed
- blocked
- review_required

Provider moderation responses must be normalized before product logic consumes them.

## 4. Pricing, entitlement, and credits

The V3 contract layer establishes:

- PricingQuote — server-generated quote with revision, fingerprint, expiry, credit cost, and optional monetary amount.
- Entitlement — canonical plan/subscription entitlement independent of Apple, Google, Stripe, or future providers.
- CreditTransaction — immutable credit-ledger entry.

The client must never determine authoritative pricing, credits, entitlement state, or subscription-grant behavior.

Provider events are normalized first and then used to update canonical entitlement and credit state.

## 5. API contract rules

All new V3 APIs must adopt, directly or through adapters, these principles:

1. Explicit contract versioning.
2. Strict input validation; unknown fields rejected where practical.
3. Request and correlation identifiers.
4. Authenticated actor context.
5. Idempotency keys for retryable side-effecting operations.
6. Structured error codes rather than parsing free-form messages.
7. Cursor pagination for collection APIs where needed.
8. Backend-owned business rules.
9. Provider-specific state never exposed as the canonical product contract.
10. Existing V2 API behavior remains until a defined compatibility/cutover decision exists.

## 6. Error model

Canonical V3 errors initially include:

- validation_error
- unauthenticated
- forbidden
- not_found
- conflict
- idempotency_conflict
- entitlement_required
- insufficient_credits
- safety_blocked
- rate_limited
- provider_unavailable
- internal_error

Every error must indicate whether retry is appropriate. Internal provider exceptions should be translated at the service boundary.

## 7. Identity baseline inherited from V2

The V2 core currently uses Argon2 password hashing, opaque refresh tokens stored via HMAC, and JWT access tokens containing subject, email, tier, and roles. V3 initially preserves this behavior behind a future identity boundary rather than rewriting authentication during the core-contract milestone.

V3 evolution must distinguish authentication identity from account ownership and authorization scopes. API-key and service actors must be first-class identities rather than impersonating end users.

## 8. Provider orchestration rule

Face, Audio, Fusion, payments, storage, and future AI providers must be hidden behind canonical capability interfaces.

Provider execution is operational state; generation is product state.

A change from one model/provider to another must not force client contract changes unless the actual product capability changes.

## 9. Service-boundary direction

Existing V2-derived services remain operationally valid during migration:

- svc-core
- svc-pricing
- svc-commerce
- svc-dashboard
- svc-face
- svc-audio
- svc-fusion
- shared libraries

V3 does not assume these are the permanent final service boundaries. Boundaries are changed only when EIP evidence demonstrates ownership/coupling problems and the target migration can be made safely.

## 10. Database evolution rule

No V3 domain table is created merely because a contract exists.

Before each persistence change:

1. Retrieve V2 schema/table/usage evidence through EIP.
2. Identify canonical ownership and current dependencies.
3. Decide reuse, additive evolution, migration, or replacement.
4. Write a V3-only migration.
5. Define backward compatibility and data migration.
6. Add tests and rollback evidence.

V3 must operate against an isolated V3 database before V3-specific schema migrations are executed.

## 11. Compatibility strategy

Migration follows a strangler pattern:

```text
V2-derived implementation
        |
compatibility adapter
        |
V3 canonical contract
        |
new V3 consumers
```

Once all relevant consumers use the V3 contract and equivalence is certified, the legacy contract can be retired explicitly.

## 12. Immediate next architecture increments

### Increment V3-C2 — persistence inventory

Use EIP to map current tables and ownership for users/accounts, payments/subscriptions, credits/ledger, media, Face jobs, Audio jobs, Fusion jobs, generated outputs, masterdata, and provider metadata.

Output: current-to-target entity mapping. No speculative tables.

### Increment V3-C3 — API inventory and compatibility map

Map current frontend/mobile endpoints to V2 backend handlers and define which become V3 canonical endpoints, adapters, or retired endpoints.

### Increment V3-C4 — media lifecycle implementation

Implement MediaAsset persistence/adapters first because Face, Audio, Fusion, Story, Conversation, sharing, library, and Director will all depend on media lineage.

### Increment V3-C5 — common generation/job lifecycle

Normalize Face/Audio/Fusion asynchronous behavior into the canonical GenerationRequest / GenerationJob / ProviderExecution contract while preserving existing service execution.

### Increment V3-C6 — commerce lifecycle

Map and harden entitlement renewal, provider events, pricing quotes, and credit ledger behavior before changing user-facing pricing flows.

## 13. Freeze rules

The following are frozen for the current milestone:

- V3 evolves additively from the working V2 baseline.
- V2 source branches are not modified by V3 work.
- EIP evidence precedes migration decisions.
- Cross-service contracts live under a versioned V3 namespace.
- Media identity is provider/storage independent.
- Generation intent, job execution, and provider execution are distinct concepts.
- Pricing, entitlement, and credits remain server authoritative.
- Provider state is normalized behind product contracts.
- Feature rewrites do not begin before the relevant core contract and persistence impact are understood.
