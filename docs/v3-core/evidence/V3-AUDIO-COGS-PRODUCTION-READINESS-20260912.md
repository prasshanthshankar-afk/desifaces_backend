# V3 EIP Evidence Record

Change-ID: `V3-AUDIO-COGS-PRODUCTION-READINESS-20260912`
Status: `READY`
Owner: `#v3-core / pricing-audio-release`
Date: `2026-09-12`

## 1. Requirement

Correct the launch economics baseline so `AUDIO_TTS_1K_CHARS` has a non-zero internal provider cost, keep customer pricing unchanged, fail closed when launch COGS is incomplete, and package the already-certified production-readiness controls for systemd timers and Stripe live inspection without enabling production or live billing.

## 2. EIP source

- EIP/current V3 source: canonical `desifaces_backend` branch `desifaces-v3` at `b4305fd6e2e913d21fddfaaa3dde60737540cbf7`.
- Change branch: `fix/v3-audio-cogs-production-readiness-20260912`.
- Retrieval objective(s):
  - establish the existing pricing/economics source of truth;
  - prove Audio TTS had no configured non-zero launch cost basis;
  - preserve customer-facing pricing and credit contracts;
  - capture the already-running DEV timer artifacts before any production install.
- Evidence methods:
  - migration and pricing-contract source inspection;
  - read-only Story economics reconciliation;
  - DEV Audio COGS correction certification;
  - source-controlled timer capture/provenance manifest;
  - Stripe live readiness inspection that prints only readiness state, never secrets.

## 3. V2 current-state evidence

### Code and service ownership

- Repository/ref: `prasshanthshankar-afk/desifaces_backend` / `b4305fd6e2e913d21fddfaaa3dde60737540cbf7`.
- Pricing/economics ownership remains in the backend pricing catalog/configuration and ledger; Audio does not own customer prices.
- Production operations ownership is represented by source-controlled install/certification scripts; application services are not restarted by this change.

### API/contracts

- No customer API contract changes.
- `AUDIO_TTS_1K_CHARS` customer credits remain unchanged.
- Stripe live remains disabled; readiness inspection does not activate payment rails.

### Persistence

- Additive data correction only: `migrations/2026_09_12_audio_tts_cost_basis.sql`.
- No new tables or schema shape changes.
- Customer price rows are explicitly treated as immutable by the certification gate.

### Runtime/configuration

- Audio launch COGS baseline: `$0.016 / 1K characters` internal cost.
- Existing DEV notification-dispatch and safe-Docker-cleanup unit/timer artifacts were captured into `ops/production/systemd/` with provenance and SHA256 manifest.
- Production installer is fail closed for hostname/environment/provenance and does not manually invoke cleanup.

### Tests/operations

- `scripts/certify-v3-production-economics-costs.sh`
- `scripts/audit-v3-story-economics-complete-readonly.sh`
- `scripts/apply-v3-audio-cogs-dev-20260912.sh`
- `scripts/apply-v3-audio-cogs-production.sh`
- `scripts/capture-v3-production-systemd-bundle-dev.sh`
- `scripts/install-v3-production-systemd-bundle.sh`
- `scripts/certify-v3-stripe-live-readiness.sh`
- DEV certification already established non-zero Audio COGS and no customer price mutation.

## 4. Evidence gaps

- Production migration has not been applied.
- Production systemd bundle has not been installed.
- Stripe live credentials/catalog/webhook have not been provisioned or enabled.
- These are intentional launch-cutover gates, not assumptions made by this evidence record.

## 5. V3 disposition

Disposition: `ADAPT`

Rationale:

Reuse the canonical V3 pricing, ledger, Audio and operations architecture. Adapt only the missing internal cost basis and production-readiness packaging. Do not introduce a parallel pricing model, a new schema, or a new billing rail.

## 6. #v3-core architecture decision

The canonical backend remains the source of truth for customer pricing, credits, ledger idempotency and internal launch economics. Provider cost metadata is an internal economics input and must never silently default to zero when launch margin is reported. Production operational jobs are installed from immutable source-controlled artifacts with explicit environment guards. Stripe live activation remains a separate bounded cutover after readiness certification.

## 7. Contract impact

- Canonical contract changes: none for customer-facing APIs.
- Versioning impact: none.
- Compatibility adapter required: no.
- Client impact: none.

## 8. Database impact

- Schema change: none; additive data correction only.
- Migration file: `migrations/2026_09_12_audio_tts_cost_basis.sql`.
- Data backfill/reconciliation: set/verify the internal Audio TTS cost basis used by launch economics.
- Rollback/compensating action: restore the prior cost configuration from the pre-migration production backup if validation fails.
- Confirm V3-only DB execution: production apply script is explicitly guarded; production has not been touched by this PR.

## 9. Security and privacy impact

- Authentication: unchanged.
- Authorization/account ownership: unchanged.
- Secrets: Stripe readiness checks validate presence/type without printing secret values.
- PII/media/privacy: no new access or data exposure.
- Audit requirements: preserve migration, timer provenance, hashes and launch certification output.

## 10. Pricing/entitlement/credit impact

- Pricing: customer-facing prices unchanged.
- Entitlement: unchanged.
- Credits/ledger/idempotency: unchanged; launch reconciliation remains fail closed and exactly reconciled.
- Provider billing events: internal cost reporting corrected for Audio TTS only.

## 11. Provider/model impact

- Provider-specific behavior inspected: Azure Neural TTS internal launch cost basis.
- Canonical normalization: represented as the existing Audio TTS economics SKU/configuration rather than a service-local formula.
- Routing/failover impact: none.

## 12. Implementation scope

- Files/services expected to change:
  - `migrations/2026_09_12_audio_tts_cost_basis.sql`
  - production-readiness/economics scripts under `scripts/`
  - captured timer artifacts under `ops/production/systemd/`
  - `docs/production-readiness-20260912.md`
- Explicitly out of scope:
  - customer price changes;
  - production deployment;
  - Stripe live activation;
  - application service restarts;
  - mobile billing rail changes.

## 13. Compatibility / migration strategy

The correction is backward compatible because customer pricing, credit quantities, API contracts and application routing are unchanged. DEV certification uses the same canonical pricing/economics model and validates the non-zero cost basis before production. Production migration and timer installation remain separate guarded steps with backup/provenance checks. Stripe live stays off until its own bounded cutover gate passes.

## 14. Test and certification plan

- Unit/source-contract tests: validate migration provenance, non-zero Audio COGS and customer pricing immutability.
- Contract tests: validate canonical launch SKUs have complete configured costs.
- Integration tests: read-only Story economics reconciliation across Face, Audio and Fusion.
- Migration tests: DEV apply + re-read of `AUDIO_TTS_1K_CHARS`; production apply remains pending.
- Runtime/end-to-end certification: certify timer bundle provenance and fail-closed installer; Stripe readiness inspection only.
- V2 regression protection: no new tables, no client contract changes, no customer price changes, no live billing enablement.

## 15. Final certification evidence

- Commit/PR: PR #18 / branch `fix/v3-audio-cogs-production-readiness-20260912`.
- Test result: DEV economics certification passed with non-zero Audio COGS and complete Face/Audio/Fusion launch cost coverage.
- Runtime evidence: production timer bundle captured with provenance and hashes; production installation not performed.
- Migration/schema evidence: DEV migration/correction passed; production migration pending launch cutover.
- #v3-core document updated: N/A; this record is the bounded #v3-core evidence artifact.

## 16. Freeze statement

`Freeze customer pricing, entitlement, credit and routing behavior for this correction. Any new SKU semantics, schema ownership, billing rail, provider routing or customer price change requires returning to #v3-core before implementation.`
