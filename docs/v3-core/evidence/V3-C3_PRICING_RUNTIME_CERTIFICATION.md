# V3-C3 Pricing Runtime-Shell Certification

Change-ID: `V3-C3-PRICING-RUNTIME`
Status: `RUNTIME_SHELL_CERTIFIED`
Owner: `#v3-core`
Date: `2026-08-17`

## 1. Requirement

Certify the V3 Pricing canonical compatibility bridge at the isolated API runtime boundary without executing quotes, reservations, payments, ledger mutations, subscription reconciliation, or provider work.

## 2. EIP source

- Repository: `prasshanthshankar-afk/desifaces_backend`
- Branch: `feature/v3-c3-canonical-adapters-20260817`
- Architecture source: `docs/v3-core/evidence/V3-C3_CANONICAL_ADAPTER_MATRIX.md`
- Pricing adapter evidence: `docs/v3-core/evidence/V3-C3_PRICING_CANONICAL_ADAPTER.md`
- V3 runtime baseline: certified V3-C2C isolated API stack.

## 3. V2 current-state evidence

Pricing compatibility endpoints remain unchanged. The canonical shadow probe is additive, hidden from OpenAPI, authenticated, and read-only. The V3 runtime explicitly keeps `DF_SUBSCRIPTION_RECONCILER_ENABLED=false`.

## 4. Evidence gaps

This runtime-shell certification proves packaging, route mounting, authentication protection, OpenAPI compatibility, and execution-worker/reconciler guards. It does not yet prove a successful authenticated mapping for a real V3 user/account. That is the final cross-capability C3 certification gate.

## 5. V3 disposition

Disposition: `PRESERVE + NORMALIZE`.

## 6. #v3-core architecture decision

The current Pricing HTTP surface remains a compatibility boundary. Canonical V3 quote/reservation/credit contracts are translated behind it. The hidden probe exists only for V3 certification and is excluded from OpenAPI.

## 7. Contract impact

No public route change. Canonical Pricing bridge remains internal/additive.

## 8. Database impact

None. Runtime certification did not mutate pricing persistence.

## 9. Security and privacy impact

The hidden route returned HTTP `401` without credentials, proving authentication protection. No bearer or secret was logged in certification output.

## 10. Pricing/entitlement/credit impact

No quote execution, reservation, commit, release, subscription reconciliation, or ledger write occurred.

## 11. Provider/model impact

None.

## 12. Implementation scope

Runtime proof only for `svc-pricing` V3 API container.

## 13. Compatibility / migration strategy

V2 and V3 Pricing OpenAPI `.paths` remain identical; canonical mapping stays hidden behind the V3-only shadow feature flag.

## 14. Test and certification plan

Completed runtime-shell checks:

- `GET 127.0.0.1:18009/api/health`
- unauthenticated hidden probe call
- V2/V3 OpenAPI `.paths` diff
- container environment guard checks
- V3 worker/scheduler absence check

## 15. Final certification evidence

Observed on Azure V3 workspace:

- `HEALTH={"ok":true}`
- `PROBE_HTTP=401`
- `PRICING_OPENAPI_PARITY=PASS`
- `DF_SUBSCRIPTION_RECONCILER_ENABLED=false`
- `DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED=true`
- `PRICING_V3_RUNTIME_CERTIFICATION=PASS`

Result: `RUNTIME_SHELL_CERTIFIED`.

Remaining C3 gate: successful authenticated, read-only cross-capability mapping for one real V3 cloned user/account with before/after persistence invariants.

## 16. Freeze statement

`V3 Pricing runtime packaging, protected shadow mounting, public OpenAPI compatibility, reconciler disablement, and execution-worker guards are certified. Full C3 critical-path certification still requires authenticated read-only mapping against a real V3 account and proof of no persistence mutation.`
