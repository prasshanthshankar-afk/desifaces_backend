# V3-MPS1C — Staged Studio HITL Orchestration

Change-ID: `V3-MPS1C`
Status: `READY_FOR_FUNCTIONAL_CERTIFICATION`
Owner: `#v3-core / Multi-Person + Story`
Date: `2026-08-18`
Implementation branch: `feature/v3-multiperson-core-20260818`

## 1. Product invariant

The desifaces Studio workflow remains explicit and reviewable at every capability boundary.

### Direct/current flow

```text
Participant
  -> Face Studio
  -> Face output review / approval
  -> Audio Studio consuming the approved Face
  -> Audio output review / approval
  -> Fusion consuming approved Face + approved Audio
  -> Fusion video review / approval
```

Story is not required for this flow.

### Story flow

```text
StoryGraph
  -> Face stage per Participant
  -> HITL approval per active Face output
  -> Audio stage per speech DialogueTurn
       dependency: approved Face for that speaker
  -> HITL approval per active Audio output
  -> Fusion stage per Scene
       dependencies: approved Faces for scene cast + approved Audio turns for scene
  -> HITL approval per active scene video
  -> for multi-scene Story: final assembly stage
       dependencies: approved scene Fusion outputs
  -> final Story review / approval
```

The canonical architecture does not permit a downstream Studio stage to consume an unapproved upstream Studio artifact.

## 2. Why this is separate from the Creative Director graph

Two long-running state machines have different responsibilities:

1. `Creative Director`: brief -> retrieval -> LLM plan -> critic/revision -> creative-plan HITL -> canonical StoryGraph.
2. `Studio Execution`: canonical creation -> Face -> review -> Audio -> review -> Fusion -> review -> optional Story assembly -> review.

Creative reasoning and media execution are therefore coupled by canonical IDs, not by one monolithic HTTP request or hidden prompt chain.

## 3. Durable asynchronous Director execution

`POST /api/director/runs` is a control-plane operation and returns HTTP 202 with a durable run/thread identifier. Long-running LLM/RAG/LangGraph execution is performed by `svc-director-worker` under the explicit `v3-orchestration` Compose profile.

Canonical Director run states include:
- queued;
- running;
- awaiting_review;
- ready;
- failed.

PostgreSQL `v3_director_runs` owns queue/lease/retry metadata. LangGraph PostgreSQL checkpoints own graph execution/checkpoint state. Human resume decisions requeue the same thread and reset the technical retry budget for that new human-directed execution cycle.

This removes LLM latency from the HTTP request lifetime.

## 4. Studio persistence

Relations:
- `v3_studio_workflows`;
- `v3_studio_stage_runs`;
- `v3_studio_stage_dependencies`;
- `v3_studio_stage_inputs`;
- `v3_studio_stage_outputs`;
- `v3_studio_review_items`.

Migrations:
- `2026_08_18_v3_studio_hitl_workflow.sql`;
- `2026_08_18_v3_studio_hitl_hardening.sql`.

## 5. HITL enforcement

A stage output creates an explicit review item. The stage remains `awaiting_review` until its active outputs are resolved.

Approval rules:
- downstream stage dependencies must be `approved` before generation can start;
- an upstream artifact can become a downstream input only when the exact source output is active and approved;
- Studio media must belong to the same billing account as the workflow;
- upstream source stage and downstream target stage must belong to the same workflow;
- a stage cannot become approved without at least one active output;
- every active output must be approved before the stage becomes approved.

## 6. Variant / supersession behavior

Studios may generate alternatives. Rejected or `revise` outputs remain as historical/audit evidence but are marked inactive. Inactive variants do not block approval of the selected active output and cannot be used downstream.

This supports Face variants, alternative Audio takes and Fusion video variants without losing review history.

## 7. Direct vs Story scope

Canonical stage/scope combinations are:
- Face -> participant;
- Audio -> participant for direct flow OR dialogue_turn for Story;
- Fusion -> participant for direct flow OR scene for Story;
- Story final -> story.

This preserves the current one-person Studio experience while enabling 1..N participant storytelling on the same execution engine.

## 8. UI contract

`StudioWorkflowView` is a structured UI read model containing:
- workflow state/current stage;
- every stage and scope;
- C5 request/job IDs when assigned;
- input artifacts;
- output variants and active/inactive state;
- review items/decisions/feedback;
- next review action.

Story workspace projection also maps:
- Face stage state -> Participant card generation state;
- Audio stage state -> Dialogue row generation state;
- Fusion stage state -> Scene generation state.

The UI therefore does not reconstruct workflow state from provider job IDs.

## 9. Assistant context

The latest Studio workflow is projected into `CreationContextBundle.generation_context`. The projection honors Story/Scene/Participant/Scene+Participant focus so the Assistant can answer creation-specific questions such as:
- which Face is awaiting approval;
- whether Audio can start;
- which dialogue turn failed or needs review;
- why Fusion is blocked;
- which scene video is approved;
- what the next permitted action is.

The Assistant receives canonical IDs and review state, not hidden chain-of-thought.

## 10. Execution safety

MPS1C does not enable Face/Audio/Fusion provider workers. `svc-director-worker` is an orchestration worker under the separate `v3-orchestration` profile. Existing provider/background workers remain gated by `v3-execution`.

MPS2, MPS3 and MPS4 will bind the corresponding stage runs to certified C5 GenerationRequest/GenerationJob execution and attach produced MediaAssets to these gates.

## 11. Functional certification

The repository functional gate is:

`scripts/v3-mps-functional-test.sh`

It must prove:
- participant-reference regression tests pass;
- Studio HITL topology unit tests pass;
- target DB is `desifaces_v3`;
- both MPS1C migrations apply idempotently;
- Director HTTP API is nonblocking/durable-queue mode;
- orchestration worker is running;
- direct Face -> Audio -> Fusion workflow has exactly three stages;
- Audio rejects unapproved Face and accepts approved Face;
- Fusion rejects unapproved Audio and accepts approved Face + Audio;
- variant selection/supersession behaves correctly;
- deterministic direct-flow synthetic media/data roll back;
- live LLM/RAG Director run pauses for creative HITL, persists checkpoint, resumes same thread and compiles a canonical Story;
- 2-person/2-scene Story generates the expected Face/Audio/Fusion/final dependency DAG;
- Story UI and scoped Assistant context remain synchronized;
- no V3 provider execution worker is started;
- V2 Fusion remains healthy.

Only after this gate passes should MPS1C be marked `CERTIFIED`.
