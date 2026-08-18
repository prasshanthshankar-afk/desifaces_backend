# V3-MPS1B — Creative Director, LangGraph, RAG, UI and Assistant Context

Change-ID: `V3-MPS1B`
Status: `READY_FOR_RUNTIME_CERTIFICATION`
Owner: `#v3-core / Multi-Person + Story`
Date: `2026-08-18`
Parent foundation: `V3-MPS1`
Canonical V3 baseline: `44675e7c6a4977e23add93628ea44868b1de60a6`
Implementation branch: `feature/v3-multiperson-core-20260818`

## 1. Requirement

Multi-Person + Story is a creative AI orchestration capability, not only a relational data model.
The implementation must combine:

1. deterministic, account-owned canonical creative state;
2. LangGraph stateful orchestration;
3. schema-constrained LLM planning and critique;
4. hybrid retrieval over canonical creation context and creative knowledge;
5. human review / pause / resume;
6. structured UI consumption;
7. creation-specific Assistant/chatbot context;
8. deterministic compilation into canonical StoryGraph;
9. future Face, Audio and Fusion tool execution through certified GenerationRequest/GenerationJob boundaries.

The UI, Assistant and generation services MUST NOT invent independent story representations.

## 2. Architecture decision

The canonical architecture is:

```text
User / UI / Assistant brief
        |
        v
svc-director
        |
        v
LangGraph Creative Director
        |
        +-- retrieve canonical creation state
        +-- retrieve creative RAG context
        +-- LLM structured CreativeStoryPlan
        +-- Creative Critic
        +-- revise loop
        +-- human review interrupt
        +-- deterministic compiler
        |
        v
Canonical StoryGraph
        |
        +-- StoryWorkspaceView ------> Web / Mobile UI
        |
        +-- CreationContextBundle ---> Assistant / Chatbot
        |
        +-- GenerationRequest -------> Face / Audio / Fusion
```

`StoryGraph` is the business system of record.
LangGraph checkpoint state is orchestration working memory.
They are deliberately separate responsibilities.

## 3. Why deterministic persistence remains mandatory

LLMs are used for creative reasoning, planning, critique and revision. They do not own:

- account identity;
- user identity;
- participant IDs;
- project/story ownership;
- media ownership;
- pricing/credits/entitlements;
- provider capability truth;
- GenerationJob identity;
- final persistence constraints.

The LLM proposes a `CreativeStoryPlan`; deterministic code validates and compiles that plan into canonical state.

The model never receives permission to execute arbitrary SQL or persist arbitrary UUIDs.

## 4. LangGraph graph

The initial graph is:

```text
START
  |
  v
retrieve
  |
  v
plan
  |
  v
critique
  |
  +-- not ready / revisions available --> revise --> critique
  |
  +-- ready --> review (when required)
  |               |
  |               +-- reject/edit --> revise
  |               +-- approve -----> compile
  |
  +-- review disabled --------------> compile
                                          |
                                          v
                                         END
```

Graph state explicitly carries:

- `run_id`;
- `thread_id`;
- authenticated `account_id`;
- authenticated `owner_user_id`;
- creative brief;
- retrieved context + references;
- current structured plan;
- critique;
- revision count;
- review feedback/approval;
- compiled canonical StoryGraph;
- UI workspace projection;
- Assistant creation context;
- errors.

Account/user identity is persisted as explicit graph state so poll/resume operations cannot depend on out-of-band identity assumptions.

## 5. Durable checkpoints and HITL

`svc-director` uses `AsyncPostgresSaver` with the V3 Postgres database.

Checkpoint pool configuration is explicitly hardened with:

- `autocommit=True`;
- `prepare_threshold=0`;
- `row_factory=dict_row`;
- strict MsgPack deserialization through `LANGGRAPH_STRICT_MSGPACK=true`.

Human review uses LangGraph `interrupt()`.
A paused run is recoverable through the same `thread_id` after browser refresh/reconnect.
The API recovers persisted interrupt payloads from the checkpoint state for UI polling.

Resume requires the authenticated account/user to equal the account/user stored in graph state.

## 6. Structured LLM boundary

Initial provider adapter: OpenAI through `langchain-openai`.
The LangGraph orchestration and contracts remain provider-neutral.

Planner output is constrained to `CreativeStoryPlan`.
Critic output is constrained to `CreativeCritique`.

A `CreativeStoryPlan` contains:

- title/logline/summary;
- participants;
- participant roles/persona/continuity direction;
- scenes;
- participant membership per scene;
- setting;
- visual/audio/camera/performance direction;
- ordered dialogue;
- locale/emotion/delivery direction;
- continuity plan;
- retrieved-context references;
- explicit assumptions.

The model is explicitly instructed not to invent account IDs, media IDs, pricing, entitlements or provider capability facts.

## 7. Participant identity hardening

`PlannedParticipant.participant_id` may echo an existing participant ID from retrieved context.
The compiler accepts it only when the exact participant exists in the authenticated account and project.

An arbitrary/model-invented participant UUID is ignored and replaced with a deterministic Director-thread-derived UUID.

Existing participant identity may also be resolved from the authenticated project by display identity during this first increment; later participant editing APIs will make explicit selection primary.

## 8. Idempotent compilation

`CanonicalStoryCompiler` derives new IDs using deterministic UUID5 keys scoped to the Director thread for:

- project;
- story;
- participants;
- scenes;
- dialogue turns.

Replaying/resuming the same Director thread therefore does not create duplicate canonical creative identities.
If the compiled story already exists, the compiler returns the existing canonical StoryGraph.

## 9. Hybrid RAG

RAG is intentionally split into two evidence classes.

### 9.1 Authoritative structured retrieval

Direct canonical/database/service retrieval is used for:

- authenticated account/user;
- existing Project;
- existing StoryGraph;
- Participants;
- primary Face MediaAsset references;
- voice-profile references;
- participant persona/continuity;
- existing scenes/dialogue.

Future Director tools will similarly query authoritative masterdata, pricing, entitlements, safety, provider capability and job state from their owning services rather than vector retrieval.

### 9.2 Creative knowledge retrieval

Product creative knowledge is stored separately from EIP engineering knowledge.

New relations:

- `v3_creative_knowledge_sources`;
- `v3_creative_knowledge_chunks`;
- `v3_director_retrieval_events`.

Chunks support:

- content;
- source/revision;
- locale;
- tags;
- optional pgvector embedding;
- embedding model;
- metadata;
- active lifecycle.

When `DF_DIRECTOR_EMBEDDING_MODEL` is configured, retrieval uses semantic vector similarity.
When it is not configured, retrieval safely falls back to PostgreSQL full-text search.

Every retrieval run is auditable by account/project/story/thread and source references.

RAG may ground creativity but must not infer or impose stereotypes about ethnicity, religion, socioeconomic status, facial anatomy or other protected/personal traits from geography/community metadata.

## 10. UI contract

The UI does not receive an opaque model response as product state.

`StoryWorkspaceView` is the stable Web/Mobile projection and includes:

- project/story IDs;
- story status/revision;
- Director run state;
- active scene;
- Participants with face/voice/persona/continuity state;
- Scenes ordered by sequence;
- scene participants;
- setting/visual/camera direction;
- ordered DialogueTurns with speaker identity/name;
- locale/emotion;
- generated Audio MediaAsset link when available;
- scene preview/final media;
- generation state;
- warnings;
- allowed UI actions;
- final Story MediaAsset when available.

This is designed for participant sidebars, scene timelines, dialogue editors, generation progress and Assistant integration without UI-side domain reconstruction.

## 11. Assistant/chatbot context contract

`CreationContextBundle` is the creation-specific context supplied to Assistant.

It contains:

- canonical account/project/story/scene IDs;
- participant IDs;
- concise story summary;
- participant context;
- scene context;
- dialogue/speaker context;
- continuity context;
- generation context;
- media context;
- pricing context when added by the owning pricing tool;
- retrieved creative source references;
- allowed Assistant actions;
- context version/timestamp.

It intentionally does NOT expose:

- hidden chain-of-thought;
- internal LLM reasoning tokens;
- secrets;
- provider credentials;
- arbitrary SQL state.

The future Assistant service will inject this bundle for requests such as:

- “Why did you choose this setting?”
- “Make scene 2 warmer.”
- “Change Ananya’s dialogue but keep her face and voice.”
- “Which person still needs a voice?”
- “How much will generating the remaining scenes cost?”

The Assistant then uses tool APIs for changes rather than directly mutating Story tables.

## 12. V3-only API surface

New service: `svc-director`.

V3 local endpoint: `127.0.0.1:18011` -> container `8011`.

Initial APIs:

- `GET /api/health`
- `POST /api/director/runs`
- `GET /api/director/runs/{thread_id}`
- `POST /api/director/runs/{thread_id}/resume`
- `GET /api/director/stories/{story_id}/workspace`
- `GET /api/director/stories/{story_id}/assistant-context`

This is a new V3 capability and intentionally has no V2 route parity requirement.
Existing Face/Audio/Fusion public compatibility routes remain unchanged.

## 13. Authentication / authorization

Director validates bearer JWT signature, issuer and audience using the same contract as the certified Face service.
The JWT subject must resolve to an existing `core.users` row.
Canonical account ownership is resolved through the shared C3 account-context resolver.

Run polling/resume verifies persisted graph account/user against the current authenticated actor.
Story workspace/context reads are filtered by authenticated account.

## 14. Generation boundary

MPS1B does not activate Face, Audio or Fusion provider workers.

The Director first compiles canonical Story state.
Subsequent MPS2/MPS3/MPS4 increments will translate approved Participant/Scene/Dialogue intent into certified C5 `GenerationRequest` / `GenerationJob` records and invoke capabilities through their owned adapters/tools.

This keeps creative planning separate from expensive/irreversible execution.

## 15. UI / Assistant synchronization invariant

For every compiled creation:

```text
canonical StoryGraph story_id
   == StoryWorkspaceView.story_id
   == CreationContextBundle.story_id

canonical Project project_id
   == StoryWorkspaceView.project_id
   == CreationContextBundle.project_id

canonical Participant IDs
   == UI Participant IDs
   == Assistant Participant IDs
```

A deterministic LangGraph unit test enforces this invariant with fake retriever/planner/critic/compiler components and no external LLM call.

## 16. Runtime certification scope

Foundation certification must prove:

1. all V3 canonical tests pass;
2. LangGraph Director graph test passes without external LLM calls;
3. only `desifaces_v3` is targeted;
4. pre-migration backup exists;
5. MPS1A schemas apply;
6. Creative Director RAG schema applies;
7. deterministic one-person and multi-person StoryGraph runtime proof passes;
8. Story-linked GenerationRequest proof passes;
9. `svc-director` image starts on V3 only;
10. health reports Postgres LangGraph checkpoint mode;
11. LangGraph checkpoint tables exist;
12. structured Director APIs appear in OpenAPI;
13. no V3 execution worker/scheduler is started;
14. Fusion recovery remains disabled;
15. strict MsgPack mode is enabled;
16. V2 Fusion remains healthy;
17. V2/V3 Fusion compatibility path parity remains unchanged.

Certification deliberately does NOT call a live creative LLM/provider. It certifies orchestration safety, persistence, contracts and consumption boundaries.

## 17. Next certification after foundation

After MPS1A + MPS1B runtime certification, a separate Creative Quality/RAG certification will configure an approved Director model and controlled creative knowledge corpus and validate:

- brief -> structured plan quality;
- one-person planning;
- two-person dialogue;
- 3+ participant planning;
- multi-scene story quality;
- cultural/context grounding without stereotypes;
- retrieval relevance/source traceability;
- critique/revision behavior;
- HITL approval/resume;
- latency/token/cost telemetry;
- safe failure behavior.

Only after that quality gate do we enable MPS2 participant-aware Face orchestration through the Director.

## 18. Files

Contracts:
- `services/shared/df_contracts/v3/director.py`
- `services/shared/df_contracts/v3/story.py`

Shared orchestration/projections:
- `services/shared/python/desifaces_shared/v3/director_graph.py`
- `services/shared/python/desifaces_shared/v3/creation_context.py`
- `services/shared/python/desifaces_shared/v3/story_store.py`

Director service:
- `services/svc-director/app/app/main.py`
- `services/svc-director/app/app/security.py`
- `services/svc-director/app/app/db.py`
- `services/svc-director/app/app/retrieval.py`
- `services/svc-director/app/app/llm.py`
- `services/svc-director/app/app/compiler.py`
- `services/svc-director/app/requirements.txt`
- `services/svc-director/app/Dockerfile.v3`

Database:
- `migrations/2026_08_18_v3_creative_director_rag.sql`

Tests/certification:
- `test/test_v3_creative_director_foundation.py`
- `scripts/v3-multiperson-story-certify.sh`
- `.github/workflows/v3-contract-tests.yml`
