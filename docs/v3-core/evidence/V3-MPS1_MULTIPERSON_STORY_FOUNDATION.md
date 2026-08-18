# V3-MPS1 — Multi-Person + Story Domain Foundation

Change-ID: `V3-MPS1`
Status: `READY_FOR_RUNTIME_CERTIFICATION`
Owner: `#v3-core / Multi-Person + Story`
Date: `2026-08-18`
Canonical V3 baseline: `44675e7c6a4977e23add93628ea44868b1de60a6`
Implementation branch: `feature/v3-multiperson-core-20260818`
Companion orchestration evidence: `V3-MPS1B_CREATIVE_DIRECTOR_LANGGRAPH_RAG.md`

## 1. Product requirement

Create one canonical domain that supports:

1. the current one-person Face -> Audio -> Fusion journey;
2. two-person conversations/couples/interviews;
3. groups with three or more participants;
4. persistent character identity across scenes;
5. ordered dialogue with a durable speaker identity;
6. multi-scene storytelling;
7. the MPS1B LangGraph Creative Director that converts natural-language intent into Participants, Scenes, Dialogue, UI projections, Assistant context and later generation jobs.

The architecture MUST NOT fork into separate `single-person` and `multi-person` business domains.
MPS1 deterministic state and MPS1B agentic orchestration are certified as one combined foundation.

## 2. EIP evidence

EIP source: `prasshanthshankar-afk/desifaces-eos`, `feature/eos-foundation`.
Primary standard: `ekb/06-integration/Integration_Architecture_Standard.md`.

Applicable rules retrieved before implementation:
- backend services own canonical business rules;
- APIs and persisted boundaries must use explicit typed contracts;
- AI generation with variable latency uses durable asynchronous jobs;
- prompt/model/provider execution is separated from product intent;
- integrations must be explicit, observable, resilient and replaceable;
- retries/idempotency must not duplicate expensive AI execution;
- product development must retrieve EIP/source evidence before implementation.

## 3. V2 evidence

V2 Face has a Phase-1 composition transport model:
- `single_person`;
- `two_people`;
- optional subject gender/relationship hints;
- masterdata `face_generation_subject_compositions.subject_count` currently permits 1..12.

This is useful compatibility/provider evidence but is not sufficient as a V3 domain because it does not create durable participant identity that can persist from Face to Audio to Fusion or across Story scenes.

Audio remains fundamentally one text/voice request and Fusion remains fundamentally one primary face/audio source. Those capability changes belong to later MPS increments and are intentionally not hidden inside MPS1.

## 4. Core architecture decision

### 4.1 Cardinality

Canonical cardinality is `1..N Participant`.

There is no canonical enum for `single_person`, `two_people`, or `group`. Those values may still exist as:
- compatibility API inputs;
- UI presentation;
- provider capability/routing projections;
- DB masterdata for provider prompt composition.

Provider-specific maximum subject counts MUST NOT become the canonical Story/Participant domain limit.

### 4.2 Canonical graph

```text
Project
  |
  +-- Participant 1..N
  |     +-- primary Face MediaAsset
  |     +-- Face reference MediaAssets
  |     +-- voice profile reference
  |     +-- persona / continuity metadata
  |
  +-- Story 0..N
        |
        +-- StoryParticipant 1..N
        |
        +-- Scene 0..N (ordered)
              |
              +-- SceneParticipant 0..N
              |     +-- placement
              |     +-- performance direction
              |
              +-- DialogueTurn 0..N (ordered)
                    +-- speech -> speaker Participant required
                    +-- narration -> speaker optional
                    +-- action -> speaker optional
```

A basic one-person studio project can persist only Project + one Participant and generate Face/Audio/Fusion. Story is optional until storytelling is used.

### 4.3 Participant identity

`Participant` is the durable business identity used across capabilities.

It owns/references:
- canonical account/project;
- kind: person/character/pet/other;
- display/persona metadata;
- primary Face `MediaAsset`;
- additional identity reference media;
- provider-neutral voice profile reference;
- locale;
- continuity metadata.

The participant ID—not array position, gender, Face job ID, voice ID or provider ID—is the cross-capability identity key.

### 4.4 Story continuity

Continuity belongs to Participant/Story/Scene metadata and Creative Director policy. A provider prompt may consume it, but providers do not own it.

Examples:
- identity lock / visual continuity;
- relationship/role information;
- attire continuity;
- voice continuity;
- scene-to-scene narrative state.

### 4.5 Dialogue

Dialogue is persisted as ordered `DialogueTurn` records. Speech requires `speaker_participant_id`. This is the critical bridge to participant-aware Audio and active-speaker Fusion.

### 4.6 Generation linkage

C5 `GenerationRequest` now optionally carries:
- `story_id`;
- `scene_id`;
- existing `participant_ids`.

New Story-driven generation must use `CanonicalStoryGenerationStore`, which validates account/project/story/scene/participant ownership before creating the certified C5 GenerationRequest/GenerationJob records.

Existing C3 compatibility Face/Audio/Fusion flows are unchanged.

## 5. Database implementation

Migration:
`migrations/2026_08_18_v3_multiperson_story_foundation.sql`

New canonical relations:
- `v3_projects`;
- `v3_participants`;
- `v3_participant_media`;
- `v3_stories`;
- `v3_story_participants`;
- `v3_scenes`;
- `v3_scene_participants`;
- `v3_dialogue_turns`;
- `v3_story_graph_summary` read model.

Additive C5 columns:
- `v3_generation_requests.story_id`;
- `v3_generation_requests.scene_id`.

Cross-aggregate DB guards prevent:
- story participants from another project;
- scene participants not registered in the Story;
- dialogue speakers not registered in the Story;
- participant media owned by another billing account.

## 6. Shared contracts and persistence

Contracts:
- `services/shared/df_contracts/v3/story.py`;
- `GenerationRequest.story_id/scene_id` in `domain.py`.

Persistence:
- `CanonicalStoryStore`;
- `CanonicalStoryGenerationStore`.

No FastAPI, provider SDK, pricing call, storage write or AI invocation is performed by these shared persistence boundaries.

Agentic orchestration, RAG, UI projection and Assistant context are specified separately but co-certified in `V3-MPS1B_CREATIVE_DIRECTOR_LANGGRAPH_RAG.md`.

## 7. Compatibility strategy

MPS1 does not remove or rename current Face/Audio/Fusion public routes.

Legacy Face composition becomes a projection:

```text
1 Participant  -> single_person
2 Participants -> two_people / compatible pairing projection
3+ Participants -> group/provider-capability projection
```

The canonical system never reconstructs participant identity from those labels.

## 8. Security and ownership

Every Project/Participant/Story is account-owned. Story-driven generation validates the same account and project before C5 persistence. Cross-account participant media attachment is rejected at the DB boundary.

## 9. Pricing impact

None in MPS1. No credit reservation/commit is performed. Later multi-person pricing must be quoted by Pricing from explicit workload units (participants, audio turns, render strategy, duration) rather than hard-coded client multiplication.

## 10. Provider impact

None in MPS1 deterministic persistence. The MPS1B Creative Director may plan with an LLM, but foundation certification uses deterministic fake model/retrieval adapters and does not invoke a live AI provider. Face/Audio/Fusion provider execution remains disabled.

## 11. Certification plan

Repository command:
`scripts/v3-multiperson-story-certify.sh`

Combined MPS1 + MPS1B certification must prove:
- all V3 unit tests pass before DB mutation;
- deterministic LangGraph planning/projection test passes without a live LLM;
- LangGraph review interrupt/resume test passes;
- migration targets only `desifaces_v3`;
- pre-migration V3 DB backup/checksum exists;
- one-participant StoryGraph roundtrip;
- three-participant StoryGraph roundtrip;
- Story/Scene/Participant -> C5 GenerationRequest linkage;
- same Story generation idempotently replays to the same canonical IDs;
- participant outside the Story is rejected from Story generation;
- all synthetic Project/Participant/Story/Generation rows roll back;
- Creative Director RAG schema applies;
- V3-only `svc-director` starts and creates Postgres checkpoint tables;
- structured Director run/workspace/Assistant-context API surface exists;
- UI workspace and Assistant context share canonical Story/Project/Participant IDs;
- no V3 execution worker/scheduler is activated;
- Fusion recovery remains disabled;
- V2 Fusion remains healthy;
- public V2/V3 Fusion `.paths` parity is unchanged.

Only after those gates pass do MPS1 and MPS1B become `CERTIFIED`.

## 12. Implementation sequence

### MPS1B — Creative Director foundation — CURRENT / companion to MPS1
- LangGraph stateful creative orchestration;
- schema-constrained LLM plan/critique contracts;
- hybrid structured + creative RAG;
- Postgres checkpoints;
- HITL pause/review/resume;
- deterministic Story compiler;
- `StoryWorkspaceView` for UI;
- focus-aware `CreationContextBundle` for Assistant/chatbot;
- V3-only `svc-director` API.

### MPS2 — Participant-aware Face
- adapt current one-person Face into one Participant;
- generate/update Face identity for a specific `participant_id`;
- support 2+ participant identity preparation without relying on list position;
- maintain identity/continuity across scenes;
- invoke through Director tool orchestration after approved plan;
- keep current mobile Face API compatibility.

### MPS3 — Speaker-aware Audio
- dialogue compiler: DialogueTurn -> per-participant TTS units;
- durable participant voice binding;
- locale/emotion/delivery per turn;
- individual audio MediaAssets + ordered timeline/mix;
- pricing based on actual audio work;
- invoke from approved Director Scene/Dialogue state.

### MPS4 — Multi-Person Fusion
- SceneParticipant -> spatial composition;
- active speaker timeline;
- per-person lip sync;
- non-speaking reactions/gesture direction;
- one-person remains same execution model with one participant;
- provider capability routing remains replaceable;
- use Director camera/performance/continuity direction as structured input.

### MPS5 — Full Story execution orchestration
- approved Story -> scene execution plan;
- parent/child C5 GenerationJobs;
- Face identity reuse;
- Audio turn generation;
- Fusion scene rendering;
- scene stitching/final Story MediaAsset;
- restart/retry at scene/participant granularity;
- Director tool nodes monitor and adapt deterministic execution state.

### MPS6 — Product UI integration
- participant workspace;
- story/scene editor;
- dialogue timeline;
- mobile progressive UX;
- rich web multi-panel editor;
- Assistant consumes the same canonical creation context and invokes Director/tools for edits/actions.
