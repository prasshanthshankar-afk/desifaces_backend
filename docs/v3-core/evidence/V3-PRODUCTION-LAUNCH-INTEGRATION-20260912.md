# V3 EIP Evidence Record

Change-ID: `V3-PRODUCTION-LAUNCH-INTEGRATION-20260912`
Status: `READY`
Owner: `#v3-core / production-launch`
Date: `2026-09-12`

## 1. Requirement

Build one clean production-launch backend candidate from canonical-descended, previously certified V3 runtime material and layer only the Sep 12 launch deltas required by the certified Web/mobile clients: enriched authenticated recent-Story discovery and durable Audio read-url refresh from `media_assets.storage_ref`. Keep production untouched until the candidate passes source, test, package and runtime-independent certification gates.

## 2. EIP source

- Canonical V3 baseline: `desifaces-v3` at `b4305fd6e2e913d21fddfaaa3dde60737540cbf7`.
- Canonical-descended production baseline: `audit/full-stack-sync-20260908` plus bounded Sep 9 production-closeout corrections.
- Certified DEV recent-Story source: Director hotfix lineage ending at `16d89f0914df20660e9cf9e1a298251f222de425`.
- Certified DEV durable Audio source: `fix/v3-audio-read-url-refresh-20260912`.
- Release candidate: `release/v3-production-launch-20260912`.
- Retrieval objective(s):
  - preserve existing Assistant/Director/Multi-Person runtime required by Web/mobile;
  - avoid direct merge of divergent PR #17;
  - preserve the production branch's existing canonical Audio route while fixing historical durable-media reads;
  - retain account/user ownership enforcement and no-secret/no-PII expansion.

## 3. V2 current-state evidence

### Code and service ownership

- `svc-director` owns Story discovery/workflow projection for Piku/Web/mobile.
- `svc-audio` owns Audio synthesis, canonical Audio media identity and fresh read-url signing.
- Web/mobile consume backend contracts; they do not own Story state or durable media signing.

### API/contracts

- Existing: `GET /api/director/stories/recent`.
- Existing: `GET /api/audio/assets/{media_id}/read-url`.
- Recent-Story output now includes optional workflow/state/attention fields while preserving `story_id`, `thread_id`, `state`, `title`, `updated_at`, and `continue_path` compatibility.
- Audio read-url path preserves the same endpoint/response contract and changes only the signing source to durable `storage_ref` with compatibility fallback.

### Persistence

- Story discovery reads existing `v3_stories`, `v3_projects`, `v3_director_runs`, `v3_studio_workflows`, and `v3_studio_stage_runs`.
- Audio read-url reads existing `media_assets.storage_ref`; no new table or schema is introduced.
- Audio COGS data correction is separately covered by `V3-AUDIO-COGS-PRODUCTION-READINESS-20260912`.

### Runtime/configuration

- Recent Story access is scoped by `account_id` and project `owner_user_id`.
- Audio signing remains owner-service behavior using the existing Azure storage connection and output container configuration.
- Historical durable Audio storage representations supported: bare blob path, container-prefixed path, `az://`/`azure://` reference, or Azure Blob URL.

### Tests/operations

- `services/svc-director/tests/test_recent_story_contract.py`.
- `services/svc-audio/tests/test_v3_audio_durable_read_url_contract.py`.
- Web and mobile launch branches already certify the same API paths and durable-media behavior.

## 4. Evidence gaps

- No production runtime smoke has been run from this candidate because production touch remains forbidden until source/package gates and physical mobile device acceptance are complete.
- Stripe live and store submissions are intentionally outside this integration step.

## 5. V3 disposition

Disposition: `ADAPT`

Rationale:

Reuse the existing canonical-descended Assistant/Director/Audio runtime and endpoint contracts. Adapt only Story discovery richness and the durable Audio signing source. Do not add a parallel Director, duplicate Audio endpoint, new pricing model, or new persistence model.

## 6. #v3-core architecture decision

The production candidate is composed from canonical-descended, previously production-packaged V3 runtime rather than directly merging the divergent operational PR #17. `svc-director` remains authoritative for account-scoped Story/workflow discovery. `svc-audio` remains authoritative for fresh Audio read URLs; `media_assets.storage_ref` is the durable identity and SAS URLs are ephemeral transport artifacts only. Web/mobile must request fresh read URLs rather than persist or trust generation-time SAS values.

## 7. Contract impact

- Canonical contract changes: additive optional recent-Story workflow/attention fields only.
- Versioning impact: none; existing response fields remain.
- Compatibility adapter required: no; `continue_path` retained.
- Client impact: existing Web/mobile behavior preserved; richer fields may be consumed incrementally.

## 8. Database impact

- Schema change: none for Story discovery or durable Audio refresh.
- Migration file: N/A for these two deltas.
- Data backfill/reconciliation: none.
- Rollback/compensating action: revert release commits; no data mutation is performed by either read path.
- Confirm V3-only DB execution: candidate certification is source/build/test only until explicit production cutover.

## 9. Security and privacy impact

- Authentication: existing Director/Audio authenticated dependencies retained.
- Authorization/account ownership: Story queries require account + project owner; Audio media requires user + account ownership.
- Secrets: Azure credentials remain server-side; generated SAS is short-lived read-only output.
- PII/media/privacy: no broader data projection; Story discovery returns bounded workflow metadata, not raw briefs/scripts or unrestricted customer data.
- Audit requirements: source hashes, tests and release workflow results retained in GitHub.

## 10. Pricing/entitlement/credit impact

- Pricing: unchanged by these deltas.
- Entitlement: unchanged.
- Credits/ledger/idempotency: unchanged.
- Provider billing events: unchanged.

## 11. Provider/model impact

- Provider-specific behavior inspected: Azure Blob read-only SAS generation.
- Canonical normalization: durable storage reference is resolved into container/blob coordinates only at read time.
- Routing/failover impact: none.

## 12. Implementation scope

- Files/services expected to change:
  - `services/svc-director/app/app/main.py`
  - `services/svc-director/tests/test_recent_story_contract.py`
  - `services/svc-audio/app/app/api/routes/v3_audio_output.py`
  - `services/svc-audio/app/app/services/azure_storage_service.py`
  - `services/svc-audio/tests/test_v3_audio_durable_read_url_contract.py`
  - production-launch certification workflow/evidence.
- Explicitly out of scope:
  - production deployment;
  - schema additions for these read paths;
  - customer pricing changes;
  - Stripe live activation;
  - mobile store submission;
  - direct merge of PR #17 or PR #19 wholesale.

## 13. Compatibility / migration strategy

Preserve current endpoint paths and old fields. For Audio, prefer `storage_ref` and fall back only to durable coordinates in metadata for transitional rows; never fall back to a persisted SAS URL. For Director, preserve `continue_path` and base Story fields while deriving workflow/attention data from existing V3 tables. Production receives the candidate only after branch-level Docker/test certification and final manual device acceptance.

## 14. Test and certification plan

- Unit/source-contract tests: Director recent-Story ownership/route ordering; Audio durable-storage signing and anti-regression assertion.
- Contract tests: existing V3 Story/Assistant/static contracts plus Audio endpoint route presence.
- Integration tests: build `svc-director`, `svc-audio`, and required production compose candidate without starting production.
- Migration tests: N/A for these read-path deltas; Audio COGS migration separately certified.
- Runtime/end-to-end certification: authenticated Story and Audio smoke only after production deploy approval.
- V2 regression protection: no V2 runtime touch; no endpoint removal; no pricing/schema changes.

## 15. Final certification evidence

- Commit/PR: `release/v3-production-launch-20260912` candidate; final head recorded after CI.
- Test result: pending branch certification at record creation.
- Runtime evidence: DEV route/Audio behavior already certified; production smoke pending cutover.
- Migration/schema evidence: no schema change for these deltas.
- #v3-core document updated: this bounded evidence record.

## 16. Freeze statement

`Freeze the production-launch contract to account-scoped Story discovery plus owner-service fresh media signing from durable storage identity. Any relaxation of ownership, persistence of SAS URLs, new pricing semantics, schema ownership change, or direct LLM access to unrestricted customer data requires returning to #v3-core.`
