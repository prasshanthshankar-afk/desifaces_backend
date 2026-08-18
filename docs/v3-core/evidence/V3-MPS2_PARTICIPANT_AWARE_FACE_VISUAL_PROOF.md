# V3-MPS2 — Participant-Aware Face Visual Proof

**Status:** READY_FOR_VISUAL_CERTIFICATION  
**Branch:** `feature/v3-multiperson-core-20260818`  
**Depends on:** Certified MPS1A + MPS1B + MPS1C

## Objective

Prove the visible desifaces creative chain using real V3 services and real image generation:

`User intent -> svc-director RAG/LLM plan -> human plan approval -> deterministic participant Face requests -> Face pricing -> human pricing approval -> svc-face/gpt-image-2 -> two Face MediaAssets -> canonical Participants -> Face HITL review`

This gate is deliberately visual. It is not sufficient to assert that Face stage rows exist; the generated Director plan, Face prompts and final image bytes must be retained for human inspection.

## Frozen product invariants

1. `svc-director` is the creative planner, not the image provider.
2. The LLM proposes typed participant `persona`, `continuity` and `visual_direction`.
3. A deterministic compiler converts approved participant direction into the existing Face Studio API contract.
4. Each canonical Participant gets an independent single-person Face generation. Canonical V3 does not persist `single_person|two_people|group` as its domain cardinality model.
5. Existing `svc-face` pricing, safety, prompt construction, provider adapter, storage and job lifecycle remain authoritative.
6. T2I provider model must resolve to `gpt-image-2` for this proof.
7. Exactly one variant per participant is requested during this controlled proof to bound provider spend.
8. Gender is never inferred from a participant name, relationship role, locale or geography. It is forwarded only when explicitly supplied by user/context.
9. Geography may inform setting only. It must not infer ethnicity, skin tone, religion, attire, occupation, socioeconomic status, facial anatomy or personality.
10. Obvious sensitive/account/security metadata is stripped before any participant metadata becomes a provider prompt.
11. Generated Face output is bound to `v3_participants.primary_face_media_id` and `v3_participant_media(relation='primary_face')`.
12. Generated Face output is attached to the corresponding Face Studio stage and receives a pending HITL review item.
13. Face outputs are not auto-approved. Audio must remain blocked until the human approves the selected Face output.
14. Generated proof images are retained for human inspection; SAS query strings are not stored in the proof manifest/status files.

## Proof scenario

Intent:

- Ananya — 35-year-old woman, daughter.
- Ravi — 65-year-old man, father.
- Warm contemporary story discussing restoration of an ancestral house as a community arts space.
- Chennai is setting context only.
- One scene is sufficient for this visual identity proof.
- Both participants are planned as distinct recurring cinematic characters.

## Interactive gates

### Gate 1 — Director plan review

The runner displays and stores the actual structured Director output including:

- title / summary
- participant persona
- participant continuity
- participant visual direction
- voice direction
- critique

The user must approve the plan before canonical Story compilation unless the explicit test-only environment approval is supplied.

### Gate 2 — Face pricing / provider execution review

Before any image generation, the runner displays for both participants:

- deterministic Face Studio prompt
- pricing quote / pricing metadata
- account balance summary supplied by Face/Pricing APIs

The user must approve provider execution before the two image jobs are created.

### Gate 3 — Face HITL review

After generation, each Face is deliberately left with a `pending` Studio review item. The proof ends here. Human visual approval/revision is the next action; Audio is not authorized by this proof.

## Expected retained artifacts

Under `artifacts/v3-mps2-visual-proof/<UTC timestamp>/`:

- `01_director_intent.json`
- `02_director_generative_plan.json`
- `03_director_critique.json`
- `04_story_workspace_before_faces.json`
- `05_ananya_face_request.json`
- `05_ravi_face_request.json`
- `06_ananya_pricing_preview.json`
- `06_ravi_pricing_preview.json`
- `07_ananya_face.png`
- `07_ravi_face.png`
- `08_ananya_face_status.json`
- `08_ravi_face_status.json`
- `09_story_workspace_after_faces.json`
- `10_studio_workflow_after_faces.json`
- `manifest.json`

`artifacts/v3-mps2-visual-proof/latest` points to the latest run.

## Expected runtime proof markers

- `MPS2_VISUAL_TARGETED_UNIT=PASS`
- `MPS2_VISUAL_DB_TARGET=PASS`
- `MPS2_VISUAL_SERVICE_HEALTH=PASS`
- `MPS2_VISUAL_EXECUTION_SCOPE=PASS:director_worker+face_worker_only`
- `MPS2_VISUAL_FACE_MODEL=PASS:gpt-image-2`
- `MPS2_INTENT_TO_DIRECTOR=PASS`
- `MPS2_DIRECTOR_GENERATIVE_OUTPUT_VISIBLE=PASS`
- `MPS2_DIRECTOR_PLAN_COMPILED=PASS`
- `MPS2_TWO_PARTICIPANT_FACE_STAGES=PASS`
- `MPS2_DIRECTOR_TO_FACE_REQUESTS_VISIBLE=PASS`
- two `MPS2_FACE_GENERATED=<participant>...` records
- `MPS2_INTENT_TO_GENERATIVE_PLAN=PASS`
- `MPS2_GENERATIVE_PLAN_TO_TWO_FACE_REQUESTS=PASS`
- `MPS2_REAL_GPT_IMAGE_2_TWO_FACES=PASS`
- `MPS2_PARTICIPANT_PRIMARY_FACE_BINDING=PASS`
- `MPS2_FACE_HITL_PENDING_REVIEW=PASS`
- `V3_MPS2_VISUAL_FACE_PROOF=PASS`
- `MPS2_VISUAL_ARTIFACT_COPY=PASS`
- `V3_MPS2_VISUAL_PROOF_RUNNER=PASS`

## Certification boundary

A passing visual proof establishes:

- real Director intent-to-plan behavior,
- real generative AI participant direction,
- deterministic Director-to-Face compilation,
- real Face pricing and authenticated generation,
- real `gpt-image-2` outputs,
- Participant/MediaAsset binding,
- Face-stage HITL pending state.

It does **not** by itself close full MPS2 certification. Before full MPS2 freeze, verify the Face execution is linked to canonical C5 `GenerationRequest` / `GenerationJob` identifiers and complete continuity/regeneration tests across multiple scenes/variants.
