# V3 EIP Evidence Record — Audio Canonical Compatibility Adapter

Change-ID: `V3-C3-AUDIO-ADAPTER`
Status: `READY`
Owner: `#v3-core`
Date: `2026-08-17`

## 1. Requirement

Implement the V3-C3 Audio/TTS compatibility adapter for the critical creator path while preserving the certified V2 Audio HTTP contract.

The bounded change establishes:

- a typed, provider-neutral Audio generation parameter contract;
- translation from the current `POST /api/audio/tts` payload to canonical `RequestContext` + `GenerationRequest(kind=audio)`;
- normalization of current voice/language/delivery aliases without copying them into the generic V3 domain;
- canonical Audio `MediaAsset` construction and compatibility response mapping;
- reuse of the shared billing-account resolver established by the Face adapter;
- a hidden, authenticated, read-only V3-only Audio mapping probe;
- no translation, TTS synthesis, provider execution, pricing reservation, persistence mutation or public OpenAPI change.

## 2. EIP source

- EIP repository: `prasshanthshankar-afk/desifaces-eos`
- EIP ref/commit: `feature/eos-foundation` (repository EKB tree observed as `110e101b1ded730b0e3ed1d313ed05fe448d1f80`)
- Retrieval objective(s):
  - establish the current Audio create/status contract and aliases;
  - establish current auth behavior for user and service-token callers;
  - establish current pricing-confirmation and pricing orchestration boundaries;
  - establish current service packaging/runtime behavior;
  - preserve the globalization rule that language/voice capability remains service/catalog driven rather than introducing geography inference in the adapter.
- Retrieval query/command/reference:
  - current backend repository inspection on `feature/v3-c3-canonical-adapters-20260817`;
  - `services/svc-audio/app/app/api/routes/tts_jobs.py`;
  - `services/svc-audio/app/app/services/tts_orchestrator.py`;
  - `services/svc-audio/app/app/api/deps.py`;
  - `services/svc-audio/app/Dockerfile`;
  - canonical V3 domain/adapter contracts;
  - V3-C3 live V2/V3 OpenAPI `.paths` comparison, previously 10/10 `PASS`.

The EOS foundation remains a working-draft standards/EKB foundation; exact current Audio symbols, aliases and runtime packaging facts in this record are therefore grounded directly in current repository/runtime evidence rather than inferred from stale documentation.

## 3. V2 current-state evidence

### Code and service ownership

- Repository/ref: `prasshanthshankar-afk/desifaces_backend`; V2 anchor `70a80cef08cebb8f02385a8e0f1adbac7c85fbb8`; V3 implementation branch `feature/v3-c3-canonical-adapters-20260817`.
- Service/path/symbol: `services/svc-audio/app/app/api/routes/tts_jobs.py::create_tts_job` and `services/svc-audio/app/app/services/tts_orchestrator.py::TTSOrchestrator`.
- Current owner/responsibility: `svc-audio` owns TTS request validation, pricing preview/create orchestration, language/translation parameters, voice selection/catalog validation, provider execution through TTS services, audio storage, job status, pricing settlement and notifications.

### API/contracts

- Endpoint/event/contract: `POST /api/audio/tts` with current `TTSCreateRequest`.
- Current request fields include:
  - `text`
  - `target_locale`
  - optional `source_language`
  - `translate`
  - `voice` and `voice_id`
  - optional `voice_locale`
  - `speaker_gender`
  - `voice_gender`
  - `translation_tone`
  - `style` / `style_degree`
  - `rate` / `pitch` / `volume`
  - `context`
  - `output_format`
  - optional `pricing_confirmation {quote_id, preview_fingerprint}`.
- Current alias precedence: `_build_audio_payload()` uses `req.voice or req.voice_id`; current mobile clients commonly send `voice_id`.
- Current language default: execution payload uses `source_language or "en"` as `input_language`.
- Current voice-locale default: `voice_locale or target_locale`.
- Status endpoint: `GET /api/audio/jobs/{job_id}/status` returns `JobStatusResponse` with `job_id`, status/error fields and `variants[]`; each variant includes `audio_url`, optional `artifact_id`, `content_type`, and `bytes`.
- Consumers: current mobile Audio Studio and Fusion/longform integration clients; V3-C3 preserves the route contract.

### Persistence

- Audio execution continues to use existing studio/job/artifact/media/pricing persistence through current repositories/orchestrator.
- The C3 Audio adapter introduces no schema/table/write path.
- Canonical account ownership is resolved through the already-established shared resolver over `pricing_billing_account_members`, `pricing_credit_accounts.billing_account_id`, and `pricing_billing_accounts` fallback `account_code=user:<uuid>`.
- Final physical canonical media persistence remains V3-C4 scope; this adapter creates/mapping contracts only.

### Runtime/configuration

- Environment/config keys: additive V3-only `DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED=true` for `svc-audio` in `docker-compose.v3.yml`.
- Queue/worker/cache/storage/provider dependencies: the hidden probe invokes none of them; the existing `svc-audio-worker` remains behind the `v3-execution` profile.
- Runtime evidence identifier/path: V3-C2C certified parallel runtime; V3 APIs isolated on `df-v3-net`, workers disabled.
- Packaging evidence: prior Audio Dockerfile copied `desifaces_shared`, shared LLM helpers, and service code but not `df_contracts`; C3 adds `COPY services/shared/df_contracts /app/df_contracts`.
- Startup behavior: `svc-audio` main has no background execution loop; the probe is conditionally and lazily mounted only when the V3 flag is enabled.

### Tests/operations

- Shared tests: `test/test_v3_adapter_primitives.py`, `test/test_v3_account_context.py`.
- Face precedent already unit/runtime certified before Audio implementation: shared/Face/account tests `26 passed`, Face V3 rebuild/health/OpenAPI/hidden-auth probe/worker guard all passed.
- Added Audio tests: `test/test_v3_audio_adapter.py`.
- CI: `.github/workflows/v3-contract-tests.yml` compiles shared + Face + Audio adapter code and executes all focused V3 adapter tests.

## 4. Evidence gaps

- Audio focused tests have not yet been executed on the Azure V3 workspace after this implementation.
- The rebuilt V3 `svc-audio` image has not yet been runtime-certified with the new packaged `df_contracts`.
- The hidden Audio probe has not yet been invoked in the V3 runtime.
- Public Audio OpenAPI parity must be re-proven after the rebuild.
- A real authenticated successful mapping payload is deferred until a safe user token/context is intentionally selected; the first runtime mount test may use the expected unauthenticated `401` response.
- Final canonical Audio media persistence and cross-studio lineage are V3-C4/C5 concerns.
- Canonical Pricing quote identity remains pending the Pricing bridge; current non-UUID quote IDs are retained only as compatibility metadata.

## 5. V3 disposition

Disposition: `ADAPT`

Rationale:

The current Audio API is an active compatibility contract, but its transport model includes legacy/mobile voice aliases, defaulting semantics, current pricing identifiers and service-specific execution behavior. V3 therefore preserves the existing HTTP contract while translating it into one typed Audio capability model and the shared V3 request/generation/media vocabulary.

## 6. #v3-core architecture decision

V3 Audio uses the same adapter-first boundary already frozen for Face:

`current Audio HTTP payload → Audio compatibility adapter → RequestContext + AudioGenerationParameters + GenerationRequest → canonical job/media lifecycle → Audio compatibility response`

The adapter MUST:

- preserve current `voice or voice_id` precedence at the edge while emitting one `voice_id` internally;
- normalize missing source language to the current execution default (`en`);
- normalize missing voice locale to the selected target locale;
- retain speaker-gender and voice-gender as distinct capability fields because the current service gives them different semantics;
- never infer speaker/voice gender from geography;
- keep provider identity/state out of `GenerationRequest` and place it in `ProviderExecution` during execution cutover;
- preserve non-UUID current quote IDs as compatibility metadata rather than fabricating canonical UUIDs;
- use shared request-context/idempotency/status/error primitives;
- leave current TTS execution and public response contract unchanged until separately certified.

The first runtime integration remains a hidden read-only mapping probe, not an execution cutover.

## 7. Contract impact

- Canonical contract changes: additive `AudioGenerationParameters`, `AudioGender`, `AudioTranslationTone`, `AudioPricingConfirmationCompat`, `AudioGenerateAdapterResult`, Audio request/media/status mapping helpers.
- Versioning impact: additive V3 internal contract only.
- Compatibility adapter required: yes.
- Client impact: none; current mobile/web/service callers remain on the existing Audio endpoints.

## 8. Database impact

- Schema change: `none`.
- Migration file: `N/A`.
- Data backfill/reconciliation: `N/A` for this change; existing account mappings are reused read-only.
- Rollback/compensating action: disable the V3 shadow flag/remove additive probe/adapter code; no data compensation required.
- Confirm V3-only DB execution: shadow flag exists only in `docker-compose.v3.yml`; the V3 service already targets the certified `desifaces_v3` database.

## 9. Security and privacy impact

- Authentication: hidden probe uses existing `svc-audio` `get_current_user_id`.
- Authorization/account ownership: normal users derive identity from verified JWT claims; service-token calls require `X-Actor-User-Id` and validate that actor against `core.users`; canonical account is then resolved from billing-account persistence.
- Secrets: no secrets introduced or logged.
- PII/media/privacy: adapter does not log the TTS text/script; runtime log records only identifiers plus voice ID/target locale needed for adapter diagnostics.
- Audit requirements: execution cutover later must persist correlation/idempotency/generation/pricing/provider references; this probe remains read-only.

## 10. Pricing/entitlement/credit impact

- Pricing: existing Audio preview/reserve/commit/release behavior remains entirely in current orchestrator/pricing client.
- Entitlement: unchanged in this checkpoint.
- Credits/ledger/idempotency: canonical deterministic idempotency is created by the adapter; no credit mutation occurs in the shadow probe.
- Provider billing events: none from the probe.
- Quote compatibility: UUID current quote IDs map to `GenerationRequest.pricing_quote_id`; non-UUID IDs are preserved as compatibility metadata until the Pricing bridge supplies canonical quote identity.

## 11. Provider/model impact

- Provider-specific behavior inspected: current TTS orchestrator/service owns provider routing, translation/TTS errors, voice catalog checks and storage execution.
- Canonical normalization: provider name/model/request IDs/status do not enter `AudioGenerationParameters`; they remain future `ProviderExecution` data.
- Routing/failover impact: none in this checkpoint.

## 12. Implementation scope

- Files/services changed:
  - `services/shared/df_contracts/v3/audio_adapter.py`
  - `services/shared/df_contracts/v3/__init__.py`
  - `test/test_v3_audio_adapter.py`
  - `services/svc-audio/app/Dockerfile`
  - `services/svc-audio/app/app/services/v3_audio_adapter_shadow.py`
  - `services/svc-audio/app/app/api/v3_adapter_probe.py`
  - `services/svc-audio/app/app/main.py`
  - `docker-compose.v3.yml`
  - `.github/workflows/v3-contract-tests.yml`
  - this evidence record.
- Explicitly out of scope:
  - replacing `TTSOrchestrator`/`TTSService`;
  - activating `svc-audio-worker`;
  - calling Sarvam/ElevenLabs/Azure or any other provider from the shadow path;
  - modifying Audio catalog/masterdata;
  - changing pricing/credits;
  - schema migrations;
  - Fusion adapter implementation;
  - final media/job persistence cutover.

## 13. Compatibility / migration strategy

The existing `POST /api/audio/tts`, pricing preview, status and catalog APIs remain the compatibility surface.

Migration sequence:

1. normalize current request aliases through the pure shared adapter;
2. certify account/request/idempotency/generation mapping with a hidden V3-only probe;
3. prove public Audio OpenAPI remains unchanged;
4. preserve existing TTS execution while the adapter is shadow-only;
5. after Face + Audio + Fusion + Pricing bridges are certified, switch execution behind the existing compatibility API in a separately controlled milestone;
6. V2 production remains unaffected because the feature flag is V3-only and the V3 runtime/DB/network are isolated.

## 14. Test and certification plan

- Unit tests:
  - `test/test_v3_adapter_primitives.py`
  - `test/test_v3_audio_adapter.py`
  - `test/test_v3_account_context.py`
- Contract tests:
  - voice-vs-voice_id precedence;
  - source-language and voice-locale default normalization;
  - UUID/non-UUID quote behavior;
  - deterministic idempotency;
  - canonical Audio media creation;
  - canonical job → current Audio status/variant mapping.
- Integration tests:
  - rebuild only V3 `svc-audio`;
  - verify `/api/health`;
  - verify hidden probe is mounted and authenticated;
  - verify V2/V3 Audio OpenAPI `.paths` parity;
  - verify V3 shadow environment flag in the running container.
- Migration tests: schema N/A; account resolution already shared and tested.
- Runtime/end-to-end certification:
  - confirm no V3 worker/scheduler is running;
  - confirm no provider execution is triggered by probe;
  - confirm V2 Audio remains healthy/unmodified.
- V2 regression protection:
  - hidden route uses `include_in_schema=False`;
  - lazy V3 import when flag is enabled only;
  - no existing Audio route handler changed;
  - V3-only Compose flag.

## 15. Final certification evidence

Complete before marking `CERTIFIED`.

- Commit/PR: implementation branch `feature/v3-c3-canonical-adapters-20260817`; runtime checkpoint pending.
- Test result: pending focused Azure V3 test execution.
- Runtime evidence: pending V3 Audio rebuild/health/probe/OpenAPI/worker certification.
- Migration/schema evidence: schema change `N/A`.
- #v3-core document updated: `yes` — canonical matrix plus this Audio evidence record.

## 16. Freeze statement

`For V3-C3 Audio migration, the existing Audio HTTP contract remains the compatibility boundary while one canonical Audio capability representation plus shared V3 request/account/idempotency/generation/media semantics are introduced behind it. Voice aliases, transport defaults, provider identifiers/status, pricing implementation identifiers and client-specific fallback fields must not become generic V3 domain fields. Any change to this boundary strategy requires returning to #v3-core.`
