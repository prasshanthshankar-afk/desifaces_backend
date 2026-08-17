# desifaces-v3 Backend Baseline

## Purpose
This file records the immutable bootstrap point for the desifaces-v3 backend development line.

## Inherited V2 Source
- Repository: `prasshanthshankar-afk/desifaces_backend`
- V3 branch: `desifaces-v3`
- V2 source branch: `main`
- V2 source commit: `70a80cef08cebb8f02385a8e0f1adbac7c85fbb8`
- Bootstrap date: 2026-08-17

The V3 branch started as an as-is copy of this exact V2 commit. V2 production/release branches must remain unaffected by V3 development.

## Architecture Control
`#v3-core` is the source of truth for desifaces-v3 Architecture & Integration Control. Cross-cutting changes to the common domain model, APIs, database evolution, service boundaries, identity/auth, pricing/credits, media lifecycle, provider orchestration, deployment topology, and cross-stream contracts must be governed through `#v3-core`.

## EIP Rule
Before changing, reusing, or replacing V2 behavior, use EIP continuously to obtain current implementation evidence from V2: code paths, API contracts, schemas/migrations, masterdata, configuration, dependencies, tests, runtime/deployment behavior, and operational assumptions.

## Evolution Rule
V3 is allowed to evolve aggressively, but inherited V2 behavior is not considered a V3 architectural contract merely because it exists. Every material cross-cutting change must follow:

Requirement -> EIP V2 evidence -> V3 architecture decision -> contract/schema impact -> compatibility/migration strategy -> implementation -> certification -> freeze in `#v3-core`.
