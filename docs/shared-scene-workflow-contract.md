# Shared-Scene Conversation Workflow Contract

Status: DEV contract for multi-person group-photo conversations.

## Goal

Make web, mobile, Director, Face, Audio, Fusion, and PostgreSQL consume one durable workflow state instead of reconstructing phase state independently in each client.

## Ownership

- PostgreSQL is the durable source of truth.
- `v3_studio_workflows.metadata_json` owns workflow-level shared-scene decisions:
  - `shared_scene_people_approved`
  - `shared_scene_people_snapshot`
  - `shared_scene_source_mode`
  - `shared_scene_state_version`
- `v3_studio_stage_runs.metadata_json` on the Fusion scene owns scene-level configuration:
  - draft/approved shared-scene media
  - image dimensions
  - participant targets
  - selected aspect ratio
  - video motion settings
- Existing Studio stage rows remain authoritative for Audio/Fusion execution state.
- Existing `final_media_id` remains authoritative for final output.

No new tables are required.

## Canonical projection

Clients read:

`GET /api/director/studio-workflows/{workflow_id}/shared-scene-state`

The response contains exactly one current `phase`, one `next_action`, and an `allowed_actions` set.

Canonical phases:

1. `people`
2. `group_photo_source`
3. `group_photo_prepare`
4. `group_photo_map`
5. `group_photo_approve`
6. `audio`
7. `video`
8. `final`

Clients must not infer the current phase from local state.

## Commands

The source decision is durable:

`PUT /api/director/studio-workflows/{workflow_id}/shared-scene-source`

Body:

```json
{"mode":"generate"}
```

or:

```json
{"mode":"upload"}
```

Rules:

- People must be approved first.
- Generate currently requires exactly two speakers.
- A source-mode change is rejected after a photo has been selected.
- A source-mode change is rejected after the group photo is approved.
- Repeating the same source-mode command is idempotent.

## Approved people snapshot

People approval persists the exact approved speaker context into
`shared_scene_people_snapshot`.

The snapshot contains participant id, display name, gender presentation, age,
country and region. Generation clients use this approved snapshot instead of
reconstructing demographic inputs from stale browser state or older Director text.

## Client rule

Web and mobile may keep temporary form state, but temporary state is never the
workflow authority.

On story/workflow load:

1. Load workflow.
2. Load canonical shared-scene state.
3. Render the phase returned by the backend.
4. Execute only an allowed action.
5. After every successful command, reload canonical state.

## Invalidation

A pricing quote, generated photo, mapping, audio job or Fusion job belongs to the
state version and durable inputs that produced it. A client-side navigation event
must never carry these objects into another story.

## Safety and billing

This contract does not move pricing or provider execution ownership into Director.
Face, Audio and Fusion keep their existing pricing/execution contracts. Director
only owns orchestration, durable phase decisions, HITL gates and lineage.
