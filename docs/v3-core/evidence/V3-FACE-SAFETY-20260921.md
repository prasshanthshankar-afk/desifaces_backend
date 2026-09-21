# V3 EIP Evidence Record

Change-ID: `V3-FACE-SAFETY-20260921`
Status: `READY`
Owner: `#v3-core / svc-face`
Date: `2026-09-21`

## 1. Requirement

Close a P0 Face Studio safety gap demonstrated by a generated portrait containing a firearm from a prompt that explicitly requested a gun. desifaces product policy for this change is: generated Face Studio imagery must not intentionally include guns/firearms/weapons, blood, gore, or graphic injury.

The change must block unsafe prompt intent before pricing/generation, preserve safe creator language such as body "arms" and geography such as Arizona, and prevent generated images that fail content-safety review from being stored or returned.

## 2. EIP source

- EIP repository: `prasshanthshankar-afk/desifaces-eos`
- EIP ref/commit: `ffba612b54c6ee1dbccd6c8664761e1f7e892441`
- Retrieval objective(s):
  - Establish whether the EIP source already defines a specific hard-block contract for weapons, blood, or gore in Face Studio.
  - Confirm whether this incident can be handled as a bounded V3 safety adaptation without schema, pricing, or provider-routing changes.
- Retrieval query/command/reference:
  - Repository search for `safety weapon gore blood face studio content moderation`.
  - No direct matching hard-block contract was located; the concrete current-state behavior is therefore established from the V3 svc-face implementation and this incident.

## 3. V2 current-state evidence

Record concrete evidence only.

### Code and service ownership

- Repository/ref: `prasshanthshankar-afk/desifaces_backend@desifaces-v3`
- Service/path/symbol: `services/svc-face/app/app/services/safety_service.py` / `SafetyService`
- Current owner/responsibility: svc-face owns prompt keyword safety, Azure Content Safety text/image checks, and the safety negative prompt used by Face Studio generation.

### API/contracts

- Endpoint/event/contract: `POST /creator/pricing/preview`, `POST /creator/prompt/enhance`, and `POST /creator/generate` are the customer-facing Face Studio paths affected by prompt safety.
- Handler/service: `services/svc-face/app/app/api/routes/face_jobs.py` and `CreatorOrchestrator`.
- Consumers: desifaces web and native mobile Face Studio clients.

### Persistence

- Schema/table/migration: no schema or migration change.
- Readers: existing Face Studio job/status/dashboard readers are unchanged.
- Writers: existing Face Studio job, media asset, artifact, and face-profile writers are unchanged except unsafe generated output must be rejected before persistence.
- FK/index/constraint dependencies: none changed.

### Runtime/configuration

- Environment/config keys: existing Azure Content Safety configuration and existing OpenAI image configuration only; no new secrets.
- Queue/worker/cache/storage/provider dependencies: existing svc-face worker, OpenAI image provider, Azure Content Safety, and Azure storage.
- Runtime evidence identifier/path: generated-image bytes flow through `CreatorOrchestrator._process_variant` before storage.

### Tests/operations

- Existing tests: `services/svc-face/tests/test_glamour_safety_policy.py`, `services/svc-face/tests/test_openai_image_moderation.py`.
- Health/monitoring/runbook dependencies: existing svc-face health and worker operation; no new operational service dependency.

## 4. Evidence gaps

- Azure Content Safety does not expose a dedicated firearm category in the existing integration; deterministic prompt hard blocks are therefore required for intentional gun/weapon requests.
- A model may theoretically introduce an unsafe object that was not requested. This change adds fail-closed generated-image content-safety validation before persistence, but weapon-specific visual classification remains dependent on provider/content-safety behavior for unprompted weapon hallucinations.
- No direct EIP record defining this exact product prohibition was located; this is a new bounded V3 safety decision driven by the observed incident.

## 5. V3 disposition

Disposition: `ADAPT`

Rationale:

The existing svc-face safety service and generation orchestration are the correct owners. The change strengthens those existing controls rather than replacing the architecture: deterministic hard blocks are added for product-prohibited prompt terms, unsafe prompts are checked earlier, queued work is policy-versioned for revalidation, and generated image bytes are checked before persistence.

## 6. #v3-core architecture decision

Keep safety enforcement backend-authoritative in svc-face. Web/mobile may provide friendly UX, but clients are not trusted as the enforcement boundary.

For Face Studio:
1. Validate the original user prompt.
2. Translate non-English prompt text and validate the translated text.
3. Reject prohibited weapons/blood/gore intent before pricing reservation or generation.
4. Prevent prompt-enhancement from sanitizing and forwarding prohibited intent.
5. Generate only after the current safety-policy version has been recorded.
6. Validate generated image bytes before storage/return and fail closed if image safety is unavailable or blocks the output.

## 7. Contract impact

- Canonical contract changes: unsafe prompts continue to return the existing `DF_UNSAFE_PROMPT` error family; user-facing message is broadened beyond sexual content to reflect weapons/blood/gore policy.
- Versioning impact: internal `SAFETY_POLICY_VERSION` marker added for queued-job revalidation.
- Compatibility adapter required: no.
- Client impact: clients should surface the existing unsafe-prompt error and ask the user to edit the prompt.

## 8. Database impact

- Schema change: none.
- Migration file: N/A.
- Data backfill/reconciliation: N/A.
- Rollback/compensating action: revert this bounded code change if certification reveals a regression.
- Confirm V3-only DB execution: no DB execution is required.

## 9. Security and privacy impact

- Authentication: unchanged.
- Authorization/account ownership: unchanged.
- Secrets: no new secrets.
- PII/media/privacy: generated image bytes are inspected before persistence; no new persistence path.
- Audit requirements: safety-block reasons remain available in logs/job failure metadata.

## 10. Pricing/entitlement/credit impact

- Pricing: unsafe prompt intent is rejected before pricing reservation/generation where possible.
- Entitlement: unchanged.
- Credits/ledger/idempotency: unchanged; blocked prompt requests must not create chargeable successful generation output.
- Provider billing events: fewer provider calls for deterministically blocked prompts.

## 11. Provider/model impact

- Provider-specific behavior inspected: OpenAI image generation currently uses provider moderation plus svc-face safety controls; existing negative prompt already mentions weapons/blood/gore.
- Canonical normalization: deterministic desifaces policy is authoritative before provider moderation.
- Routing/failover impact: none.

## 12. Implementation scope

- Files/services expected to change:
  - `services/svc-face/app/app/services/safety_service.py`
  - `services/svc-face/app/app/services/creator_orchestrator.py`
  - `services/svc-face/app/app/api/routes/face_jobs.py`
  - `services/svc-face/tests/test_weapons_gore_hard_block_policy.py`
  - `.github/workflows/v3-contract-tests.yml`
- Explicitly out of scope:
  - DB schema and migrations
  - pricing rules and plan amounts
  - identity-lock behavior
  - geography masterdata
  - provider routing
  - unrelated studio UX

## 13. Compatibility / migration strategy

The existing Face Studio request/response shapes remain unchanged. Current clients continue using the same pricing, enhance, and generate endpoints. New safety behavior is stricter only for product-prohibited content. Existing queued jobs that were validated under the earlier policy are revalidated because the recorded safety-policy version must match the current version.

## 14. Test and certification plan

- Unit tests:
  - exact reported prompt containing `gun` is blocked
  - firearm/rifle/pistol/shotgun/weapon/ammunition aliases are blocked
  - blood/bloody/bleeding/gore/open-wound terms are blocked
  - safe `arms crossed` / Arizona prompts are not false positives
  - generated safe prompt reinforces no weapons/blood/gore
- Contract tests:
  - existing V3 Canonical Contract Tests
  - Face Studio weapons/blood/gore regression test is wired into the canonical PR gate
  - existing unsafe-prompt API contract remains `DF_UNSAFE_PROMPT`
- Integration tests:
  - pricing preview rejects prohibited prompt before quote/provider work
  - prompt enhancement rejects prohibited prompt
  - creator generate rejects prohibited prompt
  - generated image bytes pass image-safety gate before storage
- Migration tests: N/A.
- Runtime/end-to-end certification:
  - retry the exact reported gun prompt and verify no job/output is generated
  - verify a safe Arizona portrait with "arms crossed" still generates normally
- V2 regression protection:
  - existing glamour-safety and OpenAI image moderation tests remain unchanged and must pass

## 15. Final certification evidence

Complete before marking `CERTIFIED`.

- Commit/PR: `PR #47`
- Test result: pending required GitHub V3 gates
- Runtime evidence: pending post-merge runtime certification
- Migration/schema evidence: N/A — no schema change
- #v3-core document updated: N/A for this bounded safety adaptation; this evidence record is the required governance artifact

## 16. Freeze statement

Once certified, the Face Studio product prohibition on intentional guns/firearms/weapons and blood/gore/graphic injury is frozen as a backend-authoritative safety contract. Any future relaxation, exception taxonomy, or replacement of this enforcement boundary requires returning to #v3-core with new evidence and certification.
