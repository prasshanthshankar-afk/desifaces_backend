# V3-C5 Canonical Generation Persistence

Change-ID: `V3-C5`
Status: `READY_FOR_RUNTIME_CERTIFICATION`
Owner: `#v3-core`
Date: `2026-08-18`
Backend V2 evidence anchor: `70a80cef08cebb8f02385a8e0f1adbac7c85fbb8`
C3 baseline: `61876055c5f43cdd5032e6eadd37da5ab24a9ec4`
Implementation branch: `feature/v3-c4-c6-foundation-closure-20260818`

## 1. Requirement

Create one provider-neutral canonical persistence layer for V3 generation requests, root/child jobs, provider attempts, job/media linkage, state transitions, retries, leases, errors, and idempotency before Multi-Person and future Conversation/Story/Director orchestration are implemented.

The layer must coexist with existing Face/Audio/Fusion/studio job tables during migration and must not activate any V3 worker simply because the schema exists.

## 2. EIP source

EIP repository: `prasshanthshankar-afk/desifaces-eos`
EIP ref: `feature/eos-foundation`
Primary standard: `ekb/06-integration/Integration_Architecture_Standard.md`

Relevant EIP rules:
- asynchronous variable-latency AI work uses durable jobs;
- lifecycle states are normalized across capabilities;
- provider request state is normalized behind backend records;
- retries require bounded attempts and idempotency;
- duplicate expensive provider calls must be prevented;
- request/correlation identity must survive asynchronous execution;
- integrations must expose enough state for observability and support.

## 3. V2 current-state evidence

V2 has working service-specific job persistence for Face, Audio, Fusion, Longform, Music, Commerce and common `studio_jobs`, but state/fields/provider details differ by service.

C3 demonstrated:
- Face, Audio and Fusion compatibility contracts can map to one canonical `GenerationRequest` and `GenerationJob` vocabulary;
- Fusion currently carries provider details in API/job views that should move to `ProviderExecution` rather than the business request;
- Longform creates parent/child execution relationships and suppresses pricing on billable-parent child renders;
- job aliases and provider-specific status vocabulary must not become canonical domain fields;
- idempotency is mandatory because generation calls are expensive and potentially irreversible.

## 4. Evidence gaps

Before certification:
- prove schema creation and constraints on V3 DB;
- prove request/root-job idempotent replay returns the same IDs;
- prove parent/child-ready job contract and bounded attempts;
- prove provider execution can be registered and transitioned without provider calls;
- prove input/output MediaAsset linkage;
- prove allowed state transitions and terminal-state resurrection rejection;
- prove runtime certification leaves no synthetic generation rows behind.

## 5. V3 disposition

Disposition: `ADDITIVE CANONICAL PERSISTENCE`.

Existing V2 service-specific job tables remain compatibility/runtime sources during migration. New V3 capabilities use canonical generation persistence. Existing services may dual-link/migrate progressively after evidence-backed adapter work.

## 6. #v3-core architecture decision

1. `GenerationRequest` is the immutable canonical generation intent.
2. `GenerationJob` is the durable execution lifecycle and may have child jobs.
3. `ProviderExecution` records provider/model attempts separately from business intent.
4. `GenerationJob` media relationships are explicit (`input`, `intermediate`, `preview`, `output`, `thumbnail`).
5. Canonical request idempotency is unique per account; same key + different digest is a conflict.
6. Provider attempts are bounded and uniquely identified by job/provider/capability/attempt.
7. Worker claiming uses database locking/lease semantics and must not exceed `max_attempts`.
8. Terminal jobs cannot be resurrected; a new provider attempt/child job is the retry mechanism where allowed.
9. Every state transition is auditable through append-only job events.
10. Creating the schema does not start workers. C2C execution profiles stay disabled until a separate execution certification explicitly enables them.

## 7. Contract impact

`GenerationJob` now carries parent/job type and attempt information. `ProviderExecution` carries provider-level idempotency identity. Existing C3 adapters remain compatible because new fields have safe defaults.

## 8. Database impact

Migration: `migrations/2026_08_18_v3_generation_persistence.sql`

New relations:
- `v3_generation_requests`
- `v3_generation_jobs`
- `v3_provider_executions`
- `v3_generation_job_media`
- `v3_generation_job_events`
- `v3_generation_job_summary` view

Adds FK from canonical media producing-job field to `v3_generation_jobs`.

No V2 job row is rewritten.

## 9. Security and privacy impact

Each request stores account/user identity and serialized canonical RequestContext. Media attachment verifies account equality. Provider metadata stays outside public compatibility responses unless explicitly mapped.

## 10. Pricing/entitlement/credit impact

Generation persistence stores optional canonical pricing quote identity only. Pricing reserve/commit/release remains owned by Pricing. Internal child billing suppression remains orchestration behavior, not a user billing mode.

## 11. Provider/model impact

Provider/model names and provider request IDs are stored only in `v3_provider_executions`. Canonical GenerationRequest remains provider-neutral.

## 12. Implementation scope

- `migrations/2026_08_18_v3_generation_persistence.sql`
- `services/shared/df_contracts/v3/domain.py`
- `services/shared/python/desifaces_shared/v3/generation_store.py`
- unit tests
- rolled-back runtime certification

## 13. Compatibility / migration strategy

Current Face/Audio/Fusion public APIs remain unchanged. C3 adapters become the bridge from compatibility payloads into canonical requests. New V3 enhancement work may persist directly to the canonical store while existing execution is progressively adapted.

No automatic replay/import of historic V2 jobs is required to start enhancements. Historical jobs remain accessible through current compatibility/library paths; future migration can back-reference them where useful.

## 14. Test and certification plan

Certification requires:
- all focused V3 unit tests pass;
- C5 migration applies only to V3 DB;
- request/idempotent replay test succeeds;
- synthetic provider execution registration succeeds without external call;
- input/output MediaAsset attachment succeeds;
- submitted -> queued -> running -> succeeded succeeds;
- terminal succeeded -> running is rejected;
- certification transaction rolls back to exact pre-test row counts;
- no V3 execution workers/schedulers start;
- V2 remains healthy.

Status becomes `CERTIFIED` only after runtime evidence passes.
