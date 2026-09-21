# V3 EIP Evidence Record

Change-ID: `V3-NEXT3-SHARED-SCENE-CONVERSATION-20260921`
Status: `READY`
Owner: `#v3-core / Multi-Person + Story`
Date: `2026-09-21`

## 1. Requirement

Add an additive DEV-only multi-person conversation workflow where two or more speaking participants are visible together in one shared source image. A user supplies a topic or script; Creative Director owns speaker-attributed dialogue; Audio remains participant/turn-specific; Fusion lip-syncs the mapped active speaker for each turn; non-speaking people remain in the same shared frame; the final result is a synchronized conversation video.

The feature must not replace or silently modify the existing multi-person workflow that creates or reuses one separate Face image per participant. Existing single-person Face, Audio, Fusion and the existing separate-Faces multi-person path remain compatibility requirements.

Production deployment is explicitly out of scope. Initial runtime target is `desifaces-dev` only.

## 2. EIP source

- EIP repository: `prasshanthshankar-afk/desifaces-eos`
- EIP ref/commit: `ffba612b54c6ee1dbccd6c8664761e1f7e892441`
- Retrieval objective(s):
  - Determine whether EOS already defines a canonical one-shared-image multi-speaker lip-sync workflow.
  - Preserve existing multi-person ownership boundaries rather than creating a new media execution stack.
- Retrieval query/command/reference:
  - Repository search for `multi-person conversation shared scene speaker dialogue`.
  - No direct shared-scene conversation contract was located at the pinned EOS ref. Current-state evidence is therefore established from the V3 Multi-Person/Studio workflow implementation and existing EIP evidence records.

## 3. V2 current-state evidence

### Code and service ownership

- Repository/ref: `prasshanthshankar-afk/desifaces_backend@desifaces-v3`.
- Service/path/symbol:
  - `services/svc-director/app/app/studio_workflow.py` owns Studio stage topology.
  - `services/svc-director/app/app/fusion_execution.py` owns approved Story/Fusion input lineage.
  - `services/svc-fusion` owns provider-neutral Fusion jobs.
  - `services/svc-fusion-extension` owns deterministic scene assembly.
- Current owner/responsibility: Creative Director/Studio orchestrates canonical participants, dialogue turns, review gates and scene execution. Audio and Fusion remain the generation owners.

### API/contracts

- Existing workflow contract: Story Studio creates Face stages per participant, Audio stages per speech turn and Fusion stages per scene.
- Existing user-facing multi-person behavior: individual Face identity per participant before downstream Audio/Fusion.
- Additive endpoint introduced by this change: `PUT /api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/shared-scene`.
- Consumers: desifaces web first; native mobile follows the same canonical Director contract after DEV certification.

### Persistence

- Schema/table/migration: existing `v3_studio_workflows`, `v3_studio_stage_runs`, dependencies, outputs and review records.
- Readers/writers: existing Director Studio persistence only.
- New schema: none.
- Shared-scene state is persisted in existing workflow/stage `metadata_json`: conversation mode, shared media id, image dimensions and speaker targets.

### Runtime/configuration

- Environment/config keys: `SYNC_API_KEY`, `SYNC_API_BASE_URL`, `DF_SYNC3_MODEL_ID`, `DF_SYNC3_HTTP_TIMEOUT_SECONDS`.
- Queue/worker/cache/storage/provider dependencies:
  - existing Director worker/control plane;
  - existing Audio generation;
  - existing Fusion API/worker;
  - Sync `sync-3` provider for deterministic active-speaker targeting;
  - existing Fusion Extension scene assembly;
  - existing Azure media storage.
- Runtime target: `desifaces-dev` only until functional certification.

### Tests/operations

- Existing regression evidence: V3 canonical contract suite, Multi-Person pricing tests, existing Studio HITL workflow contract.
- New focused tests:
  - `services/svc-director/tests/test_shared_scene_contract.py`
  - `services/svc-director/tests/test_shared_scene_fusion_gate.py`
  - `services/svc-fusion/tests/test_sync3_adapter.py`
  - `services/svc-fusion-extension/tests/test_shared_scene_hard_cut.py`

## 4. Evidence gaps

- Real-provider `sync-3` runtime behavior must be certified in `desifaces-dev` with a real shared image and at least two alternating speakers.
- DEV runtime must prove `SYNC_API_KEY` is present without exposing its value.
- Non-speaking participant preservation, identity stability and visual continuity require visual acceptance, not only unit tests.
- Native mobile UX has not yet been certified on the current mobile release baseline.
- Production rollout, load behavior, provider cost and production entitlement packaging are intentionally not established by this record.

## 5. V3 disposition

Disposition: `ADAPT`

Rationale:

Reuse the existing canonical Story/Studio persistence and Audio/Fusion execution ownership, while adding a separate workflow mode that changes only the visual input topology. Shared-scene mode uses one authoritative image and explicit participant coordinates instead of a Face-generation cohort. This avoids duplicating orchestration, pricing, review or media persistence architecture.

## 6. #v3-core architecture decision

Two multi-person experiences coexist explicitly:

```text
Existing:
Intent -> canonical Story -> separate Face per participant -> Audio -> Fusion -> Final

#next3 shared scene:
Intent/topic -> canonical Story -> one shared image + speaker mapping -> Audio -> active-speaker Fusion -> Final
```

Shared-scene mode is selected explicitly with `conversation_mode=shared_scene`. The legacy/default mode remains `ordered_speaker_shots`.

In shared-scene mode:
1. No participant Face-generation stages are created.
2. Audio starts from canonical speaker-attributed dialogue turns.
3. One owned/safety-checked image is persisted as the scene visual authority.
4. Every speaking participant must have an explicit user-confirmed normalized target point/box on that image.
5. Director cannot advance from Audio to Fusion until the shared image and complete speaker map are persisted.
6. Each Fusion child uses the same shared image, that turn's approved Audio, and only that turn's active-speaker coordinates.
7. Fusion routes shared-scene turns to `sync3`; existing Fusion remains on its existing provider path.
8. Scene assembly preserves deterministic dialogue order with hard cuts for shared-scene segments.

## 7. Contract impact

- Canonical contract changes:
  - additive Director workflow selector `conversation_mode`;
  - additive shared-scene stage metadata contract;
  - additive active-speaker provider options for shared-scene Fusion children.
- Versioning impact: shared-scene metadata contract version 1.
- Compatibility adapter required: no replacement adapter; existing mode remains the default.
- Client impact: clients must make the workflow choice explicit and must not present shared-scene as the existing separate-Faces flow.

## 8. Database impact

- Schema change: none.
- Migration file: N/A.
- Data backfill/reconciliation: N/A.
- Rollback/compensating action: stop using `conversation_mode=shared_scene` and redeploy the prior DEV runtime; existing separate-Faces records remain valid.
- Confirm V3-only DB execution: the change uses existing V3 Studio tables and no migration.

## 9. Security and privacy impact

- Authentication: unchanged Director/Face/Fusion JWT boundaries.
- Authorization/account ownership: shared image must resolve to an owned image MediaAsset in the same account.
- Secrets: Sync API key is runtime-only; no secret is stored in Git or returned to clients.
- PII/media/privacy: the shared source image is existing user media; speaker targeting stores normalized coordinates only.
- Audit requirements: conversation mode, shared media id, speaker mappings, dialogue turn id and participant id remain in canonical workflow/Fusion lineage.

## 10. Pricing/entitlement/credit impact

- Pricing: no new pricing table/schema change in this DEV implementation.
- Entitlement: existing Multi-Person premium entitlement remains the controlling product category during DEV certification.
- Credits/ledger/idempotency: existing parent scene pricing and internal child suppression/idempotency remain authoritative.
- Provider billing events: every shared-scene dialogue turn may create one Sync provider generation child; actual cost/package policy remains a later product/pricing decision.

## 11. Provider/model impact

- Provider-specific behavior inspected: Sync `sync-3` supports explicit active-speaker coordinates on a still image; adapter submits the same shared image plus per-turn Audio and disables automatic speaker detection.
- Canonical normalization: normalized user-confirmed coordinates are converted to native-pixel center coordinates at the Director/Fusion boundary.
- Routing/failover impact: shared-scene mode explicitly selects `sync3`; existing `ordered_speaker_shots` provider routing remains unchanged. No automatic provider fallback is added by this change.

## 12. Implementation scope

- Files/services expected to change:
  - Director shared-scene route, workflow builder, workflow selection, progression gate and Fusion input resolution.
  - Fusion Sync-3 provider adapter/registration and runtime configuration.
  - Fusion Extension shared-scene hard-cut assembly behavior.
  - focused backend contract tests and CI.
  - web Multi-Person entry UX and shared-image speaker mapping on a separate feature branch.
- Explicitly out of scope:
  - production deployment;
  - replacement of the existing separate-Faces multi-person workflow;
  - changes to single-person Face/Audio/Fusion;
  - DB migrations;
  - new pricing amounts or plan packaging;
  - overlapping/interruption dialogue;
  - automatic computer-vision speaker identification;
  - cinematic gaze/reaction/camera behavior beyond the first sequential-turn implementation.

## 13. Compatibility / migration strategy

`ordered_speaker_shots` remains the default request mode and retains the existing Face cohort requirement. `shared_scene` is additive and must be explicitly selected. No existing workflow is migrated automatically.

The web UI exposes the two experiences before Director starts:
- “One image with multiple people” for shared-scene conversation.
- “Separate Face for each person” for the existing flow.

This is a functional distinction, not cosmetic labeling. Shared-scene clients do not generate separate participant Faces, while existing clients that send no new mode continue through the original workflow.

## 14. Test and certification plan

- Unit tests:
  - shared-scene contract validation, unique speaker targets and image bounds;
  - native-pixel coordinate derivation;
  - all speaking participants require targets;
  - shared-scene child compilation succeeds with `face_media_id=None`;
  - only the shared image is resolved for visual input;
  - Sync-3 adapter request/poll contract;
  - hard-cut scene assembly.
- Contract tests:
  - V3 Canonical Contract Tests must pass on PR #52.
  - Existing Multi-Person pricing and foundation tests remain in the same gate.
- Integration tests:
  - create `shared_scene` Studio workflow and prove zero Face stages;
  - approve Audio, prove progression remains blocked until shared image mapping is saved;
  - persist complete speaker map, then allow Fusion;
  - verify every Fusion child carries the matching `participant_id`, dialogue turn and active-speaker coordinates.
- Migration tests: N/A.
- Runtime/end-to-end certification:
  - deploy feature SHAs to `desifaces-dev` only;
  - verify Sync configuration presence without secret disclosure;
  - two-person image, alternating dialogue, participant-specific voices;
  - visually verify correct mouth moves for each turn and non-speaker does not receive the speaking Audio;
  - verify final stitched conversation order/audio/video synchronization;
  - verify web distinguishes shared-image vs separate-Faces before workflow creation.
- V2 regression protection:
  - existing workflow remains default and continues to require approved individual Faces;
  - no production branch/runtime is changed;
  - existing canonical tests remain mandatory.

## 15. Final certification evidence

Complete before marking `CERTIFIED`.

- Commit/PR: backend draft PR #52; web draft PR #36.
- Test result: web PR #36 `web-build` run #312 — TypeScript typecheck PASS and Next.js production build PASS; backend canonical/EIP gates pending after this evidence record.
- Runtime evidence: pending DEV-only deployment and two-speaker end-to-end generation.
- Migration/schema evidence: N/A — no schema change.
- #v3-core document updated: this evidence record is the required #v3-core governance artifact for the additive workflow.

## 16. Freeze statement

After DEV functional certification, the #next3 first-slice contract is frozen as: one user-confirmed shared image, at least two speaking participants, explicit participant mapping, sequential speaker-attributed Audio turns, per-turn active-speaker Sync-3 lip-sync, deterministic final assembly, and no separate Face cohort. Any production rollout, provider replacement/fallback, automatic face detection, overlapping dialogue, pricing-package change, schema change, or change to existing separate-Faces behavior requires a new #v3-core review/evidence update.


## Frozen user-facing UX vocabulary

The implementation may retain internal contract names such as `shared_scene` and
`ordered_speaker_shots`, but those technical names must not be used as the primary
mode labels presented to creators.

User-facing mode selection is frozen as:

- **Add people one by one** — existing multi-person workflow where each participant is
  created or added independently before the conversation is assembled.
- **Create one group photo** — #next3 workflow where the people remain together in one
  photo and desifaces synchronizes the active speaker turn by turn.

Saved Work must keep conversation outputs discoverable with dedicated tabs beside
Videos:

- **Group Photo Conversation**
- **Multi-Person Conversation**

Persistence/classification contract:

- `conversation_mode=shared_scene` → `conversation_kind=group_photo_conversation`
- `conversation_mode=ordered_speaker_shots` → `conversation_kind=multi_person_conversation`

The generic **Videos** tab remains available and continues to show video output without
changing existing single-person behavior.
