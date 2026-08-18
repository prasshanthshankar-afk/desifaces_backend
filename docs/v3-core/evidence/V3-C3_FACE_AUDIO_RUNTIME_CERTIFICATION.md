# V3 EIP Evidence Record — Face and Audio Adapter Runtime Shell Certification

Change-ID: `V3-C3-FACE-AUDIO-RUNTIME`
Status: `CERTIFIED`
Owner: `#v3-core`
Date: `2026-08-17`

## 1. Requirement

Certify the non-executing V3 runtime integration shell for the Face and Audio canonical compatibility adapters without changing the current public API surface, activating provider workers, mutating pricing/credits, or submitting generation jobs.

This certification is intentionally narrower than full capability cutover. It proves packaging, service startup, hidden-route mounting, authentication protection, public OpenAPI parity, feature-flag activation, and worker isolation for both adapters.

## 2. EIP source

- EIP repository: `prasshanthshankar-afk/desifaces-eos`
- EIP ref: `feature/eos-foundation`
- Backend repository: `prasshanthshankar-afk/desifaces_backend`
- V2 anchor: `70a80cef08cebb8f02385a8e0f1adbac7c85fbb8`
- V3 branch: `feature/v3-c3-canonical-adapters-20260817`
- Parent architecture evidence:
  - `docs/v3-core/evidence/V3-C3_CANONICAL_ADAPTER_MATRIX.md`
  - `docs/v3-core/evidence/V3-C3_FACE_CANONICAL_ADAPTER.md`
  - `docs/v3-core/evidence/V3-C3_AUDIO_CANONICAL_ADAPTER.md`
- Runtime evidence supplied from Azure V3 workspace on 2026-08-17 / 2026-08-18 UTC crossover.

## 3. V2 current-state evidence

### Code and service ownership

- `svc-face` owns the current Face Creator compatibility API and `CreatorOrchestrator` execution path.
- `svc-audio` owns the current TTS compatibility API and `TTSOrchestrator` execution path.
- Neither execution path is replaced by this certification.

### API/contracts

Face:
- existing public Face OpenAPI `.paths` compared V2 `127.0.0.1:8003` to V3 `127.0.0.1:18003` after the adapter probe was mounted;
- result: `FACE_OPENAPI_PARITY=PASS`.

Audio:
- existing public Audio OpenAPI `.paths` compared V2 `127.0.0.1:8004` to V3 `127.0.0.1:18004` after the adapter probe was mounted;
- result: `AUDIO_OPENAPI_PARITY=PASS`.

Hidden certification routes:
- Face: `POST /internal/v3/face-adapter/map`, excluded from OpenAPI.
- Audio: `POST /internal/v3/audio-adapter/map`, excluded from OpenAPI.
- Unauthenticated calls returned `401` for both, proving the routes are mounted while retaining service authentication protection.

### Persistence

- No schema changes were introduced by this certification.
- Hidden probes are read-only by design.
- No generation-job, media, pricing-reservation, ledger, or provider-execution mutation is part of the shell checks.

### Runtime/configuration

Face runtime evidence:
- V3 image rebuilt successfully with `services/shared/df_contracts` packaged at `/app/df_contracts`.
- container `df-v3-svc-face` started successfully.
- `/api/health` returned `{"status":"ok"}`.
- `DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED=true` present in running container environment.
- hidden probe returned HTTP `401` without credentials.
- no `df-v3-*worker*` or `df-v3-*scheduler*` process was running.
- result: `FACE_V3_RUNTIME_CERTIFICATION=PASS`.

Audio runtime evidence:
- V3 image rebuilt successfully with `services/shared/df_contracts` packaged at `/app/df_contracts`.
- container `df-v3-svc-audio` started successfully.
- `/api/health` returned status `ok`, service `svc-audio`, version `dev`.
- `DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED=true` present in running container environment.
- hidden probe returned HTTP `401` without credentials.
- no `df-v3-*worker*` or `df-v3-*scheduler*` process was running.
- result: `AUDIO_V3_RUNTIME_CERTIFICATION=PASS`.

### Tests/operations

Face focused test execution in ephemeral Face image:
- `test/test_v3_adapter_primitives.py`
- `test/test_v3_face_adapter.py`
- `test/test_v3_account_context.py`
- result: `26 passed in 0.17s`.

Audio focused test execution in ephemeral Audio image:
- `test/test_v3_adapter_primitives.py`
- `test/test_v3_audio_adapter.py`
- `test/test_v3_account_context.py`
- result: `28 passed in 0.16s`.

## 4. Evidence gaps

This shell certification does **not** prove full authenticated end-to-end canonical mapping against a real V3 user/account row.

Still pending before the individual Face/Audio adapter evidence records can be marked fully `CERTIFIED`:

- authenticated Face probe using a real V3 user/account context;
- owned Face I2I source-media mapping against the V3 clone where applicable;
- authenticated Audio probe using a real V3 user/account context;
- explicit before/after mutation guard around those authenticated read-only probe calls;
- execution cutover remains out of scope and workers remain disabled.

## 5. V3 disposition

Disposition: `ADAPT`

The existing public Face and Audio APIs remain compatibility boundaries while hidden V3-only mapping probes validate canonical translation independently of execution.

## 6. #v3-core architecture decision

The Face and Audio canonical adapters may remain mounted in the V3 API tier behind `DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED=true` because:

- they are excluded from OpenAPI;
- they retain existing service authentication;
- they perform no provider execution;
- the public V2-compatible API surface remains unchanged;
- V3 execution workers remain disabled.

The shell certification does not authorize replacing current orchestration or enabling workers.

## 7. Contract impact

- Canonical contract changes: additive Face and Audio capability-specific adapter contracts.
- Versioning impact: none to public API routes.
- Compatibility adapter required: yes.
- Client impact: none.

## 8. Database impact

- Schema change: `none`.
- Migration file: `N/A`.
- Data backfill/reconciliation: `N/A` for this shell certification.
- Rollback: disable the V3 shadow flag and/or revert additive probe/packaging code.
- V3-only execution: confirmed by the `docker-compose.v3.yml` flag and V3 loopback ports.

## 9. Security and privacy impact

- Authentication: existing Face/Audio auth dependencies protect hidden probes.
- Authorization/account ownership: canonical account/media ownership resolution remains part of authenticated probe validation.
- Secrets: none added or exposed.
- PII/media/privacy: no media body or provider data is emitted by shell certification.
- Audit: shell checks are operational certification only.

## 10. Pricing/entitlement/credit impact

- Pricing: unchanged.
- Entitlement: unchanged.
- Credits/ledger/idempotency: no credit or ledger mutation during shell checks.
- Provider billing events: none.

## 11. Provider/model impact

- No providers were invoked.
- No routing/failover changes were enabled.
- Canonical provider separation remains deferred to execution cutover/Fusion work.

## 12. Implementation scope

Certified shell scope includes:
- shared V3 adapter package importability;
- Face/Audio service image packaging;
- hidden V3-only route mounting;
- service startup and health;
- OpenAPI compatibility;
- feature-flag activation;
- worker/scheduler isolation.

Explicitly out of scope:
- provider execution;
- job creation;
- pricing reservation/commit/release;
- public route replacement;
- full authenticated real-account probe certification;
- worker activation.

## 13. Compatibility / migration strategy

Face and Audio continue serving the existing client contracts. Canonical translation is exercised separately behind hidden V3-only probes until real-account mapping is certified and a later #v3-core decision authorizes execution cutover.

## 14. Test and certification plan

Completed:
- Face adapter unit suite: 26 passed.
- Audio adapter unit suite: 28 passed.
- Face V3 image rebuild/startup: PASS.
- Audio V3 image rebuild/startup: PASS.
- Face health: PASS.
- Audio health: PASS.
- Face public OpenAPI parity: PASS.
- Audio public OpenAPI parity: PASS.
- hidden Face probe authentication protection: PASS (`401`).
- hidden Audio probe authentication protection: PASS (`401`).
- V3 adapter shadow flags: PASS.
- V3 worker/scheduler guard: PASS.

Pending for full adapter certification:
- authenticated read-only canonical mapping against the V3 clone.

## 15. Final certification evidence

- Branch: `feature/v3-c3-canonical-adapters-20260817`.
- Face unit result: `26 passed in 0.17s`.
- Audio unit result: `28 passed in 0.16s`.
- Face runtime result: `FACE_V3_RUNTIME_CERTIFICATION=PASS`.
- Audio runtime result: `AUDIO_V3_RUNTIME_CERTIFICATION=PASS`.
- OpenAPI parity: Face PASS; Audio PASS.
- Worker/scheduler guard: PASS.
- Schema evidence: no schema change.
- #v3-core documentation: adapter matrix and capability evidence records present.

## 16. Freeze statement

`The V3 Face and Audio canonical-adapter runtime shells are CERTIFIED for isolated, non-executing use behind the V3 shadow flag. Public APIs remain unchanged and provider/background execution remains disabled. Full individual adapter certification still requires authenticated real-account mapping evidence before execution cutover.`
