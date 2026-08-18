# V3-C3 Fusion Canonical Adapter — Runtime Shell Certification

Change-ID: `V3-C3-FUSION-RUNTIME`
Status: `RUNTIME_SHELL_CERTIFIED`
Owner: `#v3-core`
Date: `2026-08-17`

## 1. Requirement

Certify the V3 Fusion canonical compatibility adapter at the API runtime shell without enabling provider execution, recovery processing, workers, schedulers, pricing mutation, or public API changes.

## 2. EIP source

- Governing design: `docs/v3-core/evidence/V3-C3_CANONICAL_ADAPTER_MATRIX.md`.
- Capability evidence: `docs/v3-core/evidence/V3-C3_FUSION_CANONICAL_ADAPTER.md`.
- Runtime baseline: V3-C2C isolated API runtime.
- Implementation branch: `feature/v3-c3-canonical-adapters-20260817`.

## 3. V2 current-state evidence

The current public Fusion contract remains `POST /jobs`, `POST /jobs/pricing/preview`, and job status APIs with provider/artifact compatibility fields. Current mobile callers retain extensive aliases for source media, duration, mode/profile, camera, prompt, provider hints and pricing confirmation. The V3 adapter normalizes these only at the compatibility boundary.

## 4. Evidence gaps

This certification proves the runtime shell and hidden protected probe mount. It does not yet prove a successful authenticated real-user probe against V3 billing-account/media state, and it does not enable or certify provider execution.

## 5. V3 disposition

Disposition: `ADAPT`.

## 6. #v3-core architecture decision

The hidden Fusion adapter probe may be mounted only in V3 with `DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED=true`. It remains excluded from OpenAPI and performs canonical translation only. Fusion recovery remains disabled and execution workers/schedulers remain disabled.

## 7. Contract impact

No public path/method change. Canonical Fusion parameters and source/provider compatibility metadata remain internal V3 contracts.

## 8. Database impact

No schema change and no runtime data mutation is required for this certification.

## 9. Security and privacy impact

The hidden probe is protected by existing Fusion authentication. Unauthenticated invocation returned HTTP `401`, proving the route is mounted but not anonymously accessible.

## 10. Pricing/entitlement/credit impact

No credit reservation, commit, release, or ledger mutation occurred in the certification commands.

## 11. Provider/model impact

No provider call occurred. `FUSION_RECOVERY_ENABLED=false` was verified in the rebuilt V3 container and no V3 worker/scheduler was running.

## 12. Implementation scope

Runtime certification covered only `svc-fusion` on the isolated V3 API runtime.

## 13. Compatibility / migration strategy

The V2 public Fusion OpenAPI `.paths` object was diffed against the rebuilt V3 Fusion OpenAPI `.paths` object and remained identical.

## 14. Test and certification plan

Executed on Azure V3 workspace:

- canonical adapter suite: `45 passed in 0.23s`;
- V3 Fusion image rebuilt successfully with `services/shared/df_contracts` packaged;
- `/api/health` returned healthy service response;
- hidden `/internal/v3/fusion-adapter/map` returned `401` without credentials;
- V2/V3 Fusion OpenAPI `.paths` diff returned no differences;
- `FUSION_RECOVERY_ENABLED=false` verified;
- `DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED=true` verified;
- no container matching `df-v3-.*(worker|scheduler)` was running.

## 15. Final certification evidence

Observed runtime outputs:

- test result: `45 passed in 0.23s`;
- `HEALTH={"status":"ok","service":"svc-fusion","version":"dev",...}`;
- `PROBE_HTTP=401`;
- `FUSION_OPENAPI_PARITY=PASS`;
- `FUSION_RECOVERY_ENABLED=false`;
- `DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED=true`;
- `FUSION_V3_RUNTIME_CERTIFICATION=PASS`.

This establishes `RUNTIME_SHELL_CERTIFIED`, not full authenticated mapping/provider execution certification.

## 16. Freeze statement

`The V3 Fusion canonical adapter runtime shell is certified on the isolated V3 API tier with public API parity preserved, recovery disabled, execution workers/schedulers disabled, and the hidden adapter probe authentication-protected. Provider execution and authenticated real-account/media mapping remain separate certification gates.`
