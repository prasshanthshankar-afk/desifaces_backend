# V3 EIP Evidence Record — Fusion Canonical Compatibility Adapter

Change-ID: `V3-C3-FUSION-ADAPTER`
Status: `READY`
Owner: `#v3-core`
Date: `2026-08-17`

## 1. Requirement

Implement the V3-C3 Fusion compatibility adapter for the critical Face → Audio → Fusion creator path while preserving the certified V2 Fusion HTTP contract and keeping provider/background execution disabled.

The bounded change establishes:

- a typed, provider-neutral Fusion capability contract;
- collapse of the current mobile compatibility aliases for media, duration, profile/mode, camera, prompts, provider hints and pricing confirmation;
- translation to canonical `RequestContext` + `GenerationRequest(kind=fusion)`;
- strict separation of provider hints from generic generation parameters;
- strict separation of legacy `public.artifacts` UUIDs from canonical `MediaAsset` identity;
- canonical video `MediaAsset` construction and compatibility response mapping;
- explicit preservation of internal longform child-render billing markers as orchestration compatibility metadata;
- a hidden, authenticated, read-only V3-only Fusion mapping probe;
- no provider call, no pricing mutation, no recovery execution and no public OpenAPI change.

## 2. EIP source

- EIP repository: `prasshanthshankar-afk/desifaces-eos`
- EIP ref: `feature/eos-foundation`
- Backend repository: `prasshanthshankar-afk/desifaces_backend`
- V2 anchor: `70a80cef08cebb8f02385a8e0f1adbac7c85fbb8`
- V3 branch: `feature/v3-c3-canonical-adapters-20260817`
- Frontend repository: `prasshanthshankar-afk/desifaces_frontend`
- Frontend V2 ref: `environment/development-testflight-20260811`
- Retrieval objectives:
  - establish current Fusion create/pricing/status contracts;
  - establish the validated backend request model and provider restrictions;
  - identify mobile compatibility aliases that must stop at the adapter boundary;
  - establish artifact/media identity semantics;
  - establish internal child-render pricing suppression behavior;
  - preserve V3-C2C recovery/worker fences.
- Primary evidence paths:
  - `services/svc-fusion/app/app/domain/models.py`
  - `services/svc-fusion/app/app/api/routes/fusion_jobs.py`
  - `services/svc-fusion/app/app/repos/artifacts_repo.py`
  - `services/svc-fusion/app/app/api/deps.py`
  - `services/svc-fusion/app/app/main.py`
  - `src/features/fusion/api/creatorFusion.ts`
  - `docs/v3-core/evidence/V3-C3_CANONICAL_ADAPTER_MATRIX.md`

## 3. V2 current-state evidence

### Code and service ownership

- `svc-fusion` owns direct Fusion pricing preview, job creation, provider orchestration, job/status views, artifacts, and recovery APIs.
- `FusionOrchestrator` remains the active execution path and is not replaced in this C3 checkpoint.
- `svc-fusion-extension` may create internal child Fusion render jobs for longform orchestration.

### API/contracts

Current direct routes include:

- `POST /jobs/pricing/preview`
- `POST /jobs`
- `GET /jobs/{job_id}`
- `GET /jobs/{job_id}/status-light`
- `GET /jobs/{job_id}/status`
- internal recovery sweep route.

The backend validates direct create requests using `FusionJobCreate`. Current validated fields include:

- `face_image_url`
- `face_artifact_id`
- legacy/provider-specific `heygen_talking_photo_id`, `image_key`
- `voice_mode`
- `voice_audio {audio_url, audio_asset_id, audio_artifact_id}`
- `voice_tts {voice_id, script, language}`
- `video {aspect_ratio, dimension, duration_sec, emotion, motion_style, resolution, delivery_surface, shot_type, prompt}`
- `consent.external_provider_ok`
- `provider`
- `provider_options`
- reference image URLs/artifact IDs
- tags.

The frontend compatibility type is broader and additionally carries aliases including:

- `image_url`, `audio_url`, top-level `audio_artifact_id`;
- `requested_duration_sec`, `pricing_duration_sec`, `video_duration_sec`, `duration_ms`, `minutes`, `requested_units`;
- `video_mode`, `generation_mode`, `product_code`;
- `profile`, `profile_code`, nested `video.profile`;
- nested/top-level camera angle, framing and motion style;
- `prompt`, `user_prompt`, `video_prompt`, `creative_direction`, performance/motion/gesture/body/emotion/expression prompt variants;
- `provider_hint`, `quality_tier`, `scenario_name`;
- inline script/audio locale/voice/gender compatibility fields;
- `pricing_confirmation`.

The adapter therefore must normalize more than the current backend Pydantic model alone.

### Persistence

`public.artifacts` is a separate current persistence model:

- `id uuid`
- `job_id uuid`
- `kind`
- `url`
- optional content type/hash/bytes/metadata.

Fusion `face_artifact_id`, `audio_artifact_id` and reference artifact IDs refer to this artifact namespace/current orchestration semantics. They MUST NOT be automatically copied into canonical `GenerationRequest.source_media_ids` merely because they are UUIDs.

Canonical media identity remains `MediaAsset` and final physical migration/lineage is V3-C4 scope.

### Runtime/configuration

Existing V3-C2C controls remain binding:

- `FUSION_RECOVERY_ENABLED=false` in V3 Compose;
- Fusion execution worker remains behind the `v3-execution` profile;
- provider/background execution must remain disabled during C3 adapter certification;
- V3 Fusion API remains loopback-bound on port `18002` and isolated on `df-v3-net`.

C3 adds V3-only `DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED=true` for `svc-fusion`.

### Tests/operations

Added focused test: `test/test_v3_fusion_adapter.py`.

CI workflow `.github/workflows/v3-contract-tests.yml` now compiles/tests shared, Face, Audio and Fusion canonical adapter code.

## 4. Evidence gaps

- Fusion adapter unit tests have not yet been executed on the Azure V3 workspace for this checkpoint.
- Rebuilt V3 `svc-fusion` image/probe/OpenAPI parity have not yet been runtime-certified.
- Authenticated real-account probe remains pending, consistent with the current Face/Audio full-certification gap.
- V3-C4 has not yet defined final migration from current `public.artifacts`/URLs to durable canonical `MediaAsset` lineage.
- Longform Story/Scene/Director orchestration remains future scope; this adapter only preserves current internal-child metadata safely.

## 5. V3 disposition

Disposition: `ADAPT`

Rationale:

The direct Fusion API is an active compatibility contract and the backend model already enforces valuable provider/input validation. However the frontend and orchestration layers carry many aliases and provider/internal-billing details that are inappropriate as generic V3 domain fields. V3 therefore preserves the HTTP boundary while collapsing aliases into one capability-specific Fusion model and one canonical `GenerationRequest`.

## 6. #v3-core architecture decision

V3 Fusion uses:

`current Fusion payload → Fusion compatibility adapter → RequestContext + FusionGenerationParameters + GenerationRequest → canonical job/media/provider lifecycle → current Fusion compatibility response`

Frozen rules:

1. Provider choice/hints/options do not enter generic `GenerationRequest.parameters`; provider execution belongs to `ProviderExecution`.
2. Current `public.artifacts` UUIDs are compatibility references, not canonical `MediaAsset` IDs.
3. Only source media IDs explicitly resolved by the service layer after ownership/lineage validation may populate `GenerationRequest.source_media_ids`.
4. Current duration/profile/mode/camera/prompt aliases collapse before the canonical boundary.
5. Internal child-render pricing suppression remains orchestration compatibility metadata and must never become a public customer billing mode.
6. Non-UUID pricing quote IDs are preserved as compatibility metadata rather than fabricated as canonical UUIDs.
7. Inline Fusion TTS remains capability-specific compatibility behavior during C3; future Director orchestration may materialize Audio separately.

## 7. Contract impact

- Additive `FusionVoiceParameters`.
- Additive `FusionVideoParameters`.
- Additive `FusionGenerationParameters`.
- Additive `FusionPricingConfirmationCompat`.
- Additive `FusionGenerateAdapterResult`.
- Additive request/status/media mapping helpers under `services/shared/df_contracts/v3/fusion_adapter.py`.
- Public route versioning impact: none.
- Client impact: none.

## 8. Database impact

- Schema change: `none`.
- Migration file: `N/A`.
- Existing artifact/media rows are read as current-state evidence only.
- No C3 Fusion persistence mutation is introduced by the hidden probe.
- Rollback: disable V3 shadow flag/remove additive probe and contracts; no data compensation required.

## 9. Security and privacy impact

- Authentication: hidden probe reuses current `svc-fusion` JWT/service-token actor resolution.
- Authorization/account ownership: canonical account resolver is reused; explicit V3 source media IDs are accepted only after `media_assets.user_id` ownership lookup.
- Legacy artifact IDs remain compatibility metadata and are not promoted to canonical ownership identity without C4 resolution.
- Secrets: none added/exposed.
- PII/media: prompt/source references are not emitted to provider by the probe; no provider call occurs.

## 10. Pricing/entitlement/credit impact

- Existing Fusion preview/reserve/commit/release execution remains unchanged.
- C3 probe performs no pricing mutation.
- Current internal child-render suppression markers (`pricing_suppressed`, `child_job`, `bill_to_parent`, etc.) are retained under compatibility/orchestration metadata only.
- Parent/child no-double-charge semantics remain binding and will be certified with execution later.
- Quote UUIDs map to canonical `pricing_quote_id`; non-UUID IDs remain explicit compatibility metadata.

## 11. Provider/model impact

Current providers include provider-specific validation/routing semantics such as OmniHuman, HeyGen, VEED/Fabric and other video providers.

C3 decision:

- provider selection is compatibility/provider metadata;
- canonical business job state is provider-neutral;
- provider request ID, provider state, attempt and model belong to `ProviderExecution`;
- no routing/failover or provider worker is activated by this checkpoint.

## 12. Implementation scope

Changed/added scope:

- `services/shared/df_contracts/v3/fusion_adapter.py`
- `services/shared/df_contracts/v3/__init__.py`
- `test/test_v3_fusion_adapter.py`
- `services/svc-fusion/app/app/services/v3_fusion_adapter_shadow.py`
- `services/svc-fusion/app/app/api/v3_adapter_probe.py`
- `services/svc-fusion/app/app/main.py`
- `services/svc-fusion/app/Dockerfile`
- `docker-compose.v3.yml`
- `.github/workflows/v3-contract-tests.yml`
- this evidence record.

Explicitly out of scope:

- replacing `FusionOrchestrator`;
- enabling `svc-fusion-worker`;
- enabling recovery loop;
- calling providers;
- modifying public Fusion paths;
- changing pricing settlement;
- final C4 media migration;
- longform Story/Scene/Director redesign.

## 13. Compatibility / migration strategy

The existing direct Fusion API remains the external client contract. The hidden V3-only probe validates canonical translation independently.

Legacy URLs/artifact IDs remain accepted at the compatibility edge. They are retained in compatibility metadata until C4 can resolve/migrate them into canonical media lineage. New V3 orchestration may supply already-resolved canonical source media IDs to the adapter without changing current clients.

## 14. Test and certification plan

Unit/contract tests must prove:

- duration aliases collapse deterministically;
- profile/mode/camera/prompt aliases collapse deterministically;
- inline TTS remains capability-specific;
- legacy artifact IDs do not populate canonical media IDs;
- only service-resolved media IDs populate `source_media_ids`;
- provider/provider-options are absent from canonical generic parameters;
- internal child billing markers remain compatibility metadata;
- UUID/non-UUID quote behavior is explicit;
- idempotency is deterministic;
- canonical video media maps back to current Fusion response shape with provider state supplied separately through `ProviderExecution`.

Runtime certification must prove:

- V3 Fusion image builds with `df_contracts` packaged;
- `/api/health` remains healthy;
- hidden probe is mounted and protected (`401` without auth);
- V2/V3 Fusion OpenAPI `.paths` remain equal;
- `FUSION_RECOVERY_ENABLED=false` remains active;
- `DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED=true` is active;
- no V3 worker/scheduler starts.

Full adapter certification additionally requires an authenticated real-account read-only probe.

## 15. Final certification evidence

Pending runtime execution.

- Branch: `feature/v3-c3-canonical-adapters-20260817`.
- Unit result: pending.
- Runtime result: pending.
- OpenAPI parity: pending post-rebuild.
- Worker/recovery guard: pending post-rebuild.
- Schema: no change.

## 16. Freeze statement

`For V3-C3 Fusion migration, the current direct Fusion HTTP contract remains the compatibility boundary. Mobile/backend aliases, legacy artifact/URL references, provider hints and internal child-billing markers stop at that boundary. Canonical GenerationRequest contains only normalized capability parameters and service-resolved canonical media IDs; provider identity/state belongs to ProviderExecution. Changing these rules requires returning to #v3-core.`
